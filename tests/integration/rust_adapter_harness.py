"""Shared real-process harness for Rust DAP integration tests."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
from typing import AsyncIterator, Callable

import pytest

from tdb import run_mode
from tdb.dap.client import DAPClient
from tdb.dap.messages import Event, Request
from tdb.languages.rust import build_rust_profile
from tdb.persist import TdbConfig
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController
from tdb.session.inspect_service import InspectService
from tdb.session.state import SessionPhase

WAIT = 30.0
PAUSE_TIMEOUT = 10.0
SUPPORTED_RUST_VERSION = (1, 98, 0)
SCENARIOS = {
    "join",
    "mutex",
    "rwlock-read",
    "rwlock-write",
    "condvar",
    "mpsc-send",
    "mpsc-recv",
    "park",
    "cycle",
    "incomplete-cycle",
    "healthy-blocked",
}


def _version(command: list[str], pattern: str) -> tuple[int, ...] | None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError:
        return None
    match = re.search(pattern, result.stdout + result.stderr)
    return tuple(int(part) for part in match.groups()) if match else None


def rustc_version() -> tuple[int, ...] | None:
    rustc = shutil.which("rustc")
    if rustc is None:
        return None
    return _version([rustc, "--version"], r"rustc\s+(\d+)\.(\d+)\.(\d+)")


def require_supported_rust_concurrency() -> None:
    found = rustc_version()
    if found != SUPPORTED_RUST_VERSION:
        rendered = ".".join(map(str, found)) if found is not None else "missing"
        pytest.skip(
            "Rust layout-specific concurrency evidence requires rustc 1.98.0; "
            f"found {rendered}"
        )


def _gdb_dap_major() -> int | None:
    gdb = shutil.which("gdb")
    if gdb is None:
        return None
    version = _version([gdb, "--version"], r"(?:GNU gdb[^\d]*)?(\d+)(?:\.\d+)")
    return version[0] if version else None


def lldb_dap_available() -> bool:
    return shutil.which("lldb-dap") is not None


def available_rust_adapters() -> list[str]:
    adapters: list[str] = []
    if sys.platform != "darwin" and (_gdb_dap_major() or 0) >= 14:
        adapters.append("gdb")
    if lldb_dap_available():
        adapters.append("lldb-dap")
    return adapters


@pytest.fixture(scope="session")
def rust_debug_binary(tmp_path_factory) -> Callable[[str, str | None], str]:
    rustc = shutil.which("rustc")
    if rustc is None:
        pytest.skip("rustc not installed")
    source = Path(__file__).parent / "fixtures" / "rust_concurrency.rs"
    build_dir = tmp_path_factory.mktemp("rust-concurrency")
    built: dict[tuple[str, str | None], str] = {}

    def build(case: str, adapter: str | None = None) -> str:
        if case not in SCENARIOS:
            raise ValueError(f"unknown Rust concurrency scenario: {case}")
        key = (case, adapter)
        if key not in built:
            suffix = adapter or "any"
            binary = build_dir / f"{case}-{suffix}"
            env = {**os.environ, "TDB_RUST_CASE": case}
            subprocess.run(
                [
                    rustc,
                    "-C",
                    "debuginfo=2",
                    "-C",
                    "opt-level=0",
                    "-o",
                    str(binary),
                    str(source),
                ],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            built[key] = str(binary)
        return built[key]

    return build


@asynccontextmanager
async def _ready_listener() -> AsyncIterator[tuple[int, asyncio.Future[str]]]:
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[str] = loop.create_future()

    async def connected(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            payload = (await reader.read(128)).decode("utf-8", errors="replace")
            if not ready.done():
                ready.set_result(payload)
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(connected, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port, ready
    finally:
        server.close()
        await server.wait_closed()


async def launch_and_pause(binary: str, adapter: str) -> DebugController:
    """Launch, configure, await fixture readiness, pause, and load a stack."""
    handler = ServerEventHandler()
    ctrl = DebugController(handler, profile=build_rust_profile(adapter=adapter))
    try:
        async with _ready_listener() as (port, ready):
            await ctrl.start(
                program=binary,
                args=[str(port)],
                stop_on_entry=False,
            )
            await asyncio.wait_for(handler.initialized_event.wait(), WAIT)
            await ctrl.do_configure()
            case = await asyncio.wait_for(ready, WAIT)
            assert Path(binary).name.startswith(case)
        assert await ctrl.pause(timeout=PAUSE_TIMEOUT)
        await ctrl.fetch_stop_info()
        return ctrl
    except BaseException:
        try:
            await asyncio.wait_for(ctrl.stop(), WAIT)
        except BaseException:
            pass
        raise


async def run_mode_pause_probe(binary: str, adapter: str) -> bool:
    """Pause a ready blocked fixture through run()'s on_session_ready hook."""
    pause_result: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    episode_result = False
    pause_task: asyncio.Task[None] | None = None

    async with _ready_listener() as (port, fixture_ready):

        def session_ready(ctrl: DebugController) -> None:
            nonlocal pause_task

            async def pause_when_fixture_ready() -> None:
                try:
                    await asyncio.wait_for(fixture_ready, WAIT)
                    result = await ctrl.pause(timeout=PAUSE_TIMEOUT)
                    if not pause_result.done():
                        pause_result.set_result(result)
                except BaseException as exc:
                    if not pause_result.done():
                        pause_result.set_exception(exc)
                    raise

            pause_task = asyncio.create_task(pause_when_fixture_ready())

        async def inspect_episode(controller, handler, console, config, program):
            nonlocal episode_result
            snapshot = await InspectService(
                lambda: controller
            ).collect_rust_concurrency()
            episode_result = (
                controller.state.phase is SessionPhase.STOPPED
                and any(edge.operation == "park" for edge in snapshot.edges)
            )
            return False

        code = await asyncio.wait_for(
            run_mode.run(
                program=binary,
                args=[str(port)],
                profile=build_rust_profile(adapter=adapter),
                config=TdbConfig(),
                tui_episode=inspect_episode,
                on_session_ready=session_ready,
            ),
            WAIT * 3,
        )
        if pause_task is not None:
            await pause_task
        return code == 0 and await pause_result and episode_result


async def terminal_launch_probe(binary: str) -> Request:
    """Drive lldb-dap as a client that fulfills its runInTerminal request."""
    client = DAPClient(build_rust_profile(adapter="lldb-dap").adapter)
    initialized = asyncio.Event()
    stopped = asyncio.Event()
    request_seen: asyncio.Future[Request] = asyncio.get_running_loop().create_future()
    spawned: asyncio.subprocess.Process | None = None

    def event_setter(event: Event) -> None:
        initialized.set()

    def stopped_setter(event: Event) -> None:
        stopped.set()

    async def fake_terminal(request: Request) -> dict[str, int]:
        nonlocal spawned
        if not request_seen.done():
            request_seen.set_result(request)
        arguments = request.arguments
        env = {**os.environ, **(arguments.get("env") or {})}
        spawned = await asyncio.create_subprocess_exec(
            *arguments["args"],
            cwd=arguments.get("cwd"),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        return {"processId": spawned.pid}

    client.on_event("initialized", event_setter)
    client.on_event("stopped", stopped_setter)
    client.on_reverse_request("runInTerminal", fake_terminal)
    try:
        await client.start()
        await client.initialize(support_run_in_terminal=True)
        launch_future = await client.launch(
            program=binary,
            cwd=str(Path(binary).parent),
            stop_on_entry=True,
            console="externalTerminal",
        )
        await asyncio.wait_for(initialized.wait(), WAIT)
        await client.configuration_done()
        response = await asyncio.wait_for(launch_future, WAIT)
        assert response.success
        await asyncio.wait_for(stopped.wait(), WAIT)
        threads = await client.threads()
        assert threads
        assert await client.stack_trace(threads[0].id)
        return await asyncio.wait_for(request_seen, WAIT)
    finally:
        try:
            await client.disconnect(terminate=True)
        finally:
            await client.stop()
        if spawned is not None and spawned.returncode is None:
            spawned.terminate()
            try:
                await asyncio.wait_for(spawned.wait(), 5.0)
            except asyncio.TimeoutError:
                spawned.kill()
                await spawned.wait()


def _unused_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _wait_stub_listening(
    process: asyncio.subprocess.Process, port: int
) -> str:
    assert process.stdout is not None
    evidence: list[str] = []
    while True:
        line = await asyncio.wait_for(process.stdout.readline(), WAIT)
        if not line:
            raise RuntimeError(
                "remote stub exited before listening: " + "".join(evidence).strip()
            )
        text = line.decode("utf-8", errors="replace")
        evidence.append(text)
        lowered = text.lower()
        if "listen" in lowered and str(port) in text:
            return "".join(evidence)


async def remote_attach_probe(binary: str, adapter: str) -> bool:
    """Attach through the matching installed native remote stub."""
    readiness_fd: int | None = None
    inherited_fd: int | None = None
    if adapter == "gdb":
        stub = shutil.which("gdbserver")
        if stub is None:
            pytest.skip("gdbserver not installed")
        stub_command = [stub, "--once", f"127.0.0.1:{_unused_loopback_port()}"]
        port = int(stub_command[-1].rsplit(":", 1)[1])
    else:
        stub = shutil.which("lldb-server")
        if stub is None:
            pytest.skip("lldb-server not installed")
        readiness_fd, inherited_fd = os.pipe()
        stub_command = [
            stub,
            "gdbserver",
            "--pipe",
            str(inherited_fd),
            "127.0.0.1:0",
        ]
    process = await asyncio.create_subprocess_exec(
        *stub_command,
        binary,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
        pass_fds=((inherited_fd,) if inherited_fd is not None else ()),
    )
    if inherited_fd is not None:
        os.close(inherited_fd)
    handler = ServerEventHandler()
    ctrl = DebugController(handler, profile=build_rust_profile(adapter=adapter))
    try:
        if readiness_fd is None:
            await _wait_stub_listening(process, port)
        else:
            ready_data = await asyncio.wait_for(
                asyncio.to_thread(os.read, readiness_fd, 64), WAIT
            )
            match = re.search(rb"\d+", ready_data)
            if match is None:
                raise RuntimeError(
                    f"lldb-server returned invalid readiness data: {ready_data!r}"
                )
            port = int(match.group())
        await ctrl.remote_attach(
            "127.0.0.1",
            port,
            path_mappings=[(str(Path(binary).parent), str(Path(binary).parent))],
            program=binary,
        )
        await asyncio.wait_for(handler.initialized_event.wait(), WAIT)
        await ctrl.do_configure()
        # lldb-dap resumes a stub-launched inferior after configuration and
        # does not emit an initial stopped event.  Exercise tdb's public pause
        # path so both native adapters reach the same inspectable state.
        assert await ctrl.pause(timeout=PAUSE_TIMEOUT)
        await ctrl.fetch_stop_info()
        return bool(ctrl.state.stack_frames)
    finally:
        if readiness_fd is not None:
            os.close(readiness_fd)
        try:
            await asyncio.wait_for(ctrl.stop(), WAIT)
        except BaseException:
            pass
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

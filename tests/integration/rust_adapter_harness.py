"""Shared real-process harness for Rust DAP integration tests."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import AsyncIterator, Awaitable, Callable

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


@dataclass(frozen=True)
class RustcRelease:
    major: int
    minor: int
    patch: int
    channel: str | None

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return f"{base}-{self.channel}" if self.channel else base


# Derived from the probes' constant so a version bump in src cannot
# silently disable this whole suite instead of failing loudly.
from tdb.rust_concurrency.probes.gdb import (  # noqa: E402
    SUPPORTED_RUST_VERSION as _SUPPORTED_RUST_VERSION_STR,
)

SUPPORTED_RUST_VERSION = RustcRelease(
    *(int(part) for part in _SUPPORTED_RUST_VERSION_STR.split(".")), None
)


@dataclass(frozen=True)
class RustDebugTarget:
    program: str
    scenario: str
    source_path: Path
    compiled_source_path: Path

    def arguments(
        self, ready_port: int | None = None, *, control: bool = False
    ) -> list[str]:
        arguments = [self.scenario]
        if ready_port is not None:
            arguments.append(str(ready_port))
            if control:
                arguments.append("control")
        return arguments


@dataclass(frozen=True)
class ReadyConnection:
    scenario: str
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter

    def acknowledge_wait_proof(self) -> None:
        self.writer.write(b"A")

    async def wait_ready(self) -> None:
        await self.writer.drain()
        marker = await asyncio.wait_for(self.reader.readexactly(1), WAIT)
        if marker != b"R":
            raise RuntimeError(f"fixture returned invalid ready marker: {marker!r}")


@dataclass(frozen=True)
class RunModeProbeResult:
    paused: bool
    adopted: bool
    resumed: bool
    terminated: bool
    episode_count: int


@dataclass(frozen=True)
class RemoteAttachEvidence:
    frame_names: tuple[str, ...]
    source_paths: tuple[str, ...]


def _version(command: list[str], pattern: str) -> tuple[int, ...] | None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError:
        return None
    match = re.search(pattern, result.stdout + result.stderr)
    return tuple(int(part) for part in match.groups()) if match else None


def _parse_rustc_release(text: str) -> RustcRelease | None:
    match = re.search(
        r"\brustc\s+(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.]+))?(?=\s|$)",
        text,
    )
    if match is None:
        return None
    major, minor, patch = (int(match.group(index)) for index in range(1, 4))
    return RustcRelease(major, minor, patch, match.group(4))


def rustc_version() -> RustcRelease | None:
    rustc = shutil.which("rustc")
    if rustc is None:
        return None
    try:
        result = subprocess.run(
            [rustc, "--version"], capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    return _parse_rustc_release(result.stdout + result.stderr)


def require_supported_rust_concurrency() -> None:
    found = rustc_version()
    if found != SUPPORTED_RUST_VERSION:
        rendered = str(found) if found is not None else "missing"
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


@pytest.fixture(scope="session", name="rust_debug_binary")
def _rust_debug_binary(
    tmp_path_factory,
) -> Callable[[str, str | None], RustDebugTarget]:
    rustc = shutil.which("rustc")
    if rustc is None:
        pytest.skip("rustc not installed")
    source = Path(__file__).parent / "fixtures" / "rust_concurrency.rs"
    build_dir = tmp_path_factory.mktemp("rust-concurrency")
    compiled_source = build_dir / "remote-source" / source.name
    compiled_source.parent.mkdir()
    shutil.copy2(source, compiled_source)
    binary = build_dir / "rust-concurrency-fixture"
    subprocess.run(
        [
            rustc,
            "-C",
            "debuginfo=2",
            "-C",
            "opt-level=0",
            "-o",
            str(binary),
            str(compiled_source),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    def build(case: str, adapter: str | None = None) -> RustDebugTarget:
        if case not in SCENARIOS:
            raise ValueError(f"unknown Rust concurrency scenario: {case}")
        return RustDebugTarget(str(binary), case, source, compiled_source)

    return build


@asynccontextmanager
async def _ready_listener() -> AsyncIterator[
    tuple[int, asyncio.Future[ReadyConnection]]
]:
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[ReadyConnection] = loop.create_future()
    connections: list[ReadyConnection] = []

    async def connected(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            payload = (await reader.read(128)).decode("utf-8", errors="replace")
            if not ready.done():
                connection = ReadyConnection(payload, reader, writer)
                connections.append(connection)
                ready.set_result(connection)
                return
        except BaseException:
            writer.close()
            await writer.wait_closed()
            raise
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(connected, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port, ready
    finally:
        server.close()
        for connection in connections:
            connection.writer.close()
            try:
                await asyncio.wait_for(connection.writer.wait_closed(), 2.0)
            except (asyncio.TimeoutError, ConnectionError):
                pass
        await server.wait_closed()


_REQUIRED_WAIT_COUNTS = {
    "join": {"join": 1},
    "mutex": {"mutex-lock": 1},
    "rwlock-read": {"rwlock-read": 1},
    "rwlock-write": {"rwlock-write": 1},
    "condvar": {"condvar-wait": 1},
    "mpsc-send": {"mpsc-send": 1},
    "mpsc-recv": {"mpsc-recv": 1},
    "park": {"park": 1},
    "cycle": {"mutex-lock": 2},
    "incomplete-cycle": {"mutex-lock": 1, "park": 1},
    "healthy-blocked": {"park": 1},
}


def _scenario_is_blocked(snapshot, scenario: str) -> bool:
    operations = [edge.operation for edge in snapshot.edges]
    return all(
        operations.count(operation) >= count
        for operation, count in _REQUIRED_WAIT_COUNTS[scenario].items()
    )


async def _pause_until_scenario_blocked(
    ctrl,
    handler,
    scenario: str,
    *,
    collect_snapshot: Callable[[object], Awaitable[object]] | None = None,
):
    """Leave the inferior stopped only after the debugger observes its wait."""
    if collect_snapshot is None:

        async def collect_snapshot(controller):
            return await InspectService(lambda: controller).collect_rust_concurrency()

    observed: list[tuple[str, ...]] = []
    for _ in range(20):
        if ctrl.state.phase is not SessionPhase.STOPPED:
            if not await ctrl.pause(timeout=PAUSE_TIMEOUT):
                await asyncio.sleep(0)
                continue
        snapshot = await collect_snapshot(ctrl)
        operations = tuple(edge.operation for edge in snapshot.edges)
        observed.append(operations)
        if _scenario_is_blocked(snapshot, scenario):
            return snapshot
        if getattr(ctrl.state, "is_terminated", False):
            break
        handler.reset_for_continue()
        await ctrl.continue_()
    raise AssertionError(
        f"fixture {scenario!r} never reached its required blocked state; "
        f"observed operations: {observed!r}"
    )


async def launch_and_pause(target: RustDebugTarget, adapter: str) -> DebugController:
    """Launch, configure, then prove and leave the fixture wait stopped."""
    handler = ServerEventHandler()
    ctrl = DebugController(handler, profile=build_rust_profile(adapter=adapter))
    try:
        async with _ready_listener() as (port, ready):
            await ctrl.start(
                program=target.program,
                args=target.arguments(port),
                stop_on_entry=False,
            )
            await asyncio.wait_for(handler.initialized_event.wait(), WAIT)
            await ctrl.do_configure()
            connection = await asyncio.wait_for(ready, WAIT)
            assert connection.scenario == target.scenario
            await _pause_until_scenario_blocked(
                ctrl,
                handler,
                target.scenario,
            )
            connection.acknowledge_wait_proof()
            handler.reset_for_continue()
            await ctrl.continue_()
            await connection.wait_ready()
            await _pause_until_scenario_blocked(ctrl, handler, target.scenario)
        await ctrl.fetch_stop_info()
        return ctrl
    except BaseException:
        try:
            await asyncio.wait_for(ctrl.stop(), WAIT)
        except BaseException:
            pass
        raise


async def run_mode_pause_probe(
    target: RustDebugTarget, adapter: str
) -> RunModeProbeResult:
    """Pause, adopt, resume, then terminate a run-mode fixture by command."""
    pause_results: list[bool] = []
    adopted = False
    verified_wait = False
    resumed = False
    episode_count = 0
    pause_task: asyncio.Task[None] | None = None
    retry_tasks: list[asyncio.Task[None]] = []
    terminate_task: asyncio.Task[None] | None = None
    controller_box: dict[str, DebugController] = {}
    connection_box: dict[str, ReadyConnection] = {}
    continued = asyncio.Event()

    async with _ready_listener() as (port, fixture_ready):

        def session_ready(ctrl: DebugController) -> None:
            nonlocal pause_task
            controller_box["controller"] = ctrl

            def on_continued(_event: Event) -> None:
                continued.set()

            ctrl.client.on_event("continued", on_continued)

            async def pause_when_fixture_ready() -> None:
                connection = await asyncio.wait_for(fixture_ready, WAIT)
                assert connection.scenario == target.scenario
                connection_box["connection"] = connection
                pause_results.append(await ctrl.pause(timeout=PAUSE_TIMEOUT))

            pause_task = asyncio.create_task(pause_when_fixture_ready())

        async def inspect_episode(controller, handler, console, config, program):
            nonlocal adopted, episode_count, resumed, terminate_task, verified_wait
            episode_count += 1
            adopted = adopted or controller.adopted_session
            snapshot = await InspectService(
                lambda: controller
            ).collect_rust_concurrency()
            verified_wait = verified_wait or (
                controller.state.phase is SessionPhase.STOPPED
                and _scenario_is_blocked(snapshot, target.scenario)
            )
            if not verified_wait:
                continued.clear()

                async def pause_after_resume() -> None:
                    await asyncio.wait_for(continued.wait(), WAIT)
                    pause_results.append(await controller.pause(timeout=PAUSE_TIMEOUT))

                retry_tasks.append(asyncio.create_task(pause_after_resume()))
                return True

            connection = connection_box["connection"]
            connection.acknowledge_wait_proof()

            async def terminate_after_resume() -> None:
                nonlocal resumed
                await connection.wait_ready()
                resumed = True
                connection.writer.write(b"X")
                await connection.writer.drain()

            terminate_task = asyncio.create_task(terminate_after_resume())
            return True

        code = await asyncio.wait_for(
            run_mode.run(
                program=target.program,
                args=target.arguments(port, control=True),
                profile=build_rust_profile(adapter=adapter),
                config=TdbConfig(),
                tui_episode=inspect_episode,
                on_session_ready=session_ready,
            ),
            WAIT * 3,
        )
        if pause_task is not None:
            await pause_task
        if retry_tasks:
            await asyncio.gather(*retry_tasks)
        if terminate_task is not None:
            await terminate_task
        controller = controller_box["controller"]
        return RunModeProbeResult(
            paused=bool(pause_results) and all(pause_results) and verified_wait,
            adopted=adopted,
            resumed=resumed,
            terminated=code == 0 and controller.state.is_terminated,
            episode_count=episode_count,
        )


async def terminal_launch_probe(target: RustDebugTarget) -> Request:
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
            program=target.program,
            args=target.arguments(),
            cwd=str(Path(target.program).parent),
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


def _gdbserver_command(stub: str) -> list[str]:
    """Request a kernel-selected port; stdout is the ownership handoff."""
    return [stub, "--once", "127.0.0.1:0"]


async def _wait_gdbserver_listening(process: asyncio.subprocess.Process) -> int:
    assert process.stdout is not None
    evidence: list[str] = []
    while True:
        line = await asyncio.wait_for(process.stdout.readline(), WAIT)
        if not line:
            pytest.skip(
                "gdbserver lacks a usable ephemeral-port handshake: "
                + "".join(evidence).strip()
            )
        text = line.decode("utf-8", errors="replace")
        evidence.append(text)
        match = re.search(r"listen(?:ing)?\s+(?:on\s+)?port\s+(\d+)", text, re.I)
        if match is not None:
            port = int(match.group(1))
            if port == 0:
                pytest.skip(
                    "gdbserver reported port 0 instead of an owned ephemeral port: "
                    + "".join(evidence).strip()
                )
            return port


async def remote_attach_probe(
    target: RustDebugTarget, adapter: str
) -> RemoteAttachEvidence:
    """Attach through the matching installed native remote stub."""
    readiness_fd: int | None = None
    inherited_fd: int | None = None
    if adapter == "gdb":
        stub = shutil.which("gdbserver")
        if stub is None:
            pytest.skip("gdbserver not installed")
        stub_command = _gdbserver_command(stub)
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
    async with _ready_listener() as (fixture_port, fixture_ready):
        process = await asyncio.create_subprocess_exec(
            *stub_command,
            target.program,
            *target.arguments(fixture_port),
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
                port = await _wait_gdbserver_listening(process)
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
                path_mappings=[
                    (
                        str(target.source_path.parent),
                        str(target.compiled_source_path.parent),
                    )
                ],
                program=target.program,
            )
            await asyncio.wait_for(handler.initialized_event.wait(), WAIT)
            await ctrl.do_configure()
            connection = await asyncio.wait_for(fixture_ready, WAIT)
            assert connection.scenario == target.scenario
            await _pause_until_scenario_blocked(ctrl, handler, target.scenario)
            connection.acknowledge_wait_proof()
            handler.reset_for_continue()
            await ctrl.continue_()
            await connection.wait_ready()
            await _pause_until_scenario_blocked(ctrl, handler, target.scenario)
            threads = await ctrl.client.threads()
            frames = [
                frame
                for thread_info in threads
                for frame in await ctrl.client.stack_trace(
                    thread_info.id, start_frame=0, levels=64
                )
            ]
            return RemoteAttachEvidence(
                frame_names=tuple(frame.name for frame in frames),
                source_paths=tuple(
                    frame.source.path
                    for frame in frames
                    if frame.source is not None and frame.source.path is not None
                ),
            )
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

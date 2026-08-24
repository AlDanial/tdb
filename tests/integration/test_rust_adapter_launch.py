"""Real local launches through every Rust DAP adapter available here."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tdb.session.state import SessionPhase
from tests.integration import rust_adapter_harness
from tests.integration.rust_adapter_harness import (
    RustcRelease,
    _gdbserver_command,
    _parse_rustc_release,
    _pause_until_scenario_blocked,
    _ready_listener,
    available_rust_adapters,
    launch_and_pause,
    require_supported_rust_concurrency,
    _rust_debug_binary,  # noqa: F401 - registers rust_debug_binary fixture
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("rustc 1.98.0 (abc 2026-01-01)", RustcRelease(1, 98, 0, None)),
        ("rustc 1.98.0-nightly (abc 2026-01-01)", RustcRelease(1, 98, 0, "nightly")),
        ("rustc 1.98.0-beta.2 (abc 2026-01-01)", RustcRelease(1, 98, 0, "beta.2")),
    ],
)
def test_rustc_release_parser_retains_prerelease_channel(text, expected):
    assert _parse_rustc_release(text) == expected


@pytest.mark.parametrize("channel", ["nightly", "beta.2", "dev"])
def test_layout_gate_rejects_rustc_1_98_prereleases(monkeypatch, channel):
    monkeypatch.setattr(
        rust_adapter_harness,
        "rustc_version",
        lambda: RustcRelease(1, 98, 0, channel),
    )

    with pytest.raises(pytest.skip.Exception, match=rf"1\.98\.0-{channel}"):
        require_supported_rust_concurrency()


def test_gdbserver_uses_owned_ephemeral_port_handshake():
    assert _gdbserver_command("/usr/bin/gdbserver") == [
        "/usr/bin/gdbserver",
        "--once",
        "127.0.0.1:0",
    ]


def test_fixture_builder_reuses_one_runtime_selected_binary(rust_debug_binary):
    park = rust_debug_binary("park", "gdb")
    mutex = rust_debug_binary("mutex", "lldb-dap")

    assert park.program == mutex.program
    assert park.scenario == "park"
    assert mutex.scenario == "mutex"
    assert park.compiled_source_path != park.source_path


async def test_fixture_announces_ready_only_after_wait_proof_ack(rust_debug_binary):
    target = rust_debug_binary("park")
    process = None

    async with _ready_listener() as (port, probe):
        process = await asyncio.create_subprocess_exec(
            target.program,
            *target.arguments(port),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None
        stdout_ready = asyncio.create_task(process.stdout.readline())
        probe_ready = asyncio.ensure_future(probe)
        done, _pending = await asyncio.wait(
            {stdout_ready, probe_ready},
            timeout=10.0,
            return_when=asyncio.FIRST_COMPLETED,
        )

        assert probe_ready in done
        assert stdout_ready not in done
        connection = probe_ready.result()
        connection.acknowledge_wait_proof()
        await connection.wait_ready()
        assert await asyncio.wait_for(stdout_ready, 10.0) == b"READY:park\n"

    if process is not None and process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 5.0)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


async def test_blocked_readiness_retries_a_prewait_stop():
    class Controller:
        def __init__(self):
            self.pause_count = 0
            self.continue_count = 0
            self.state = SimpleNamespace(phase=SessionPhase.RUNNING)

        async def pause(self, timeout):
            self.pause_count += 1
            self.state.phase = SessionPhase.STOPPED
            return True

        async def continue_(self):
            self.continue_count += 1
            self.state.phase = SessionPhase.RUNNING

    class Handler:
        def reset_for_continue(self):
            pass

    snapshots = iter(
        [
            SimpleNamespace(edges=()),
            SimpleNamespace(edges=(SimpleNamespace(operation="park"),)),
        ]
    )

    async def collect(_controller):
        return next(snapshots)

    controller = Controller()
    snapshot = await _pause_until_scenario_blocked(
        controller,
        Handler(),
        "park",
        collect_snapshot=collect,
    )

    assert [edge.operation for edge in snapshot.edges] == ["park"]
    assert controller.pause_count == 2
    assert controller.continue_count == 1


@pytest.mark.parametrize("adapter", available_rust_adapters())
async def test_local_launch_reaches_a_real_stopped_rust_stack(
    adapter, rust_debug_binary
):
    target = rust_debug_binary("park", adapter)
    ctrl = await launch_and_pause(target, adapter)
    try:
        assert ctrl.profile.id == "rust"
        assert ctrl.profile.adapter.id == adapter
        assert ctrl.state.phase is SessionPhase.STOPPED
        assert ctrl.state.stack_frames
    finally:
        await ctrl.stop()

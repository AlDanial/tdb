from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import shutil
import signal
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from tdb.adapters.tcsh.inspect import (
    InspectionError,
    StaleReferenceError,
    UnknownFrameError,
    UnknownReferenceError,
)
from tdb.adapters.tcsh.runtime import (
    render_inspection_request as render_runtime_inspection_request,
)
from tdb.adapters.tcsh.session import (
    DebugSession,
    EvaluationError,
    InvalidStateError,
    LaunchConfig,
    LaunchError,
    RunMode,
    SessionEvent,
    SessionState,
    StopReason,
    should_stop_at_probe,
)
from tdb.adapters.tcsh.transport import (
    IncompleteResponseError,
    ProbeEvent,
    TransportError,
)

EventSink = Callable[[SessionEvent], Awaitable[None]]


def launch_config(
    program: Path,
    tcsh_path: Path,
    *,
    args: tuple[str, ...] = (),
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    stop_on_entry: bool = False,
    external_terminal: bool = False,
) -> LaunchConfig:
    return LaunchConfig(
        program=program,
        args=args,
        cwd=cwd or program.parent,
        env=env or {},
        tcsh_path=tcsh_path,
        stop_on_entry=stop_on_entry,
        external_terminal=external_terminal,
    )


def collecting_sink(events: list[SessionEvent]) -> EventSink:
    async def collect(event: SessionEvent) -> None:
        events.append(event)

    return collect


async def wait_for_event(
    events: list[SessionEvent],
    kind: str,
    *,
    occurrence: int = 1,
) -> SessionEvent:
    async with asyncio.timeout(2.0):
        while True:
            matching = [event for event in events if event.kind == kind]
            if len(matching) >= occurrence:
                return matching[occurrence - 1]
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_prepare_rejects_missing_program(tmp_path: Path) -> None:
    events: list[SessionEvent] = []
    missing = tmp_path / "missing.csh"
    session = DebugSession(
        launch_config(missing, Path("/bin/sh")),
        collecting_sink(events),
    )

    with pytest.raises(LaunchError, match="Program does not exist") as raised:
        await session.prepare()

    assert str(missing) in str(raised.value)
    assert session.state is SessionState.NEW
    assert events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "message"),
    [("cwd", "Working directory does not exist"), ("tcsh", "tcsh is not executable")],
)
async def test_prepare_validates_launch_paths(
    tmp_path: Path,
    basic_program: Path,
    field: str,
    message: str,
) -> None:
    tcsh_path = tmp_path / "tcsh"
    tcsh_path.write_text("not executable")
    cwd = tmp_path / "missing-cwd" if field == "cwd" else tmp_path
    if field == "cwd":
        tcsh_path = Path("/bin/sh")
    session = DebugSession(
        launch_config(basic_program, tcsh_path, cwd=cwd),
        collecting_sink([]),
    )

    with pytest.raises(LaunchError, match=message):
        await session.prepare()

    assert session.state is SessionState.NEW


@pytest.mark.asyncio
async def test_prepare_builds_private_runtime_and_wires_active_paths(
    tmp_path: Path,
    basic_program: Path,
    recording_tcsh: Path,
) -> None:
    sourced = tmp_path / "sourced.csh"
    sourced.write_text("echo sourced\n")
    basic_program.write_text("source sourced.csh\n")
    session = DebugSession(
        launch_config(basic_program, recording_tcsh),
        collecting_sink([]),
    )
    await session.prepare()
    assert session.workspace is not None
    assert session.transport is not None
    assert session.instrumentation is not None

    try:
        assert session.state is SessionState.CONFIGURED
        assert stat.S_IMODE(session.workspace.stat().st_mode) == 0o700
        probe = session.instrumentation.root.parent.parent / "probes" / "1.csh"
        rendered = probe.read_text()
        assert str(session.transport.paths.event_fifo) in rendered
        assert str(session.transport.paths.control_fifo) in rendered
        generated_root = session.instrumentation.root.read_text()
        assert generated_root.count(str(session.transport.paths.event_fifo)) == 2
    finally:
        await session.terminate()

    assert session.state is SessionState.TERMINATED
    assert not session.workspace.exists()


@pytest.mark.asyncio
async def test_start_owns_process_boundary_and_pumps_output(
    tmp_path: Path,
    basic_program: Path,
    recording_tcsh: Path,
) -> None:
    events: list[SessionEvent] = []
    record_path = tmp_path / "record.json"
    session = DebugSession(
        launch_config(
            basic_program,
            recording_tcsh,
            args=("first", "second"),
            cwd=tmp_path,
            env={
                "TCSH_DAP_TEST_RECORD": str(record_path),
                "TCSH_DAP_TEST_OVERLAY": "configured",
                "TCSH_DAP_TEST_EXIT": "7",
                "__tcsh_dap_original_0": "must-be-replaced",
            },
        ),
        collecting_sink(events),
    )
    await session.prepare()
    assert session.instrumentation is not None
    generated_root = session.instrumentation.root

    await session.start()
    await asyncio.wait_for(session.wait(), timeout=2.0)

    record = json.loads(record_path.read_text())
    assert record["argv"] == [
        str(recording_tcsh),
        "-f",
        str(generated_root),
        "first",
        "second",
    ]
    assert record["cwd"] == str(tmp_path.resolve())
    assert record["original_0"] == str(basic_program.resolve())
    assert record["overlay"] == "configured"
    assert record["stdin"] == ""
    assert session.process is not None
    assert record["process_group"] == record["pid"]
    assert record["process_group"] != session.process.pid
    stdout = [
        event.body
        for event in events
        if event.kind == "output" and event.body["category"] == "stdout"
    ]
    stderr = [
        event.body
        for event in events
        if event.kind == "output" and event.body["category"] == "stderr"
    ]
    assert stdout == [
        {"category": "stdout", "output": "stdout line\n"},
        {"category": "stdout", "output": "stdout partial�"},
    ]
    assert stderr == [{"category": "stderr", "output": "stderr line\n"}]
    assert [(event.kind, event.body) for event in events[-2:]] == [
        ("exited", {"exitCode": 7}),
        ("terminated", {}),
    ]
    assert session.state is SessionState.TERMINATED
    assert session.workspace is not None
    assert not session.workspace.exists()


@pytest.mark.asyncio
async def test_terminate_configured_session_is_idempotent_without_exited(
    basic_program: Path,
    recording_tcsh: Path,
) -> None:
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(basic_program, recording_tcsh),
        collecting_sink(events),
    )
    await session.prepare()
    assert session.workspace is not None

    await session.terminate()
    await session.terminate()
    await session.wait()

    assert events == [SessionEvent("terminated", {})]
    assert session.state is SessionState.TERMINATED
    assert not session.workspace.exists()


@pytest.mark.asyncio
async def test_detach_configured_session_cleans_without_process_or_exit_event(
    basic_program: Path,
    recording_tcsh: Path,
) -> None:
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(basic_program, recording_tcsh),
        collecting_sink(events),
    )
    await session.prepare()
    assert session.workspace is not None
    workspace = session.workspace

    await session.detach()
    await session.detach()
    await session.wait()

    assert session.state is SessionState.TERMINATED
    assert session.process is None
    assert events == [SessionEvent("terminated", {})]
    assert not workspace.exists()


@pytest.mark.asyncio
async def test_configured_detach_cleanup_failure_is_recorded_after_workspace_removal(
    basic_program: Path,
    recording_tcsh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(basic_program, recording_tcsh),
        collecting_sink(events),
    )
    await session.prepare()
    assert session.transport is not None
    assert session.workspace is not None
    workspace = session.workspace
    original_close = session.transport.close

    async def close_then_fail() -> None:
        await original_close()
        raise RuntimeError("detach close failed")

    monkeypatch.setattr(session.transport, "close", close_then_fail)

    with pytest.raises(RuntimeError, match="detach close failed") as raised:
        await session.detach()

    assert session.failure is raised.value
    assert session.state is SessionState.TERMINATED
    assert not workspace.exists()
    await session.detach()
    with pytest.raises(RuntimeError, match="detach close failed") as waited:
        await session.wait()
    assert waited.value is raised.value
    assert events == [SessionEvent("terminated", {})]


@pytest.mark.asyncio
async def test_configured_detach_cancellation_finishes_terminal_cleanup(
    basic_program: Path,
    recording_tcsh: Path,
) -> None:
    terminal_started = asyncio.Event()
    release_terminal = asyncio.Event()

    async def blocking_sink(event: SessionEvent) -> None:
        assert event.kind == "terminated"
        terminal_started.set()
        await release_terminal.wait()

    session = DebugSession(
        launch_config(basic_program, recording_tcsh),
        blocking_sink,
    )
    await session.prepare()
    assert session.workspace is not None
    workspace = session.workspace
    task = asyncio.create_task(session.detach())
    await asyncio.wait_for(terminal_started.wait(), timeout=0.1)

    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release_terminal.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert session.state is SessionState.TERMINATED
    assert not workspace.exists()


@pytest.mark.asyncio
async def test_configured_close_failure_is_recorded_and_propagated_after_cleanup(
    basic_program: Path,
    recording_tcsh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(basic_program, recording_tcsh),
        collecting_sink(events),
    )
    await session.prepare()
    assert session.transport is not None
    assert session.workspace is not None
    workspace = session.workspace
    original_close = session.transport.close

    async def close_then_fail() -> None:
        await original_close()
        raise RuntimeError("close failed")

    monkeypatch.setattr(session.transport, "close", close_then_fail)

    with pytest.raises(RuntimeError, match="close failed") as raised:
        await session.terminate()

    assert session.failure is raised.value
    assert not workspace.exists()
    await session.terminate()
    await session.terminate()
    for _ in range(2):
        with pytest.raises(RuntimeError, match="close failed") as repeated:
            await session.wait()
        assert repeated.value is raised.value
    assert events == [SessionEvent("terminated", {})]


@pytest.mark.asyncio
async def test_terminate_waits_for_terminal_sink_and_cleanup_after_state_changes(
    tmp_path: Path,
    basic_program: Path,
    recording_tcsh: Path,
) -> None:
    events: list[SessionEvent] = []
    terminated_started = asyncio.Event()
    release_terminated = asyncio.Event()

    async def blocking_sink(event: SessionEvent) -> None:
        events.append(event)
        if event.kind == "terminated":
            terminated_started.set()
            await release_terminated.wait()

    session = DebugSession(
        launch_config(
            basic_program,
            recording_tcsh,
            env={"TCSH_DAP_TEST_RECORD": str(tmp_path / "record.json")},
        ),
        blocking_sink,
    )
    await session.prepare()
    assert session.workspace is not None
    workspace = session.workspace
    await session.start()
    await asyncio.wait_for(terminated_started.wait(), timeout=1.0)
    assert session.state is SessionState.TERMINATED
    assert workspace.exists()

    terminate_task = asyncio.create_task(session.terminate())
    wait_task = asyncio.create_task(session.wait())
    try:
        await asyncio.sleep(0)
        assert not terminate_task.done()
        assert not wait_task.done()
    finally:
        release_terminated.set()
        await asyncio.wait_for(asyncio.gather(terminate_task, wait_task), timeout=1.0)

    assert [event.kind for event in events[-2:]] == ["exited", "terminated"]
    assert not workspace.exists()


@pytest.mark.asyncio
async def test_start_rejects_repeated_lifecycle_calls(
    basic_program: Path,
    recording_tcsh: Path,
) -> None:
    session = DebugSession(
        launch_config(basic_program, recording_tcsh),
        collecting_sink([]),
    )
    await session.prepare()
    with pytest.raises(LaunchError, match="prepare requires NEW state"):
        await session.prepare()
    await session.start()
    with pytest.raises(LaunchError, match="start requires CONFIGURED state"):
        await session.start()
    await asyncio.wait_for(session.wait(), timeout=2.0)


@pytest.mark.asyncio
async def test_output_emissions_are_serialized(
    tmp_path: Path,
    basic_program: Path,
    recording_tcsh: Path,
) -> None:
    record_path = tmp_path / "record.json"
    active = False
    concurrent_entry = False

    async def guarded_sink(event: SessionEvent) -> None:
        nonlocal active, concurrent_entry
        if active:
            concurrent_entry = True
        active = True
        await asyncio.sleep(0.01)
        active = False

    session = DebugSession(
        launch_config(
            basic_program,
            recording_tcsh,
            env={"TCSH_DAP_TEST_RECORD": str(record_path)},
        ),
        guarded_sink,
    )
    await session.prepare()
    await session.start()
    await asyncio.wait_for(session.wait(), timeout=2.0)

    assert concurrent_entry is False


@pytest.mark.asyncio
async def test_output_pump_accepts_lines_larger_than_stream_reader_limit(
    tmp_path: Path,
    basic_program: Path,
) -> None:
    executable = tmp_path / "large-output-tcsh"
    executable.write_text(
        "#!/usr/bin/env python3\nimport os\nos.write(1, b'x' * 70000 + b'\\npartial')\n"
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(basic_program, executable),
        collecting_sink(events),
    )
    await session.prepare()
    await session.start()
    await asyncio.wait_for(session.wait(), timeout=2.0)

    output = [str(event.body["output"]) for event in events if event.kind == "output"]
    assert "".join(output) == "x" * 70000 + "\npartial"
    assert all(len(item.encode()) <= 64 * 1024 for item in output)
    assert output[-1] == "partial"


@pytest.mark.asyncio
async def test_output_pump_emits_bounded_chunks_before_unterminated_stream_eof(
    basic_program: Path,
    recording_tcsh: Path,
) -> None:
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(basic_program, recording_tcsh),
        collecting_sink(events),
    )
    stream = asyncio.StreamReader()
    pump = asyncio.create_task(session._pump_output(stream, "stdout"))
    payload = b"x" * (2 * 64 * 1024)
    try:
        stream.feed_data(payload)
        async with asyncio.timeout(0.1):
            while len(events) < 2:
                await asyncio.sleep(0)

        assert not pump.done()
        output = [str(event.body["output"]) for event in events]
        assert b"".join(item.encode() for item in output) == payload
        assert all(len(item.encode()) <= 64 * 1024 for item in output)
    finally:
        stream.feed_eof()
        await asyncio.wait_for(pump, timeout=1)


@pytest.mark.asyncio
async def test_chunked_output_preserves_utf8_and_redacts_workspace_across_boundaries(
    basic_program: Path,
    recording_tcsh: Path,
) -> None:
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(basic_program, recording_tcsh),
        collecting_sink(events),
    )
    session.workspace = Path("/private/workspace-boundary")
    workspace = str(session.workspace).encode()

    class ChunkedStream:
        def __init__(self) -> None:
            self.chunks = [
                b"x" * (64 * 1024 - 3) + workspace[:3],
                workspace[3:] + b"\xe2",
                b"\x82\xac",
                b"",
            ]

        async def read(self, limit: int) -> bytes:
            assert limit == 64 * 1024
            return self.chunks.pop(0)

    await session._pump_output(ChunkedStream(), "stdout")  # type: ignore[arg-type]

    output = "".join(
        str(event.body["output"]) for event in events if event.kind == "output"
    )
    assert output == "x" * (64 * 1024 - 3) + "<adapter-workspace>€"
    assert str(session.workspace) not in output
    assert "�" not in output


@pytest.mark.asyncio
async def test_start_failure_cleans_workspace_without_exit_events(
    tmp_path: Path,
    basic_program: Path,
) -> None:
    invalid_tcsh = tmp_path / "invalid-tcsh"
    invalid_tcsh.write_text("#!/missing/interpreter\n")
    invalid_tcsh.chmod(invalid_tcsh.stat().st_mode | stat.S_IXUSR)
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(basic_program, invalid_tcsh),
        collecting_sink(events),
    )
    await session.prepare()
    assert session.workspace is not None
    workspace = session.workspace

    with pytest.raises(LaunchError, match=str(basic_program.resolve())):
        await session.start()

    assert session.state is SessionState.TERMINATED
    assert not workspace.exists()
    assert events == []


@pytest.mark.asyncio
async def test_second_guardian_pipe_failure_closes_partial_setup_and_workspace(
    basic_program: Path,
    recording_tcsh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = DebugSession(
        launch_config(basic_program, recording_tcsh),
        collecting_sink([]),
    )
    await session.prepare()
    assert session.workspace is not None
    workspace = session.workspace
    real_pipe = os.pipe
    descriptors: list[int] = []
    calls = 0

    def fail_second_pipe() -> tuple[int, int]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("second guardian pipe failed")
        pair = real_pipe()
        descriptors.extend(pair)
        return pair

    monkeypatch.setattr(os, "pipe", fail_second_pipe)
    try:
        with pytest.raises(LaunchError, match="second guardian pipe failed"):
            await session.start()

        assert session.state is SessionState.TERMINATED
        assert not workspace.exists()
        assert session._guardian_status_descriptor is None
        assert session._guardian_control_descriptor is None
        for descriptor in descriptors:
            with pytest.raises(OSError):
                os.fstat(descriptor)
    finally:
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if workspace.exists():
            await session._cleanup()


@pytest.mark.asyncio
async def test_guardian_set_blocking_failure_closes_all_pipes_and_workspace(
    basic_program: Path,
    recording_tcsh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = DebugSession(
        launch_config(basic_program, recording_tcsh),
        collecting_sink([]),
    )
    await session.prepare()
    assert session.workspace is not None
    workspace = session.workspace
    real_pipe = os.pipe
    descriptors: list[int] = []

    def tracked_pipe() -> tuple[int, int]:
        pair = real_pipe()
        descriptors.extend(pair)
        return pair

    monkeypatch.setattr(os, "pipe", tracked_pipe)
    monkeypatch.setattr(
        os,
        "set_blocking",
        lambda descriptor, blocking: (_ for _ in ()).throw(
            OSError("guardian set_blocking failed")
        ),
    )
    try:
        with pytest.raises(LaunchError, match="guardian set_blocking failed"):
            await session.start()

        assert session.state is SessionState.TERMINATED
        assert not workspace.exists()
        assert session._guardian_status_descriptor is None
        assert session._guardian_control_descriptor is None
        for descriptor in descriptors:
            with pytest.raises(OSError):
                os.fstat(descriptor)
    finally:
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if workspace.exists():
            await session._cleanup()


@pytest.mark.asyncio
async def test_terminate_signals_process_group_then_escalates(
    tmp_path: Path,
    basic_program: Path,
) -> None:
    ready = tmp_path / "ready"
    child_ready = tmp_path / "child-ready"
    child_terminated = tmp_path / "child-terminated"
    executable = tmp_path / "stubborn-tcsh"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import signal\n"
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "child_code = '''\n"
        "import signal\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "def stop(*_args):\n"
        "    Path(sys.argv[1]).write_text('terminated')\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "Path(sys.argv[2]).write_text('ready')\n"
        "while True:\n"
        "    time.sleep(1)\n"
        "'''\n"
        "child = subprocess.Popen([\n"
        "    sys.executable, '-c', child_code,\n"
        "    os.environ['CHILD_TERMINATED'], os.environ['CHILD_READY'],\n"
        "])\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "while not Path(os.environ['CHILD_READY']).exists():\n"
        "    time.sleep(0.01)\n"
        "Path(os.environ['READY']).write_text('ready')\n"
        "while True:\n"
        "    child.poll()\n"
        "    time.sleep(1)\n"
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(
            basic_program,
            executable,
            env={
                "READY": str(ready),
                "CHILD_READY": str(child_ready),
                "CHILD_TERMINATED": str(child_terminated),
            },
        ),
        collecting_sink(events),
    )
    await session.prepare()
    await session.start()
    assert session.process is not None
    try:
        async with asyncio.timeout(1.0):
            while not ready.exists():
                await asyncio.sleep(0.01)
        try:
            await asyncio.wait_for(session.terminate(), timeout=3.0)
        except BaseException as error:
            error.add_note(f"captured session events: {events!r}")
            raise
    finally:
        if session.process.returncode is None:
            os.killpg(session.process.pid, signal.SIGKILL)
            await session.process.wait()

    assert child_terminated.read_text() == "terminated"
    assert session.process.returncode == -signal.SIGKILL
    assert [event.kind for event in events[-2:]] == ["exited", "terminated"]
    assert session.workspace is not None
    assert not session.workspace.exists()


@pytest.mark.asyncio
async def test_terminate_signals_descendant_in_separate_process_group(
    tmp_path: Path,
    basic_program: Path,
) -> None:
    child_pid_file = tmp_path / "child-pid"
    child_ready = tmp_path / "child-ready"
    child_terminated = tmp_path / "child-terminated"
    executable = tmp_path / "job-control-tcsh"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import signal\n"
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "child_code = '''\n"
        "import signal\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "def stop(*_args):\n"
        "    Path(sys.argv[1]).write_text('terminated')\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "Path(sys.argv[2]).write_text('ready')\n"
        "while True:\n"
        "    time.sleep(1)\n"
        "'''\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', child_code, os.environ['CHILD_TERMINATED'], os.environ['CHILD_READY']],\n"
        "    preexec_fn=os.setpgrp,\n"
        ")\n"
        "Path(os.environ['CHILD_PID']).write_text(str(child.pid))\n"
        "while True:\n"
        "    time.sleep(1)\n"
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    session = DebugSession(
        launch_config(
            basic_program,
            executable,
            env={
                "CHILD_PID": str(child_pid_file),
                "CHILD_READY": str(child_ready),
                "CHILD_TERMINATED": str(child_terminated),
            },
        ),
        collecting_sink([]),
    )
    await session.prepare()
    await session.start()
    async with asyncio.timeout(1.0):
        while not child_pid_file.exists() or not child_ready.exists():
            await asyncio.sleep(0.01)
    child_pid = int(child_pid_file.read_text())
    try:
        await asyncio.wait_for(session.terminate(), timeout=3.0)
    finally:
        try:
            os.killpg(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(session.wait(), timeout=1.0)
        except (RuntimeError, TimeoutError):
            pass

    assert child_terminated.read_text() == "terminated"


def test_owned_process_groups_use_live_original_session_members(
    tmp_path: Path,
    recording_tcsh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program = tmp_path / "program.csh"
    program.write_text("echo ready\n")
    session = DebugSession(launch_config(program, recording_tcsh), collecting_sink([]))

    class LiveGuardian:
        pid = 101
        returncode = None

    session.process = LiveGuardian()  # type: ignore[assignment]
    session._process_group_id = 101
    session._process_session_id = 101  # type: ignore[attr-defined]
    calls: list[str] = []

    def snapshot() -> str:
        calls.append("snapshot")
        return "101 101\n202 202\n303 404\n505 606 S\ninvalid row here\n"

    monkeypatch.setattr("tdb.adapters.tcsh.session.process_table_snapshot", snapshot)
    monkeypatch.setattr(os, "waitid", lambda idtype, pid, options: None)
    monkeypatch.setattr(
        os,
        "getsid",
        lambda pid: {101: 101, 202: 101, 303: 101, 505: 505}[pid],
    )
    monkeypatch.setattr(os, "getpgid", lambda pid: 101)

    assert session._owned_process_group_ids() == {101, 202, 404}
    assert calls == ["snapshot"]


def test_darwin_shaped_snapshot_uses_kernel_session_ids_per_pid(
    tmp_path: Path,
    recording_tcsh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program = tmp_path / "program.csh"
    program.write_text("echo ready\n")
    session = DebugSession(launch_config(program, recording_tcsh), collecting_sink([]))

    class LiveGuardian:
        pid = 101
        returncode = None

    session.process = LiveGuardian()  # type: ignore[assignment]
    session._process_group_id = 101
    session._process_session_id = 101
    calls: list[str] = []

    def snapshot() -> str:
        calls.append("snapshot")
        return "101 101\n202 202\n303 404\n505 606\ninvalid row here\n"

    def process_session(pid: int) -> int:
        return {101: 101, 202: 101, 303: 101, 505: 505}[pid]

    waitid_calls: list[tuple[int, int, int]] = []

    def live_child(idtype: int, pid: int, options: int) -> None:
        waitid_calls.append((idtype, pid, options))

    monkeypatch.setattr("tdb.adapters.tcsh.session.process_table_snapshot", snapshot)
    monkeypatch.setattr(os, "getsid", process_session)
    monkeypatch.setattr(os, "getpgid", lambda pid: 101)
    monkeypatch.setattr(os, "waitid", live_child)

    assert session._owned_process_group_ids() == {101, 202, 404}
    assert calls == ["snapshot"]
    expected_options = os.WEXITED | os.WNOHANG | os.WNOWAIT
    assert waitid_calls == [
        (os.P_PID, 101, expected_options),
        (os.P_PID, 101, expected_options),
    ]


def test_reaped_root_pid_reused_as_same_sid_rejects_entire_snapshot(
    tmp_path: Path,
    recording_tcsh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program = tmp_path / "program.csh"
    program.write_text("echo ready\n")
    session = DebugSession(launch_config(program, recording_tcsh), collecting_sink([]))

    class ReapedProcess:
        pid = 101
        returncode = 0

    session.process = ReapedProcess()  # type: ignore[assignment]
    session._process_group_id = 101
    session._process_session_id = 101
    snapshots: list[str] = []
    monkeypatch.setattr(os, "getsid", lambda pid: 101)

    def snapshot() -> str:
        snapshots.append("snapshot")
        return "101 700\n"

    monkeypatch.setattr("tdb.adapters.tcsh.session.process_table_snapshot", snapshot)

    groups = session._owned_process_group_ids()
    session._signal_process_groups(groups, signal.SIGTERM)

    assert groups == set()
    assert snapshots == []


def test_reaped_owner_rejects_same_sid_members_after_replacement_leader_exits(
    tmp_path: Path,
    recording_tcsh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program = tmp_path / "program.csh"
    program.write_text("echo ready\n")
    session = DebugSession(launch_config(program, recording_tcsh), collecting_sink([]))

    class ReapedGuardian:
        pid = 101
        returncode = None

    session.process = ReapedGuardian()  # type: ignore[assignment]
    session._process_group_id = 101
    session._process_session_id = 101

    def reaped_child(idtype: int, pid: int, options: int) -> None:
        raise ChildProcessError

    monkeypatch.setattr(os, "waitid", reaped_child)
    monkeypatch.setattr(os, "getsid", lambda pid: 101)
    monkeypatch.setattr(
        "tdb.adapters.tcsh.session.process_table_snapshot",
        lambda: "202 202\n",
    )

    groups = session._owned_process_group_ids()

    assert groups == set()


def test_reaped_root_replaced_during_snapshot_rejects_same_identity_members(
    tmp_path: Path,
    recording_tcsh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program = tmp_path / "program.csh"
    program.write_text("echo ready\n")
    session = DebugSession(launch_config(program, recording_tcsh), collecting_sink([]))

    class ProcessWithStaleReturncode:
        pid = 101
        returncode = None

    session.process = ProcessWithStaleReturncode()  # type: ignore[assignment]
    session._process_group_id = 101
    session._process_session_id = 101
    root_replaced = False
    waitid_calls: list[tuple[int, int, int]] = []

    def child_generation(idtype: int, pid: int, options: int) -> None:
        waitid_calls.append((idtype, pid, options))
        if root_replaced:
            raise ChildProcessError

    def snapshot() -> str:
        nonlocal root_replaced
        root_replaced = True
        return "101 101\n202 202\n"

    monkeypatch.setattr(os, "waitid", child_generation)
    monkeypatch.setattr(os, "getsid", lambda pid: 101)
    monkeypatch.setattr(os, "getpgid", lambda pid: 101)
    monkeypatch.setattr("tdb.adapters.tcsh.session.process_table_snapshot", snapshot)
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: signals.append((pgid, sig)))

    groups = session._owned_process_group_ids()
    session._signal_process_groups(groups, signal.SIGTERM)

    expected_options = os.WEXITED | os.WNOHANG | os.WNOWAIT
    assert groups == set()
    assert signals == []
    assert waitid_calls == [
        (os.P_PID, 101, expected_options),
        (os.P_PID, 101, expected_options),
    ]


def test_exited_guardian_generation_is_rejected_without_reaping(
    tmp_path: Path,
    recording_tcsh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program = tmp_path / "program.csh"
    program.write_text("echo ready\n")
    session = DebugSession(launch_config(program, recording_tcsh), collecting_sink([]))

    class ProcessWithPendingWatcherUpdate:
        pid = 101
        returncode = None

    session.process = ProcessWithPendingWatcherUpdate()  # type: ignore[assignment]
    session._process_group_id = 101
    session._process_session_id = 101
    waitid_calls: list[tuple[int, int, int]] = []

    def zombie_child(idtype: int, pid: int, options: int) -> object:
        waitid_calls.append((idtype, pid, options))
        return object()

    snapshots: list[str] = []

    def snapshot() -> str:
        snapshots.append("snapshot")
        return "101 101\n202 202\n"

    monkeypatch.setattr(os, "waitid", zombie_child)
    monkeypatch.setattr(os, "getsid", lambda pid: 101)
    monkeypatch.setattr(os, "getpgid", lambda pid: 101)
    monkeypatch.setattr("tdb.adapters.tcsh.session.process_table_snapshot", snapshot)

    assert session._owned_process_group_ids() == set()
    expected_options = os.WEXITED | os.WNOHANG | os.WNOWAIT
    assert waitid_calls == [
        (os.P_PID, 101, expected_options),
    ]
    assert snapshots == []


def test_waitid_unavailable_fails_closed_without_snapshotting_members(
    tmp_path: Path,
    recording_tcsh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program = tmp_path / "program.csh"
    program.write_text("echo ready\n")
    session = DebugSession(launch_config(program, recording_tcsh), collecting_sink([]))

    class LiveProcess:
        pid = 101
        returncode = None

    session.process = LiveProcess()  # type: ignore[assignment]
    session._process_group_id = 101
    session._process_session_id = 101
    snapshots: list[str] = []

    def snapshot() -> str:
        snapshots.append("snapshot")
        return "101 101\n202 202\n"

    monkeypatch.delattr(os, "waitid")
    monkeypatch.setattr(os, "getsid", lambda pid: 101)
    monkeypatch.setattr(os, "getpgid", lambda pid: 101)
    monkeypatch.setattr("tdb.adapters.tcsh.session.process_table_snapshot", snapshot)

    assert session._owned_process_group_ids() == set()
    assert snapshots == []


def test_stale_root_pid_snapshot_does_not_return_or_signal_reused_group(
    tmp_path: Path,
    recording_tcsh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program = tmp_path / "program.csh"
    program.write_text("echo ready\n")
    session = DebugSession(launch_config(program, recording_tcsh), collecting_sink([]))

    class FinishedProcess:
        pid = 101
        returncode = 0

    session.process = FinishedProcess()  # type: ignore[assignment]
    session._process_group_id = 101
    session._process_session_id = 101  # type: ignore[attr-defined]

    monkeypatch.setattr(
        "tdb.adapters.tcsh.session.process_table_snapshot",
        lambda: "101 700\n102 701\n",
    )
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: signals.append((pgid, sig)))

    groups = session._owned_process_group_ids()
    session._signal_process_groups(groups, signal.SIGTERM)

    assert groups == set()
    assert signals == []


def test_process_snapshot_failure_falls_back_only_to_validated_live_root(
    tmp_path: Path,
    recording_tcsh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program = tmp_path / "program.csh"
    program.write_text("echo ready\n")
    session = DebugSession(launch_config(program, recording_tcsh), collecting_sink([]))

    class LiveProcess:
        pid = 101
        returncode = None

    session.process = LiveProcess()  # type: ignore[assignment]
    session._process_group_id = 101
    session._process_session_id = 101  # type: ignore[attr-defined]
    monkeypatch.setattr(
        "tdb.adapters.tcsh.session.process_table_snapshot",
        lambda: None,
    )
    monkeypatch.setattr(os, "waitid", lambda idtype, pid, options: None)
    monkeypatch.setattr(os, "getpgid", lambda pid: 101)
    monkeypatch.setattr(os, "getsid", lambda pid: 101)

    assert session._owned_process_group_ids() == {101}

    monkeypatch.setattr(os, "getsid", lambda pid: 700)
    assert session._owned_process_group_ids() == set()


@pytest.mark.asyncio
async def test_stop_delegates_termination_to_guardian_without_direct_killpg(
    tmp_path: Path,
    recording_tcsh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program = tmp_path / "program.csh"
    program.write_text("echo ready\n")
    session = DebugSession(launch_config(program, recording_tcsh), collecting_sink([]))

    class FinishedGuardian:
        pid = 101

        def __init__(self) -> None:
            self.returncode: int | None = None

        async def wait(self) -> int:
            self.returncode = -signal.SIGTERM
            return self.returncode

    control_reader, control_writer = os.pipe()
    status_reader, status_writer = os.pipe()
    session.process = FinishedGuardian()  # type: ignore[assignment]
    session._guardian_control_descriptor = control_writer  # type: ignore[attr-defined]
    session._guardian_status_descriptor = status_reader  # type: ignore[attr-defined]
    os.write(status_writer, b"terminated\n")
    os.close(status_writer)
    os.set_blocking(control_reader, False)
    monkeypatch.setattr(
        os,
        "killpg",
        lambda group, sig: pytest.fail(f"unowned direct killpg({group}, {sig})"),
    )
    try:
        assert await session._stop_process_group() == -signal.SIGTERM
        assert os.read(control_reader, 64) == b"terminate\n"
    finally:
        os.close(control_reader)
        os.close(control_writer)
        os.close(status_reader)


# ---- terminal-mode session seams (guardian FIFO path mode) ----


@pytest.mark.asyncio
async def test_monitor_terminal_reports_signal_death(
    tmp_path: Path,
    recording_tcsh: Path,
) -> None:
    program = tmp_path / "program.csh"
    program.write_text("echo ready\n")
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(program, recording_tcsh, external_terminal=True),
        collecting_sink(events),
    )
    status_reader, status_writer = os.pipe()
    os.set_blocking(status_reader, False)
    session._guardian_status_descriptor = status_reader  # type: ignore[attr-defined]
    session._guardian_ack_queue = asyncio.Queue()  # type: ignore[attr-defined]
    os.write(status_writer, b"signal 9\n")
    os.close(status_writer)
    try:
        await session._monitor_terminal()
    finally:
        # _monitor_terminal's own cleanup (_cleanup -> _close_guardian_
        # descriptors) already closes _guardian_status_descriptor for a
        # terminal-mode session -- don't double-close.
        with contextlib.suppress(OSError):
            os.close(status_reader)
    assert session.state is SessionState.TERMINATED
    assert [event.kind for event in events] == ["exited", "terminated"]
    assert events[0].body == {"exitCode": -9}


@pytest.mark.asyncio
async def test_monitor_terminal_reports_minus_one_on_status_eof_without_report(
    tmp_path: Path,
    recording_tcsh: Path,
) -> None:
    """A guardian that connected (so an empty read is unambiguous, not the
    "hasn't connected yet" case _read_guardian_status retries) and then
    closed the status FIFO without ever writing an exit/signal line (e.g.
    a hard crash) must be reported as exit code -1, matching the guardian
    path-mode status contract (Task 8's review)."""
    program = tmp_path / "program.csh"
    program.write_text("echo ready\n")
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(program, recording_tcsh, external_terminal=True),
        collecting_sink(events),
    )
    status_reader, status_writer = os.pipe()
    os.set_blocking(status_reader, False)
    session._guardian_status_descriptor = status_reader  # type: ignore[attr-defined]
    session._guardian_ack_queue = asyncio.Queue()  # type: ignore[attr-defined]
    session._guardian_status_writer_seen = True  # type: ignore[attr-defined]
    os.close(status_writer)
    try:
        await session._monitor_terminal()
    finally:
        with contextlib.suppress(OSError):
            os.close(status_reader)
    assert [event.kind for event in events] == ["exited", "terminated"]
    assert events[0].body == {"exitCode": -1}


@pytest.mark.asyncio
async def test_monitor_terminal_routes_non_exit_status_to_ack_queue(
    tmp_path: Path,
    recording_tcsh: Path,
) -> None:
    """A guardian status line that isn't `exit `/`signal ` (e.g. a
    `failed ...` internal-termination-failure line, per the guardian's
    path-mode status contract) must be routed onto _guardian_ack_queue by
    _monitor_terminal instead of ending the session -- it's meant for
    whoever is waiting inside _request_guardian_termination."""
    program = tmp_path / "program.csh"
    program.write_text("echo ready\n")
    session = DebugSession(
        launch_config(program, recording_tcsh, external_terminal=True),
        collecting_sink([]),
    )
    status_reader, status_writer = os.pipe()
    control_reader, control_writer = os.pipe()
    os.set_blocking(status_reader, False)
    os.set_blocking(control_reader, False)
    session._guardian_control_descriptor = control_writer  # type: ignore[attr-defined]
    session._guardian_status_descriptor = status_reader  # type: ignore[attr-defined]
    session._guardian_ack_queue = asyncio.Queue()  # type: ignore[attr-defined]
    monitor_task = asyncio.create_task(session._monitor_terminal())
    try:
        request_task = asyncio.create_task(session._request_guardian_termination())
        for _ in range(200):
            try:
                if os.read(control_reader, 64) == b"terminate\n":
                    break
            except BlockingIOError:
                pass
            await asyncio.sleep(0.01)
        else:
            pytest.fail("_request_guardian_termination never wrote terminate")
        os.write(status_writer, b"failed process inspection\n")
        with pytest.raises(RuntimeError, match="process inspection"):
            await asyncio.wait_for(request_task, timeout=2)
    finally:
        os.close(status_writer)
        await asyncio.wait_for(monitor_task, timeout=2)
        os.close(control_reader)
        # _monitor_terminal's own cleanup already closed both guardian
        # descriptors (status_reader and control_writer) for this
        # terminal-mode session -- don't double-close.
        with contextlib.suppress(OSError):
            os.close(control_writer)
        with contextlib.suppress(OSError):
            os.close(status_reader)


@pytest.mark.asyncio
async def test_request_guardian_termination_force_kills_on_ack_timeout(
    tmp_path: Path,
    recording_tcsh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the in-band acknowledgement never arrives, the only remaining
    OS-level handle on a terminal-mode session is the adopted guardian pid
    (self._process_group_id, set from the guardian's own "pid " status
    line during start()'s handshake) -- fall back to killpg(SIGKILL) on it
    rather than leaving the guardian (and whatever it's still supervising)
    to run unsupervised forever."""
    monkeypatch.setattr("tdb.adapters.tcsh.session._TERMINATE_TIMEOUT_SECONDS", 0.05)
    program = tmp_path / "program.csh"
    program.write_text("echo ready\n")
    session = DebugSession(
        launch_config(program, recording_tcsh, external_terminal=True),
        collecting_sink([]),
    )
    control_reader, control_writer = os.pipe()
    os.set_blocking(control_reader, False)
    session._guardian_control_descriptor = control_writer  # type: ignore[attr-defined]
    session._guardian_ack_queue = asyncio.Queue()  # type: ignore[attr-defined]
    session._process_group_id = 424242  # type: ignore[attr-defined]
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda group, sig: killed.append((group, sig)))
    try:
        with pytest.raises(TimeoutError, match="acknowledgement timed out"):
            await session._request_guardian_termination()
        assert killed == [(424242, signal.SIGKILL)]
    finally:
        os.close(control_reader)
        os.close(control_writer)


@pytest.mark.asyncio
async def test_request_guardian_termination_force_kills_when_write_fails(
    tmp_path: Path,
    recording_tcsh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same fallback as the ack-timeout case, but triggered by the
    in-band `terminate\\n` write itself failing (e.g. a descriptor-level
    OS error) rather than a slow/missing acknowledgement."""
    program = tmp_path / "program.csh"
    program.write_text("echo ready\n")
    session = DebugSession(
        launch_config(program, recording_tcsh, external_terminal=True),
        collecting_sink([]),
    )
    control_reader, control_writer = os.pipe()
    session._guardian_control_descriptor = control_writer  # type: ignore[attr-defined]
    session._guardian_ack_queue = asyncio.Queue()  # type: ignore[attr-defined]
    session._process_group_id = 424243  # type: ignore[attr-defined]
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda group, sig: killed.append((group, sig)))
    real_write = os.write

    def failing_write(fd: int, data: bytes) -> int:
        if fd == control_writer:
            raise OSError("simulated guardian control write failure")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", failing_write)
    try:
        with pytest.raises(OSError, match="simulated guardian control write failure"):
            await session._request_guardian_termination()
        assert killed == [(424243, signal.SIGKILL)]
    finally:
        os.close(control_reader)
        os.close(control_writer)


@pytest.mark.asyncio
async def test_close_guardian_descriptors_signals_and_unlinks_before_closing_control(
    tmp_path: Path,
    recording_tcsh: Path,
) -> None:
    """Regression test for the terminal-mode abort wedge: a guardian
    process racing our own teardown must never be left blocked forever
    inside its own os.open(control_path, os.O_RDONLY).

    Covers both halves of the fix: (1) "terminate\\n" is written into the
    control FIFO's buffer while our own descriptor is still open, so a
    guardian that already holds an independent fd on the same path (as if
    it had just opened it) finds the command waiting; (2) both FIFO paths
    are unlinked BEFORE our own control descriptor closes, so a guardian
    that hasn't reached its own open() call yet gets ENOENT (a crash it
    can exit from) once it does, rather than blocking on a path with no
    reader/writer left and no way to ever gain one.
    """
    program = tmp_path / "program.csh"
    program.write_text("echo ready\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    status_path = workspace / "guardian-status.fifo"
    control_path = workspace / "guardian-control.fifo"
    os.mkfifo(status_path, 0o600)
    os.mkfifo(control_path, 0o600)

    session = DebugSession(
        launch_config(program, recording_tcsh, external_terminal=True),
        collecting_sink([]),
    )
    session.workspace = workspace  # type: ignore[attr-defined]
    session._workspace_descriptor = os.open(  # type: ignore[attr-defined]
        workspace, os.O_RDONLY | os.O_DIRECTORY
    )
    session._guardian_status_descriptor = os.open(  # type: ignore[attr-defined]
        status_path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC
    )
    session._guardian_control_descriptor = os.open(  # type: ignore[attr-defined]
        control_path, os.O_RDWR | os.O_CLOEXEC
    )

    # A guardian that already has its own independent fd on control_path
    # (as if it had opened it a moment before teardown started) must
    # still see the queued "terminate\n" -- unaffected by our own
    # descriptors closing, or the path being unlinked afterward, since it
    # holds its own reference.
    guardian_control_fd = os.open(control_path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        session._close_guardian_descriptors()

        assert os.read(guardian_control_fd, 64) == b"terminate\n"

        assert not status_path.exists()
        assert not control_path.exists()
        # A guardian that hadn't reached its own open() calls yet by the
        # time cleanup ran must get ENOENT -- never an indefinite block --
        # once it finally tries.
        with pytest.raises(FileNotFoundError):
            os.open(control_path, os.O_RDONLY | os.O_NONBLOCK)
        with pytest.raises(FileNotFoundError):
            os.open(status_path, os.O_WRONLY | os.O_NONBLOCK)
    finally:
        os.close(guardian_control_fd)
        os.close(session._workspace_descriptor)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_terminate_finds_reparented_descendant_in_original_session(
    tmp_path: Path,
    basic_program: Path,
) -> None:
    child_pid_file = tmp_path / "child-pid"
    child_ready = tmp_path / "child-ready"
    child_terminated = tmp_path / "child-terminated"
    executable = tmp_path / "exiting-job-control-tcsh"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "child_code = '''\n"
        "import signal\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "def stop(*_args):\n"
        "    Path(sys.argv[1]).write_text('terminated')\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "Path(sys.argv[2]).write_text('ready')\n"
        "while True:\n"
        "    time.sleep(1)\n"
        "'''\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', child_code, os.environ['CHILD_TERMINATED'], os.environ['CHILD_READY']],\n"
        "    preexec_fn=os.setpgrp,\n"
        ")\n"
        "Path(os.environ['CHILD_PID']).write_text(str(child.pid))\n"
        "while not Path(os.environ['CHILD_READY']).exists():\n"
        "    time.sleep(0.01)\n"
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    session = DebugSession(
        launch_config(
            basic_program,
            executable,
            env={
                "CHILD_PID": str(child_pid_file),
                "CHILD_READY": str(child_ready),
                "CHILD_TERMINATED": str(child_terminated),
            },
        ),
        collecting_sink([]),
    )
    await session.prepare()
    await session.start()
    assert session.process is not None
    async with asyncio.timeout(1.0):
        while not child_pid_file.exists() or not child_ready.exists():
            await asyncio.sleep(0.01)
    child_pid = int(child_pid_file.read_text())
    assert session.process.returncode is None
    assert os.getpgid(child_pid) == child_pid
    assert os.getsid(child_pid) == session.process.pid
    assert os.getsid(session.process.pid) == session.process.pid
    try:
        await asyncio.wait_for(session.terminate(), timeout=1.0)
    finally:
        try:
            os.killpg(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(session.wait(), timeout=1.0)
        except (RuntimeError, TimeoutError):
            pass

    assert child_terminated.read_text() == "terminated"
    assert session.workspace is not None
    assert not session.workspace.exists()


@pytest.mark.asyncio
async def test_terminate_signals_descendants_after_group_leader_exits(
    tmp_path: Path,
    basic_program: Path,
) -> None:
    child_ready = tmp_path / "child-ready"
    child_terminated = tmp_path / "child-terminated"
    executable = tmp_path / "exiting-leader-tcsh"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "child_code = '''\n"
        "import signal\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "def stop(*_args):\n"
        "    Path(sys.argv[1]).write_text('terminated')\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "Path(sys.argv[2]).write_text('ready')\n"
        "while True:\n"
        "    time.sleep(1)\n"
        "'''\n"
        "subprocess.Popen([\n"
        "    sys.executable, '-c', child_code,\n"
        "    os.environ['CHILD_TERMINATED'], os.environ['CHILD_READY'],\n"
        "])\n"
        "while not Path(os.environ['CHILD_READY']).exists():\n"
        "    time.sleep(0.01)\n"
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(
            basic_program,
            executable,
            env={
                "CHILD_READY": str(child_ready),
                "CHILD_TERMINATED": str(child_terminated),
            },
        ),
        collecting_sink(events),
    )
    await session.prepare()
    await session.start()
    assert session.process is not None
    try:
        async with asyncio.timeout(1.0):
            while not child_ready.exists():
                await asyncio.sleep(0.01)
        assert session.process.returncode is None

        await asyncio.wait_for(session.terminate(), timeout=1.0)
    finally:
        try:
            os.killpg(session.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(session.wait(), timeout=1.0)
        except (RuntimeError, TimeoutError):
            pass

    assert child_terminated.read_text() == "terminated"
    assert [event.kind for event in events[-2:]] == ["exited", "terminated"]


@pytest.mark.asyncio
async def test_output_sink_failure_terminates_child_and_cleans_workspace(
    tmp_path: Path,
    basic_program: Path,
) -> None:
    executable = tmp_path / "long-running-tcsh"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import time\n"
        "os.write(1, b'first line\\n')\n"
        "while True:\n"
        "    time.sleep(1)\n"
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    async def broken_sink(event: SessionEvent) -> None:
        if event.kind == "output":
            raise RuntimeError("sink failed")

    session = DebugSession(launch_config(basic_program, executable), broken_sink)
    await session.prepare()
    assert session.workspace is not None
    workspace = session.workspace
    await session.start()

    with pytest.raises(RuntimeError, match="sink failed"):
        await asyncio.wait_for(session.wait(), timeout=2.0)

    assert session.failure is not None
    assert session.state is SessionState.TERMINATED
    assert not workspace.exists()
    assert session.process is not None
    assert session.process.returncode is not None


@pytest.mark.asyncio
async def test_permission_damaged_workspace_is_removed_without_masking_sink_failure(
    tmp_path: Path,
    basic_program: Path,
) -> None:
    executable = tmp_path / "permission-damaging-tcsh"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "workspace = Path(sys.argv[2]).parents[1]\n"
        "os.chmod(workspace, 0)\n"
        "os.write(1, b'trigger sink failure\\n')\n"
        "while True:\n"
        "    time.sleep(1)\n"
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    async def broken_sink(event: SessionEvent) -> None:
        if event.kind == "output":
            raise RuntimeError("sink failed")

    session = DebugSession(launch_config(basic_program, executable), broken_sink)
    await session.prepare()
    assert session.workspace is not None
    workspace = session.workspace
    await session.start()
    try:
        with pytest.raises(RuntimeError, match="sink failed"):
            await asyncio.wait_for(session.wait(), timeout=2.0)
        assert not workspace.exists()
    finally:
        if workspace.exists():
            workspace.chmod(0o700)
            shutil.rmtree(workspace)


@pytest.mark.asyncio
async def test_replaced_workspace_still_closes_descriptor_owned_transport(
    basic_program: Path,
    recording_tcsh: Path,
    tmp_path: Path,
) -> None:
    session = DebugSession(
        launch_config(basic_program, recording_tcsh),
        collecting_sink([]),
    )
    await session.prepare()
    assert session.workspace is not None
    assert session.transport is not None
    workspace = session.workspace
    transport = session.transport
    retained_workspace = tmp_path / "retained-workspace"
    victim = tmp_path / "victim"
    victim.mkdir()
    workspace.rename(retained_workspace)
    workspace.symlink_to(victim, target_is_directory=True)
    try:
        with pytest.raises(
            RuntimeError, match="refusing to remove a replaced workspace"
        ):
            await session._cleanup()

        assert transport._closed is True
    finally:
        if workspace.is_symlink():
            workspace.unlink()
        if retained_workspace.exists():
            retained_workspace.rename(workspace)
        await transport.close()
        if workspace.exists():
            shutil.rmtree(workspace)


@pytest.mark.asyncio
async def test_failed_instrumentation_removes_partial_workspace(
    tmp_path: Path,
    recording_tcsh: Path,
) -> None:
    program = tmp_path / "invalid.csh"
    program.write_bytes(b"echo \xff\n")
    session = DebugSession(
        launch_config(program, recording_tcsh),
        collecting_sink([]),
    )

    with pytest.raises(LaunchError) as raised:
        await session.prepare()

    assert str(program) in str(raised.value)
    assert session.workspace is not None
    assert not session.workspace.exists()
    assert session.state is SessionState.NEW


@pytest.mark.asyncio
async def test_live_stock_tcsh_session_exits_and_cleans_workspace(
    tmp_path: Path,
    tcsh_path: Path,
) -> None:
    program = tmp_path / "empty.csh"
    program.write_text("")
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(program, tcsh_path),
        collecting_sink(events),
    )
    await session.prepare()
    assert session.workspace is not None
    workspace = session.workspace

    await session.start()
    await asyncio.wait_for(session.wait(), timeout=2.0)

    assert [(event.kind, event.body) for event in events] == [
        ("exited", {"exitCode": 0}),
        ("terminated", {}),
    ]
    assert not workspace.exists()


@pytest.mark.parametrize(
    ("mode", "start_depth", "event_depth", "is_breakpoint", "should_stop"),
    [
        (RunMode.CONTINUE, 0, 0, False, False),
        (RunMode.CONTINUE, 0, 1, True, True),
        (RunMode.STEP_IN, 0, 1, False, True),
        (RunMode.NEXT, 0, 1, False, False),
        (RunMode.NEXT, 0, 0, False, True),
        (RunMode.STEP_OUT, 1, 1, False, False),
        (RunMode.STEP_OUT, 1, 0, False, True),
    ],
)
def test_probe_stop_decision(
    mode: RunMode,
    start_depth: int,
    event_depth: int,
    is_breakpoint: bool,
    should_stop: bool,
) -> None:
    assert (
        should_stop_at_probe(mode, start_depth, event_depth, is_breakpoint)
        is should_stop
    )


@pytest.mark.asyncio
async def test_every_probe_is_released_exactly_once(
    tmp_path: Path,
    tcsh_path: Path,
) -> None:
    program = tmp_path / "release-each.csh"
    program.write_text("echo first\necho second\n")
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(program, tcsh_path, stop_on_entry=True),
        collecting_sink(events),
    )
    await session.prepare()
    assert session.transport is not None
    release_count = 0
    release = session.transport.release

    async def counting_release() -> None:
        nonlocal release_count
        release_count += 1
        await release()

    session.transport.release = counting_release  # type: ignore[method-assign]
    try:
        await session.start()
        stopped = await wait_for_event(events, "stopped")
        assert stopped.body["reason"] == StopReason.ENTRY.value
        assert release_count == 0

        await session.continue_()
        await asyncio.wait_for(session.wait(), timeout=2.0)
    finally:
        if session.state is not SessionState.TERMINATED:
            await session.terminate()

    assert release_count == 2
    assert [event.body["reason"] for event in events if event.kind == "stopped"] == [
        "entry"
    ]


@pytest.mark.asyncio
async def test_detach_releases_current_stop_and_disables_future_stops(
    basic_program: Path,
    tcsh_path: Path,
) -> None:
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(basic_program, tcsh_path, stop_on_entry=True),
        collecting_sink(events),
    )

    class ReleasingTransport:
        def __init__(self) -> None:
            self.releases = 0

        async def release(self) -> None:
            self.releases += 1

    transport = ReleasingTransport()
    session.transport = transport  # type: ignore[assignment]
    session.state = SessionState.STOPPED
    session._entry_pending = True
    session._breakpoint_probe_ids = {basic_program: frozenset({1})}

    await session.detach()

    assert session.state is SessionState.RUNNING
    assert transport.releases == 1
    assert session._entry_pending is False
    assert session._breakpoint_probe_ids == {}
    assert events == [
        SessionEvent("continued", {"threadId": 1, "allThreadsContinued": True})
    ]


@pytest.mark.asyncio
async def test_step_in_and_step_out_track_original_source_stack(
    tmp_path: Path,
    tcsh_path: Path,
) -> None:
    child = tmp_path / "child.csh"
    child.write_text("echo child\n")
    program = tmp_path / "main.csh"
    program.write_text("source child.csh\necho caller\n")
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(program, tcsh_path, stop_on_entry=True),
        collecting_sink(events),
    )
    await session.prepare()
    try:
        await session.start()
        await wait_for_event(events, "stopped")
        thread = session.threads()[0]
        assert (thread.id, thread.name) == (1, "tcsh")

        await session.step_in()
        stopped_in = await wait_for_event(events, "stopped", occurrence=2)
        assert stopped_in.body["reason"] == StopReason.STEP.value
        frames = session.stack_trace()
        assert [(frame.path, frame.line) for frame in frames] == [
            (child.resolve(), 1),
            (program.resolve(), 1),
        ]
        assert len({frame.id for frame in frames}) == 2
        assert session.stack_trace() == frames

        await session.step_out()
        stopped_out = await wait_for_event(events, "stopped", occurrence=3)
        assert stopped_out.body["reason"] == StopReason.STEP.value
        assert [(frame.path, frame.line) for frame in session.stack_trace()] == [
            (program.resolve(), 2)
        ]

        await session.continue_()
        await asyncio.wait_for(session.wait(), timeout=2.0)
    finally:
        if session.state is not SessionState.TERMINATED:
            await session.terminate()

    execution_events = [
        event.kind for event in events if event.kind in {"continued", "stopped"}
    ]
    assert execution_events == [
        "stopped",
        "continued",
        "stopped",
        "continued",
        "stopped",
        "continued",
    ]


@pytest.mark.asyncio
async def test_breakpoint_overrides_next_suppression_in_nested_source(
    tmp_path: Path,
    tcsh_path: Path,
) -> None:
    child = tmp_path / "child.csh"
    child.write_text("echo child\necho child-again\n")
    program = tmp_path / "main.csh"
    program.write_text("source child.csh\necho caller\n")
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(program, tcsh_path, stop_on_entry=True),
        collecting_sink(events),
    )
    await session.prepare()
    bound = session.set_breakpoints(child, (1,))
    assert [(item.verified, item.line) for item in bound] == [(True, 1)]
    try:
        await session.start()
        await wait_for_event(events, "stopped")
        await session.next()
        stopped = await wait_for_event(events, "stopped", occurrence=2)

        assert stopped.body["reason"] == StopReason.BREAKPOINT.value
        assert session.stack_trace()[0].path == child.resolve()
    finally:
        await session.terminate()


@pytest.mark.asyncio
async def test_reused_source_tracks_its_dynamic_nesting_depth(
    tmp_path: Path,
    tcsh_path: Path,
) -> None:
    shared = tmp_path / "shared.csh"
    shared.write_text("echo shared\n")
    middle = tmp_path / "middle.csh"
    middle.write_text("source shared.csh\n")
    program = tmp_path / "main.csh"
    program.write_text("source shared.csh\nsource middle.csh\necho done\n")
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(program, tcsh_path, stop_on_entry=True),
        collecting_sink(events),
    )
    await session.prepare()
    session.set_breakpoints(program, (2,))
    try:
        await session.start()
        await wait_for_event(events, "stopped")
        await session.step_in()
        await wait_for_event(events, "stopped", occurrence=2)
        await session.continue_()
        await wait_for_event(events, "stopped", occurrence=3)
        await session.step_in()
        await wait_for_event(events, "stopped", occurrence=4)
        await session.step_in()
        await wait_for_event(events, "stopped", occurrence=5)

        assert [(frame.path, frame.line) for frame in session.stack_trace()] == [
            (shared.resolve(), 1),
            (middle.resolve(), 1),
            (program.resolve(), 2),
        ]
    finally:
        await session.terminate()


@pytest.mark.asyncio
async def test_step_out_at_root_keeps_current_probe_stopped(
    tmp_path: Path,
    tcsh_path: Path,
) -> None:
    program = tmp_path / "root.csh"
    program.write_text("echo root\n")
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(program, tcsh_path, stop_on_entry=True),
        collecting_sink(events),
    )
    await session.prepare()
    assert session.transport is not None
    release_count = 0
    release = session.transport.release

    async def counting_release() -> None:
        nonlocal release_count
        release_count += 1
        await release()

    session.transport.release = counting_release  # type: ignore[method-assign]
    try:
        await session.start()
        await wait_for_event(events, "stopped")

        with pytest.raises(InvalidStateError, match="root"):
            await session.step_out()

        assert session.state is SessionState.STOPPED
        assert release_count == 0
        assert len(session.stack_trace()) == 1
    finally:
        await session.terminate()


@pytest.mark.asyncio
async def test_execution_requests_require_stopped_state(
    tmp_path: Path,
    tcsh_path: Path,
) -> None:
    program = tmp_path / "running.csh"
    program.write_text("echo running\n")
    session = DebugSession(
        launch_config(program, tcsh_path),
        collecting_sink([]),
    )
    await session.prepare()

    for request in (session.continue_, session.next, session.step_in, session.step_out):
        with pytest.raises(InvalidStateError, match="stopped"):
            await request()

    await session.terminate()


@pytest.mark.asyncio
async def test_continued_sink_failure_terminates_and_cleans_session(
    tmp_path: Path,
    tcsh_path: Path,
) -> None:
    program = tmp_path / "sink-failure.csh"
    program.write_text("echo first\nsleep 30\n")
    events: list[SessionEvent] = []
    sink_error = RuntimeError("continued sink failed")

    async def sink(event: SessionEvent) -> None:
        events.append(event)
        if event.kind == "continued":
            raise sink_error

    session = DebugSession(
        launch_config(program, tcsh_path, stop_on_entry=True),
        sink,
    )
    await session.prepare()
    assert session.workspace is not None
    workspace = session.workspace
    await session.start()
    await wait_for_event(events, "stopped")

    with pytest.raises(RuntimeError, match="continued sink failed") as raised:
        await session.continue_()

    assert raised.value is sink_error
    with pytest.raises(RuntimeError, match="continued sink failed") as waited:
        await asyncio.wait_for(session.wait(), timeout=2.0)
    assert waited.value is sink_error
    assert session.state is SessionState.TERMINATED
    assert not workspace.exists()


class ScriptedEventTransport:
    def __init__(self, *events: ProbeEvent) -> None:
        self._events = asyncio.Queue[ProbeEvent]()
        for event in events:
            self._events.put_nowait(event)
        self.closed = False

    async def next_event(self) -> ProbeEvent:
        return await self._events.get()

    async def release(self) -> None:
        raise AssertionError("an unmatched leave must not release a probe")

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_unmatched_source_leave_is_a_protocol_failure(tmp_path: Path) -> None:
    program = tmp_path / "root.csh"
    program.write_text("echo root\n")
    blocking_tcsh = tmp_path / "blocking-tcsh"
    blocking_tcsh.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n")
    blocking_tcsh.chmod(blocking_tcsh.stat().st_mode | stat.S_IXUSR)
    session = DebugSession(
        launch_config(program, blocking_tcsh),
        collecting_sink([]),
    )
    await session.prepare()
    assert session.transport is not None
    await session.transport.close()
    scripted = ScriptedEventTransport(ProbeEvent("leave", probe_id=None, source_id=7))
    session.transport = scripted  # type: ignore[assignment]

    await session.start()
    with pytest.raises(TransportError, match="unmatched source leave"):
        await asyncio.wait_for(session.wait(), timeout=2.0)

    assert scripted.closed is True


@pytest.mark.asyncio
async def test_inspection_is_lazy_cached_and_shared_across_source_frames(
    tmp_path: Path,
    tcsh_path: Path,
) -> None:
    child = tmp_path / "child.csh"
    child.write_text("echo child\n")
    program = tmp_path / "main.csh"
    program.write_text("source child.csh\necho root\n")
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(program, tcsh_path, stop_on_entry=True),
        collecting_sink(events),
    )
    await session.prepare()
    assert session.transport is not None
    try:
        await session.start()
        await wait_for_event(events, "stopped")
        await session.step_in()
        await wait_for_event(events, "stopped", occurrence=2)
        frames = session.stack_trace()
        assert len(frames) == 2

        calls: list[tuple[int, str]] = []
        results = {
            "set": "shared\tcurrent\nargv\t(one two)\n",
            "env": "Z=last\nA=one=two\n",
            "alias": "ll\techo one  two\n",
        }

        async def respond(body_factory, timeout: float = 5.0) -> str:
            request_id = 40 + len(calls)
            body = body_factory(request_id)
            request_path = (
                session.transport.paths.control_fifo.parent
                / "requests"
                / f"request-{request_id}.csh"
            )
            command = request_path.read_text().removesuffix("\n")
            response_fifo = session.transport.paths.response_fifo
            nonce = session.transport.paths.nonce
            assert body == (
                f"echo {nonce}\\ BEGIN\\ {request_id}\\ ok >! {response_fifo}\n"
                f"source {request_path} >>&! {response_fifo}\n"
                f"echo >>! {response_fifo}\n"
                f"echo {nonce}\\ END\\ {request_id} >>! {response_fifo}\n"
            )
            calls.append((request_id, command))
            return results[command]

        session.transport.send_request = respond  # type: ignore[method-assign]
        inner = {scope.name: scope for scope in session.scopes(frames[0].id)}
        outer = {scope.name: scope for scope in session.scopes(frames[1].id)}

        inner_arguments = await session.variables(
            inner["Arguments"].variables_reference
        )
        outer_shell = await session.variables(
            outer["Shell Variables"].variables_reference
        )
        inner_environment = await session.variables(
            inner["Environment"].variables_reference
        )
        outer_environment = await session.variables(
            outer["Environment"].variables_reference
        )
        aliases = await session.variables(inner["Aliases"].variables_reference)

        assert [(item.name, item.value) for item in inner_arguments] == [
            ("argv", "(one two)")
        ]
        assert [(item.name, item.value) for item in outer_shell] == [
            ("argv", "(one two)"),
            ("shared", "current"),
        ]
        assert inner_environment == outer_environment
        assert [(item.name, item.value) for item in aliases] == [
            ("ll", "echo one  two")
        ]
        assert [command for _, command in calls] == ["set", "env", "alias"]
    finally:
        await session.terminate()


@pytest.mark.asyncio
async def test_inspection_rejects_unknown_and_resumed_handles(
    tmp_path: Path,
    tcsh_path: Path,
) -> None:
    program = tmp_path / "handles.csh"
    program.write_text("echo first\necho second\n")
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(program, tcsh_path, stop_on_entry=True),
        collecting_sink(events),
    )
    await session.prepare()
    try:
        await session.start()
        await wait_for_event(events, "stopped")
        frame = session.stack_trace()[0]
        with pytest.raises(UnknownFrameError, match="999"):
            session.scopes(999)
        with pytest.raises(UnknownReferenceError, match="999"):
            await session.variables(999)
        reference = session.scopes(frame.id)[0].variables_reference

        await session.continue_()

        with pytest.raises(StaleReferenceError, match=str(reference)):
            await session.variables(reference)
    finally:
        await session.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "raised_error"),
    [(TimeoutError(), "timed out"), ("not tab delimited\n", "malformed")],
)
async def test_inspection_errors_are_cached_without_releasing_the_probe(
    tmp_path: Path,
    tcsh_path: Path,
    response: str | BaseException,
    raised_error: str,
) -> None:
    program = tmp_path / "inspection-error.csh"
    program.write_text("echo stopped\n")
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(program, tcsh_path, stop_on_entry=True),
        collecting_sink(events),
    )
    await session.prepare()
    assert session.transport is not None
    calls = 0
    releases = 0
    release = session.transport.release

    async def respond(body_factory, timeout: float = 5.0) -> str:
        nonlocal calls
        calls += 1
        if isinstance(response, BaseException):
            raise response
        return response

    async def counting_release() -> None:
        nonlocal releases
        releases += 1
        await release()

    session.transport.send_request = respond  # type: ignore[method-assign]
    session.transport.release = counting_release  # type: ignore[method-assign]
    try:
        await session.start()
        await wait_for_event(events, "stopped")
        reference = session.scopes(session.stack_trace()[0].id)[0].variables_reference

        with pytest.raises(InspectionError, match=raised_error) as first:
            await session.variables(reference)
        with pytest.raises(InspectionError, match=raised_error) as second:
            await session.variables(reference)

        assert second.value is first.value
        assert calls == 1
        assert releases == 0
        assert session.state is SessionState.STOPPED
    finally:
        await session.terminate()


@pytest.mark.asyncio
async def test_resume_invalidates_inspection_handles_before_probe_release(
    tmp_path: Path,
    tcsh_path: Path,
) -> None:
    program = tmp_path / "invalidate-first.csh"
    program.write_text("echo first\nsleep 30\n")
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(program, tcsh_path, stop_on_entry=True),
        collecting_sink(events),
    )
    await session.prepare()
    assert session.transport is not None
    await session.start()
    await wait_for_event(events, "stopped")
    reference = session.scopes(session.stack_trace()[0].id)[0].variables_reference
    release = session.transport.release

    async def require_stale_handle() -> None:
        with pytest.raises(StaleReferenceError):
            session._variable_store.scope_kind(reference)
        await release()

    session.transport.release = require_stale_handle  # type: ignore[method-assign]
    try:
        await session.continue_()
    finally:
        await session.terminate()


@pytest.mark.asyncio
async def test_live_tcsh_exposes_all_four_current_shell_scopes(
    tmp_path: Path,
    tcsh_path: Path,
) -> None:
    program = tmp_path / "live-inspection.csh"
    program.write_text(
        'set shell_value = "hello world"\nalias ll "echo one two"\necho breakpoint\n'
    )
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(
            program,
            tcsh_path,
            args=("first", "second word"),
            env={"TCSH_DAP_SCOPE_TEST": "one=two"},
        ),
        collecting_sink(events),
    )
    await session.prepare()
    assert session.workspace is not None
    session.set_breakpoints(program, (3,))
    try:
        await session.start()
        await wait_for_event(events, "stopped")
        frame = session.stack_trace()[0]
        scopes = {scope.name: scope for scope in session.scopes(frame.id)}
        values = {
            name: await session.variables(scope.variables_reference)
            for name, scope in scopes.items()
        }

        assert [scope.name for scope in session.scopes(frame.id)] == [
            "Shell Variables",
            "Environment",
            "Aliases",
            "Arguments",
        ]
        assert ("shell_value", "hello world") in [
            (item.name, item.value) for item in values["Shell Variables"]
        ]
        assert ("TCSH_DAP_SCOPE_TEST", "one=two") in [
            (item.name, item.value) for item in values["Environment"]
        ]
        assert ("ll", "echo one two") in [
            (item.name, item.value) for item in values["Aliases"]
        ]
        assert [(item.name, item.value) for item in values["Arguments"]] == [
            ("argv", "(first second word)")
        ]
        assert list((session.workspace / "requests").iterdir()) == []
    finally:
        await session.terminate()


@pytest.mark.asyncio
async def test_evaluate_preserves_verbatim_multiline_combined_output_and_empty_output(
    tmp_path: Path,
    tcsh_path: Path,
) -> None:
    program = tmp_path / "evaluate.csh"
    program.write_text("echo stopped\n")
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(program, tcsh_path, stop_on_entry=True),
        collecting_sink(events),
    )
    await session.prepare()
    try:
        await session.start()
        await wait_for_event(events, "stopped")
        frame = session.stack_trace()[0]

        result = await session.evaluate(
            "echo first\n/bin/sh -c 'echo problem >&2'\necho second",
            frame_id=frame.id,
        )
        empty = await session.evaluate("/bin/true", frame_id=None)

        assert result.result == "first\nproblem\nsecond\n"
        assert result.variables_reference == 0
        assert empty.result == ""
        assert session.state is SessionState.STOPPED
    finally:
        await session.terminate()


@pytest.mark.asyncio
async def test_evaluate_mutates_the_live_shell_and_passes_expression_verbatim(
    tmp_path: Path,
    tcsh_path: Path,
) -> None:
    program = tmp_path / "mutation.csh"
    program.write_text("echo stopped\n")
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(program, tcsh_path, stop_on_entry=True),
        collecting_sink(events),
    )
    await session.prepare()
    assert session.transport is not None
    await session.start()
    await wait_for_event(events, "stopped")
    expression = "set value = changed\n# trailing comment"
    seen_expressions: list[str] = []
    send_request = session.transport.send_request

    async def record_expression(body_factory, timeout: float = 5.0) -> str:
        def recording_factory(request_id: int) -> str:
            body = body_factory(request_id)
            request_path = (
                session.transport.paths.control_fifo.parent
                / "requests"
                / f"request-{request_id}.csh"
            )
            seen_expressions.append(request_path.read_text().removesuffix("\n"))
            return body

        return await send_request(recording_factory, timeout)

    session.transport.send_request = record_expression  # type: ignore[method-assign]
    try:
        changed = await session.evaluate(expression, frame_id=None)
        observed = await session.evaluate("echo $value", frame_id=None)

        assert changed.result == ""
        assert observed.result == "changed\n"
        assert seen_expressions == [expression, "echo $value"]
    finally:
        await session.terminate()


@pytest.mark.asyncio
async def test_evaluate_rejects_running_unknown_stale_frames_and_nul(
    tmp_path: Path,
    tcsh_path: Path,
) -> None:
    program = tmp_path / "validation.csh"
    program.write_text("echo first\necho second\n")
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(program, tcsh_path, stop_on_entry=True),
        collecting_sink(events),
    )
    await session.prepare()
    try:
        with pytest.raises(InvalidStateError, match="stopped"):
            await session.evaluate("echo configured", frame_id=None)

        await session.start()
        await wait_for_event(events, "stopped")
        old_frame = session.stack_trace()[0]
        with pytest.raises(UnknownFrameError, match="999"):
            await session.evaluate("echo unknown", frame_id=999)
        with pytest.raises(EvaluationError, match="NUL"):
            await session.evaluate("echo before\x00after", frame_id=None)

        await session.step_in()
        await wait_for_event(events, "stopped", occurrence=2)
        with pytest.raises(UnknownFrameError, match=str(old_frame.id)):
            await session.evaluate("echo stale", frame_id=old_frame.id)

        await session.continue_()
        with pytest.raises(InvalidStateError, match="stopped"):
            await session.evaluate("echo running", frame_id=None)
    finally:
        await session.terminate()


@pytest.mark.asyncio
async def test_evaluate_timeout_is_retryable_and_keeps_probe_stopped(
    tmp_path: Path,
    tcsh_path: Path,
) -> None:
    program = tmp_path / "timeout.csh"
    program.write_text("echo stopped\n")
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(program, tcsh_path, stop_on_entry=True),
        collecting_sink(events),
    )
    await session.prepare()
    assert session.transport is not None
    await session.start()
    await wait_for_event(events, "stopped")
    send_request = session.transport.send_request
    calls = 0

    async def timeout_once(body_factory, timeout: float = 5.0) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError
        return await send_request(body_factory, timeout)

    session.transport.send_request = timeout_once  # type: ignore[method-assign]
    try:
        with pytest.raises(EvaluationError, match="timed out"):
            await session.evaluate("sleep 20", frame_id=None)

        assert session.state is SessionState.STOPPED
        assert (await session.evaluate("echo retry", frame_id=None)).result == "retry\n"
    finally:
        await session.terminate()


@pytest.mark.asyncio
async def test_incomplete_evaluation_terminates_desynchronized_session(
    tmp_path: Path,
    tcsh_path: Path,
) -> None:
    program = tmp_path / "incomplete-evaluation.csh"
    program.write_text("echo stopped\n")
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(program, tcsh_path, stop_on_entry=True),
        collecting_sink(events),
    )
    await session.prepare()
    assert session.transport is not None
    await session.start()
    await wait_for_event(events, "stopped")

    async def incomplete(body_factory, timeout: float = 5.0) -> str:
        del body_factory, timeout
        raise IncompleteResponseError("evaluation response is incomplete")

    session.transport.send_request = incomplete  # type: ignore[method-assign]

    with pytest.raises(EvaluationError, match="incomplete") as raised:
        await session.evaluate("echo partial", frame_id=None)

    assert session.state is SessionState.TERMINATED
    assert isinstance(raised.value.__cause__, IncompleteResponseError)
    with pytest.raises(EvaluationError) as waited:
        await session.wait()
    assert waited.value is raised.value
    assert [event.kind for event in events[-2:]] == ["exited", "terminated"]


@pytest.mark.asyncio
async def test_evaluate_process_exit_uses_normal_termination_watcher(
    tmp_path: Path,
    tcsh_path: Path,
) -> None:
    program = tmp_path / "evaluation-exit.csh"
    program.write_text("echo unreachable\n")
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(program, tcsh_path, stop_on_entry=True),
        collecting_sink(events),
    )
    await session.prepare()
    await session.start()
    await wait_for_event(events, "stopped")

    with pytest.raises(EvaluationError):
        await session.evaluate("kill -TERM $$", frame_id=None)
    await session.wait()

    assert session.failure is None
    assert session.state is SessionState.TERMINATED
    assert [(event.kind, event.body) for event in events[-2:]] == [
        ("exited", {"exitCode": -signal.SIGTERM}),
        ("terminated", {}),
    ]


@pytest.mark.asyncio
async def test_partial_inspection_timeout_terminates_desynchronized_session(
    tmp_path: Path,
    tcsh_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program = tmp_path / "partial-response.csh"
    program.write_text("echo stopped\n")
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(program, tcsh_path, stop_on_entry=True),
        collecting_sink(events),
    )
    await session.prepare()
    assert session.transport is not None
    assert session.workspace is not None
    workspace = session.workspace
    original_send_request = session.transport.send_request
    request_files: list[Path] = []

    def render_partial_response(request_id: int, command: str, paths) -> str:
        body = render_runtime_inspection_request(
            request_id,
            f"/bin/sleep 1\n{command}",
            paths,
        )
        request_files.append(
            paths.control_fifo.parent / "requests" / f"request-{request_id}.csh"
        )
        return body

    async def send_with_short_timeout(body_factory, timeout: float = 5.0) -> str:
        del timeout
        return await original_send_request(body_factory, timeout=0.05)

    monkeypatch.setattr(
        "tdb.adapters.tcsh.session.render_inspection_request", render_partial_response
    )
    session.transport.send_request = send_with_short_timeout  # type: ignore[method-assign]
    try:
        await session.start()
        await wait_for_event(events, "stopped")
        reference = session.scopes(session.stack_trace()[0].id)[0].variables_reference

        with pytest.raises(InspectionError, match="incomplete") as raised:
            await asyncio.wait_for(session.variables(reference), timeout=1.0)

        assert session.state is SessionState.TERMINATED
        assert request_files and all(not path.exists() for path in request_files)
        assert not workspace.exists()
        with pytest.raises(InspectionError) as waited:
            await asyncio.wait_for(session.wait(), timeout=0.25)
        assert waited.value is raised.value
        with pytest.raises(InvalidStateError, match="stopped"):
            await asyncio.wait_for(session.continue_(), timeout=0.25)
        assert [event.kind for event in events[-2:]] == ["exited", "terminated"]
    finally:
        if session.state is not SessionState.TERMINATED:
            await session.terminate()


@pytest.mark.asyncio
async def test_partial_inspection_control_write_terminates_desynchronized_session(
    tmp_path: Path,
    tcsh_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "advanced"
    program = tmp_path / "partial-control.csh"
    program.write_text(f"/bin/sleep 1\n/usr/bin/touch {marker}\n/bin/sleep 30\n")
    events: list[SessionEvent] = []
    session = DebugSession(
        launch_config(program, tcsh_path, stop_on_entry=True),
        collecting_sink(events),
    )
    await session.prepare()
    assert session.transport is not None
    assert session.workspace is not None
    workspace = session.workspace
    try:
        await session.start()
        await wait_for_event(events, "stopped")
        reference = session.scopes(session.stack_trace()[0].id)[0].variables_reference
        control_writer = session.transport._control_writer
        assert control_writer is not None
        original_write = os.write
        control_write_calls = 0

        def write_through_source_then_fail(
            descriptor: int,
            data: bytes | memoryview,
        ) -> int:
            nonlocal control_write_calls
            if descriptor != control_writer:
                return original_write(descriptor, data)
            control_write_calls += 1
            if control_write_calls == 1:
                encoded = bytes(data)
                first_line_end = encoded.index(b"\n") + 1
                source_line_end = encoded.index(b"\n", first_line_end) + 1
                return original_write(descriptor, encoded[:source_line_end])
            raise BrokenPipeError(errno.EPIPE, "injected control write failure")

        monkeypatch.setattr(os, "write", write_through_source_then_fail)

        with pytest.raises(InspectionError, match="set inspection failed") as raised:
            await asyncio.wait_for(session.variables(reference), timeout=1.0)

        assert session.state is SessionState.TERMINATED
        assert isinstance(raised.value.__cause__, IncompleteResponseError)
        assert not marker.exists()
        assert not workspace.exists()
        with pytest.raises(StaleReferenceError):
            await session.variables(reference)
        with pytest.raises(InspectionError) as waited:
            await asyncio.wait_for(session.wait(), timeout=0.25)
        assert waited.value is raised.value
        with pytest.raises(InvalidStateError, match="stopped"):
            await asyncio.wait_for(session.continue_(), timeout=0.25)
        assert [event.kind for event in events[-2:]] == ["exited", "terminated"]
    finally:
        if session.state is not SessionState.TERMINATED:
            await session.terminate()

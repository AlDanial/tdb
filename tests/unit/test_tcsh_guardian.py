from __future__ import annotations

import asyncio
import json
import os
import select
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tdb.adapters.tcsh import guardian


async def start_guardian(
    *argv: str,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> asyncio.subprocess.Process:
    guardian = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "tdb"
        / "adapters"
        / "tcsh"
        / "guardian.py"
    )
    environment = dict(os.environ)
    if env is not None:
        environment.update(env)
    return await asyncio.create_subprocess_exec(
        sys.executable,
        str(guardian),
        "--",
        *argv,
        cwd=str(cwd),
        env=environment,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )


def read_descriptor_line(descriptor: int) -> bytes:
    data = bytearray()
    while not data.endswith(b"\n"):
        chunk = os.read(descriptor, 1)
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def read_descriptor_line_before(descriptor: int, timeout: float) -> bytes:
    readable, _, _ = select.select((descriptor,), (), (), timeout)
    if not readable:
        return b""
    return read_descriptor_line(descriptor)


async def start_controlled_guardian(
    *argv: str,
    cwd: Path,
    start: bool = True,
) -> tuple[asyncio.subprocess.Process, int, int]:
    guardian_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "tdb"
        / "adapters"
        / "tcsh"
        / "guardian.py"
    )
    status_reader, status_writer = os.pipe()
    control_reader, control_writer = os.pipe()
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(guardian_path),
            "--status-fd",
            str(status_writer),
            "--control-fd",
            str(control_reader),
            "--",
            *argv,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            pass_fds=(status_writer, control_reader),
        )
    finally:
        os.close(status_writer)
        os.close(control_reader)
    assert await asyncio.to_thread(read_descriptor_line, status_reader) == b"armed\n"
    if start:
        os.write(control_writer, b"start\n")
    return process, control_writer, status_reader


@pytest.mark.asyncio
async def test_guardian_preserves_child_argv_env_stdio_cwd_and_exit_code(
    tmp_path: Path,
) -> None:
    record_path = tmp_path / "record.json"
    child = tmp_path / "record-child"
    child.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['RECORD_PATH']).write_text(json.dumps({\n"
        "    'argv': sys.argv,\n"
        "    'cwd': os.getcwd(),\n"
        "    'overlay': os.environ.get('GUARDIAN_OVERLAY'),\n"
        "    'stdin': os.read(0, 1).decode(),\n"
        "    'sid': os.getsid(0),\n"
        "}))\n"
        "os.write(1, b'child stdout\\n')\n"
        "os.write(2, b'child stderr\\n')\n"
        "raise SystemExit(7)\n"
    )
    child.chmod(child.stat().st_mode | stat.S_IXUSR)

    process = await start_guardian(
        str(child),
        "first",
        "",
        "two words",
        cwd=tmp_path,
        env={"RECORD_PATH": str(record_path), "GUARDIAN_OVERLAY": "present"},
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=2)

    record = json.loads(record_path.read_text())
    assert record == {
        "argv": [str(child), "first", "", "two words"],
        "cwd": str(tmp_path),
        "overlay": "present",
        "stdin": "",
        "sid": process.pid,
    }
    assert stdout == b"child stdout\n"
    assert stderr == b"child stderr\n"
    assert process.returncode == 7


@pytest.mark.asyncio
async def test_guardian_retains_session_until_background_member_exits(
    tmp_path: Path,
) -> None:
    background_pid_path = tmp_path / "background-pid"
    child = tmp_path / "background-child"
    child.write_text(
        "#!/usr/bin/env python3\n"
        "import subprocess\n"
        "import sys\n"
        "from pathlib import Path\n"
        "background = subprocess.Popen([\n"
        "    sys.executable, '-c', 'import time; time.sleep(0.2)'\n"
        "])\n"
        "Path(sys.argv[1]).write_text(str(background.pid))\n"
        "raise SystemExit(3)\n"
    )
    child.chmod(child.stat().st_mode | stat.S_IXUSR)
    process = await start_guardian(str(child), str(background_pid_path), cwd=tmp_path)
    try:
        async with asyncio.timeout(1):
            while not background_pid_path.exists():
                await asyncio.sleep(0.005)
        await asyncio.sleep(0.03)

        assert process.returncode is None
        await asyncio.wait_for(process.wait(), timeout=1)
        assert process.returncode == 3
    finally:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


@pytest.mark.asyncio
async def test_controlled_termination_remains_available_during_natural_drain(
    tmp_path: Path,
) -> None:
    child_pid_path = tmp_path / "root-pid"
    background_pid_path = tmp_path / "background-pid"
    child = tmp_path / "draining-child"
    child.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "from pathlib import Path\n"
        "background = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "Path(sys.argv[2]).write_text(str(background.pid))\n"
        "raise SystemExit(3)\n"
    )
    child.chmod(child.stat().st_mode | stat.S_IXUSR)
    process, control_writer, status_reader = await start_controlled_guardian(
        str(child),
        str(child_pid_path),
        str(background_pid_path),
        cwd=tmp_path,
    )
    child_pid = -1
    background_pid = -1
    try:
        assert await asyncio.to_thread(read_descriptor_line, status_reader) == b"ok\n"
        async with asyncio.timeout(1):
            while not child_pid_path.exists() or not background_pid_path.exists():
                await asyncio.sleep(0.005)
        child_pid = int(child_pid_path.read_text())
        background_pid = int(background_pid_path.read_text())
        async with asyncio.timeout(1):
            while True:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.005)

        assert process.returncode is None
        os.write(control_writer, b"terminate\n")
        assert (
            await asyncio.wait_for(
                asyncio.to_thread(read_descriptor_line, status_reader),
                timeout=1,
            )
            == b"terminated\n"
        )
        await asyncio.wait_for(process.wait(), timeout=1)

        assert process.returncode == 3
        # Dead-or-zombie: without an init reaper at PID 1 (docker build
        # RUN steps), the killed orphan stays an unreaped zombie and a
        # plain os.kill(pid, 0) probe would keep succeeding.
        assert guardian._process_is_gone(background_pid)  # type: ignore[attr-defined]
    finally:
        os.close(control_writer)
        os.close(status_reader)
        if background_pid > 0:
            try:
                os.kill(background_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if child_pid > 0:
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


@pytest.mark.asyncio
async def test_guardian_does_not_abandon_live_session_member_on_sigterm(
    tmp_path: Path,
) -> None:
    background_pid_path = tmp_path / "background-pid"
    child = tmp_path / "signal-child"
    child.write_text(
        "#!/usr/bin/env python3\n"
        "import subprocess\n"
        "import sys\n"
        "from pathlib import Path\n"
        "code = 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'\n"
        "background = subprocess.Popen([sys.executable, '-c', code])\n"
        "Path(sys.argv[1]).write_text(str(background.pid))\n"
    )
    child.chmod(child.stat().st_mode | stat.S_IXUSR)
    process = await start_guardian(str(child), str(background_pid_path), cwd=tmp_path)
    background_pid = -1
    try:
        async with asyncio.timeout(1):
            while not background_pid_path.exists():
                await asyncio.sleep(0.005)
        background_pid = int(background_pid_path.read_text())
        os.kill(process.pid, signal.SIGTERM)
        await asyncio.sleep(0.03)

        assert process.returncode is None
        os.kill(background_pid, signal.SIGKILL)
        await asyncio.wait_for(process.wait(), timeout=1)
        assert process.returncode == 0
    finally:
        if background_pid > 0:
            try:
                os.kill(background_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


@pytest.mark.asyncio
async def test_guardian_reports_child_exec_failure_without_hanging(
    tmp_path: Path,
) -> None:
    process = await start_guardian(str(tmp_path / "missing"), cwd=tmp_path)
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=1)

    assert stdout == b""
    assert b"could not launch" in stderr
    assert process.returncode == 127


@pytest.mark.asyncio
async def test_guardian_mirrors_child_signal_status(tmp_path: Path) -> None:
    process = await start_guardian(
        sys.executable,
        "-c",
        "import os, signal; os.kill(os.getpid(), signal.SIGKILL)",
        cwd=tmp_path,
    )

    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=1)

    assert stdout == b""
    assert stderr == b""
    assert process.returncode == -signal.SIGKILL


def test_natural_drain_uses_bounded_adaptive_membership_polling() -> None:
    membership = iter((True, True, True, True, True, False))
    delays: list[float] = []

    guardian._wait_for_session_drain(  # type: ignore[attr-defined]
        101,
        member_check=lambda session_id: next(membership),
        wait=delays.append,
    )

    assert delays == [0.05, 0.1, 0.2, 0.4, 0.5]


def test_sentinel_pins_real_process_group_until_release_and_is_reaped() -> None:
    leader_reader, leader_writer = os.pipe()
    ready_reader, ready_writer = os.pipe()
    leader_pid = os.fork()
    if leader_pid == 0:
        os.close(leader_writer)
        os.close(ready_reader)
        os.setpgid(0, 0)
        os.write(ready_writer, b"1")
        os.close(ready_writer)
        os.read(leader_reader, 1)
        os.close(leader_reader)
        os._exit(0)

    os.close(leader_reader)
    os.close(ready_writer)
    sentinel = None
    try:
        assert os.read(ready_reader, 1) == b"1"
        sentinel = guardian._pin_process_group(leader_pid)  # type: ignore[attr-defined]
        assert sentinel is not None

        os.close(leader_writer)
        leader_writer = -1
        os.waitpid(leader_pid, 0)

        assert os.getpgid(sentinel.pid) == leader_pid
        sentinel.release()
        with pytest.raises(ChildProcessError):
            os.waitpid(sentinel.pid, os.WNOHANG)
    finally:
        os.close(ready_reader)
        if leader_writer >= 0:
            os.close(leader_writer)
        try:
            os.kill(leader_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(leader_pid, 0)
        except ChildProcessError:
            pass
        if sentinel is not None:
            sentinel.release()


def test_stopped_sentinel_release_is_bounded_and_reaped() -> None:
    leader_reader, leader_writer = os.pipe()
    ready_reader, ready_writer = os.pipe()
    leader_pid = os.fork()
    if leader_pid == 0:
        os.close(leader_writer)
        os.close(ready_reader)
        os.setpgid(0, 0)
        os.write(ready_writer, b"1")
        os.close(ready_writer)
        os.read(leader_reader, 1)
        os.close(leader_reader)
        os._exit(0)

    os.close(leader_reader)
    os.close(ready_writer)
    sentinel = None
    release_thread = None
    try:
        assert os.read(ready_reader, 1) == b"1"
        sentinel = guardian._pin_process_group(leader_pid)  # type: ignore[attr-defined]
        assert sentinel is not None
        os.kill(sentinel.pid, signal.SIGSTOP)
        stopped_pid, status = os.waitpid(sentinel.pid, os.WUNTRACED)
        assert stopped_pid == sentinel.pid
        assert os.WIFSTOPPED(status)

        release_thread = threading.Thread(target=sentinel.release)
        release_thread.start()
        release_thread.join(timeout=0.1)
        completed_while_stopped = not release_thread.is_alive()
        if release_thread.is_alive():
            os.kill(sentinel.pid, signal.SIGCONT)
            release_thread.join(timeout=1)

        assert completed_while_stopped
        assert not release_thread.is_alive()
        with pytest.raises(ChildProcessError):
            os.waitpid(sentinel.pid, os.WNOHANG)
    finally:
        if (
            release_thread is not None
            and release_thread.is_alive()
            and sentinel is not None
        ):
            os.kill(sentinel.pid, signal.SIGCONT)
            release_thread.join(timeout=1)
        os.close(ready_reader)
        os.close(leader_writer)
        try:
            os.kill(leader_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(leader_pid, 0)
        if sentinel is not None:
            sentinel.release()


@pytest.mark.parametrize("termination_signal", [signal.SIGTERM, signal.SIGKILL])
def test_pinned_groups_never_signal_failed_pin_and_signal_guardian_last(
    termination_signal: signal.Signals,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Sentinel:
        def release(self) -> None:
            pass

    sentinels = {202: Sentinel(), 404: Sentinel()}
    attempted: list[int] = []

    def pin(group: int) -> Sentinel | None:
        attempted.append(group)
        return sentinels.get(group)

    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(guardian, "_pin_process_group", pin, raising=False)
    monkeypatch.setattr(os, "killpg", lambda group, sig: signals.append((group, sig)))
    groups = guardian._PinnedGroups(101)  # type: ignore[attr-defined]

    groups.discover({101, 202, 303, 404})
    groups.signal(termination_signal)

    assert attempted == [202, 303, 404]
    assert signals == [
        (202, termination_signal),
        (404, termination_signal),
        (101, termination_signal),
    ]


def test_termination_inspection_failure_reports_and_exits_without_signaling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RunningChild:
        def poll(self) -> None:
            return None

    status_reader, status_writer = os.pipe()
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        guardian, "_live_session_members", lambda session_id, excluded: None
    )
    monkeypatch.setattr(os, "killpg", lambda group, sig: signals.append((group, sig)))
    try:
        with pytest.raises(guardian._TerminationFailure):  # type: ignore[attr-defined]
            guardian._terminate_owned_session(  # type: ignore[arg-type]
                RunningChild(),
                os.getsid(0),
                status_writer,
            )
        assert read_descriptor_line(status_reader) == b"failed process inspection\n"
        assert signals == []
    finally:
        os.close(status_reader)
        os.close(status_writer)


@pytest.mark.asyncio
async def test_controlled_termination_pins_and_escalates_all_groups(
    tmp_path: Path,
) -> None:
    child_pid_path = tmp_path / "child-pid"
    child = tmp_path / "controlled-child"
    child.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import signal\n"
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "member = subprocess.Popen(\n"
        "    [sys.executable, '-c', "
        "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'],\n"
        "    preexec_fn=os.setpgrp,\n"
        ")\n"
        "Path(sys.argv[1]).write_text(str(member.pid))\n"
        "while True:\n"
        "    time.sleep(1)\n"
    )
    child.chmod(child.stat().st_mode | stat.S_IXUSR)
    process, control_writer, status_reader = await start_controlled_guardian(
        str(child),
        str(child_pid_path),
        cwd=tmp_path,
    )
    member_pid = -1
    try:
        assert await asyncio.to_thread(read_descriptor_line, status_reader) == b"ok\n"
        async with asyncio.timeout(1):
            while not child_pid_path.exists():
                await asyncio.sleep(0.005)
        member_pid = int(child_pid_path.read_text())

        os.write(control_writer, b"terminate\n")
        status = await asyncio.wait_for(
            asyncio.to_thread(read_descriptor_line, status_reader),
            timeout=3,
        )
        await asyncio.wait_for(process.wait(), timeout=1)

        assert status == b"escalating\n"
        assert process.returncode == -signal.SIGKILL
        assert guardian._process_is_gone(member_pid)  # type: ignore[attr-defined]
    finally:
        os.close(control_writer)
        os.close(status_reader)
        if member_pid > 0:
            try:
                os.killpg(member_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


@pytest.mark.asyncio
async def test_startup_termination_before_start_never_launches_child(
    tmp_path: Path,
) -> None:
    launch_record = tmp_path / "launched"
    process, control_writer, status_reader = await start_controlled_guardian(
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(launch_record)!r}).touch()",
        cwd=tmp_path,
        start=False,
    )
    try:
        os.write(control_writer, b"terminate\n")

        status = await asyncio.wait_for(
            asyncio.to_thread(read_descriptor_line, status_reader),
            timeout=4,
        )
        await asyncio.wait_for(process.wait(), timeout=1)

        assert status == b"escalating\n"
        assert process.returncode == -signal.SIGKILL
        assert not launch_record.exists()
    finally:
        os.close(control_writer)
        os.close(status_reader)
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


@pytest.mark.parametrize(
    "stale_initial_snapshot",
    (False, True),
    ids=("visible-child", "empty-snapshot-before-child"),
)
def test_startup_watchdog_cleans_child_when_guardian_stalls_before_ok(
    stale_initial_snapshot: bool,
) -> None:
    status_reader, status_writer = os.pipe()
    control_reader, control_writer = os.pipe()
    spawned_reader, spawned_writer = os.pipe()
    guardian_pid = os.fork()
    if guardian_pid == 0:
        os.close(status_reader)
        os.close(control_writer)
        os.close(spawned_reader)
        os.setsid()
        real_popen = guardian.subprocess.Popen
        real_member_loader = guardian._live_session_members_forked  # type: ignore[attr-defined]

        if stale_initial_snapshot:
            first_snapshot = True

            def load_members_after_stale_snapshot(
                session_id: int,
                excluded_pids: set[int],
            ) -> dict[int, int] | None:
                nonlocal first_snapshot
                if first_snapshot:
                    first_snapshot = False
                    return {}
                return real_member_loader(session_id, excluded_pids)

            guardian._live_session_members_forked = load_members_after_stale_snapshot  # type: ignore[attr-defined]

        def spawn_then_stall(
            *args: object, **kwargs: object
        ) -> subprocess.Popen[bytes]:
            process = real_popen(*args, **kwargs)
            os.write(spawned_writer, f"{process.pid}\n".encode())
            os.kill(os.getpid(), signal.SIGSTOP)
            return process

        guardian.subprocess.Popen = spawn_then_stall  # type: ignore[assignment]
        returncode = guardian.main(
            (
                "--status-fd",
                str(status_writer),
                "--control-fd",
                str(control_reader),
                "--",
                sys.executable,
                "-c",
                "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
            )
        )
        os._exit(returncode)

    os.close(status_writer)
    os.close(control_reader)
    os.close(spawned_writer)
    child_pid = -1
    try:
        assert read_descriptor_line_before(status_reader, 0.5) == b"armed\n"
        os.write(control_writer, b"start\n")
        child_pid = int(read_descriptor_line_before(spawned_reader, 1))
        os.write(control_writer, b"terminate\n")

        assert read_descriptor_line_before(status_reader, 4) == b"escalating\n"
        reaped_pid, status = os.waitpid(guardian_pid, 0)
        assert reaped_pid == guardian_pid
        assert os.WIFSIGNALED(status)
        assert os.WTERMSIG(status) == signal.SIGKILL
        assert guardian._process_is_gone(child_pid)  # type: ignore[attr-defined]
    finally:
        os.close(status_reader)
        os.close(control_writer)
        os.close(spawned_reader)
        if child_pid > 0:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            os.kill(guardian_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(guardian_pid, 0)
        except ChildProcessError:
            pass


# --- process inspection portability (BusyBox ps has no -a/-x/-p flags) ---


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="requires /proc")
def test_proc_snapshot_reports_live_child_with_kernel_process_group() -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        process_group=0,
    )
    try:
        members = guardian._live_session_members(os.getsid(0))
        assert members is not None
        assert members[child.pid] == os.getpgid(child.pid) == child.pid
    finally:
        child.kill()
        child.wait()


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="requires /proc")
def test_snapshot_excludes_zombies_and_state_reports_reaped_as_gone() -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = guardian._process_state_forked(child.pid)
            if state is not None and state.startswith("Z"):
                break
            time.sleep(0.01)
        else:
            raise AssertionError("child never became a zombie")
        members = guardian._live_session_members(os.getsid(0))
        assert members is not None
        assert child.pid not in members
        assert guardian._process_is_gone(child.pid)
    finally:
        child.wait()
    assert guardian._process_state_forked(child.pid) == ""
    assert guardian._process_is_gone(child.pid)
    own_state = guardian._process_state_forked(os.getpid())
    assert own_state
    assert not own_state.startswith("Z")
    assert not guardian._process_is_gone(os.getpid())


def test_ps_fallback_snapshot_uses_portable_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []

    class Result:
        returncode = 0
        stdout = "1 1 S\n"

    def run(argv: tuple[str, ...], **kwargs: object) -> Result:
        commands.append(tuple(argv))
        return Result()

    monkeypatch.setattr(guardian.subprocess, "run", run)

    assert guardian._ps_table_snapshot() == "1 1 S\n"
    # BusyBox ps rejects -a/-x/-p; -Ao is accepted by BusyBox, procps,
    # and the BSDs alike.
    assert commands == [("/bin/ps", "-Ao", "pid=,pgid=,stat=")]


def test_member_inspection_failure_raises_instead_of_reporting_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        guardian,
        "process_table_snapshot",
        lambda: None,
    )

    with pytest.raises(guardian._TerminationFailure):  # type: ignore[attr-defined]
        guardian._session_has_other_live_members(101)  # type: ignore[attr-defined]


def test_drain_gives_up_loudly_when_inspection_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def failing_member_check(session_id: int) -> bool:
        raise guardian._TerminationFailure("process inspection")  # type: ignore[attr-defined]

    guardian._drain_session_or_give_up(  # type: ignore[attr-defined]
        101,
        member_check=failing_member_check,
    )

    assert "process inspection" in capsys.readouterr().err


def read_fifo_line(descriptor: int, timeout: float = 5.0) -> bytes:
    """Read one line from a FIFO opened O_NONBLOCK before any writer connects.

    A FIFO read end opened O_NONBLOCK before a writer has attached returns
    an immediate zero-length read (and later, EAGAIN) rather than blocking
    for real data — plain os.read/read_descriptor_line calls issued right
    after open() can race the guardian subprocess's own FIFO open and
    misread that transient state as EOF. Poll with a short backoff instead.
    """

    deadline = time.monotonic() + timeout
    data = bytearray()
    while not data.endswith(b"\n"):
        try:
            chunk = os.read(descriptor, 1)
        except BlockingIOError:
            chunk = None
        if chunk:
            data.extend(chunk)
            continue
        if time.monotonic() >= deadline:
            break
        time.sleep(0.005)
    return bytes(data)


@pytest.mark.asyncio
async def test_guardian_path_mode_handshake_pid_and_exit_status(
    tmp_path: Path,
) -> None:
    status_path = tmp_path / "status.fifo"
    control_path = tmp_path / "control.fifo"
    os.mkfifo(status_path, 0o600)
    os.mkfifo(control_path, 0o600)
    status_reader = os.open(status_path, os.O_RDONLY | os.O_NONBLOCK)
    control_writer = os.open(control_path, os.O_RDWR)
    guardian_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "tdb"
        / "adapters"
        / "tcsh"
        / "guardian.py"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(guardian_path),
        "--status-path",
        str(status_path),
        "--control-path",
        str(control_path),
        "--",
        "/bin/sh",
        "-c",
        "exit 3",
        cwd=str(tmp_path),
        stdin=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        line = await asyncio.wait_for(
            asyncio.to_thread(read_fifo_line, status_reader), 5
        )
        assert line == b"armed\n"
        os.write(control_writer, b"start\n")
        assert (
            await asyncio.wait_for(
                asyncio.to_thread(read_fifo_line, status_reader), 5
            )
            == b"ok\n"
        )
        pid_line = await asyncio.wait_for(
            asyncio.to_thread(read_fifo_line, status_reader), 5
        )
        assert pid_line == f"pid {process.pid}\n".encode()
        assert (
            await asyncio.wait_for(
                asyncio.to_thread(read_fifo_line, status_reader), 5
            )
            == b"exit 3\n"
        )
        assert await asyncio.wait_for(process.wait(), 5) == 3
    finally:
        os.close(status_reader)
        os.close(control_writer)
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.asyncio
async def test_guardian_path_mode_reports_signal_death(
    tmp_path: Path,
) -> None:
    status_path = tmp_path / "status.fifo"
    control_path = tmp_path / "control.fifo"
    os.mkfifo(status_path, 0o600)
    os.mkfifo(control_path, 0o600)
    status_reader = os.open(status_path, os.O_RDONLY | os.O_NONBLOCK)
    control_writer = os.open(control_path, os.O_RDWR)
    guardian_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "tdb"
        / "adapters"
        / "tcsh"
        / "guardian.py"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(guardian_path),
        "--status-path",
        str(status_path),
        "--control-path",
        str(control_path),
        "--",
        "/bin/sh",
        "-c",
        "kill -KILL $$",
        cwd=str(tmp_path),
        stdin=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        line = await asyncio.wait_for(
            asyncio.to_thread(read_fifo_line, status_reader), 5
        )
        assert line == b"armed\n"
        os.write(control_writer, b"start\n")
        assert (
            await asyncio.wait_for(
                asyncio.to_thread(read_fifo_line, status_reader), 5
            )
            == b"ok\n"
        )
        pid_line = await asyncio.wait_for(
            asyncio.to_thread(read_fifo_line, status_reader), 5
        )
        assert pid_line == f"pid {process.pid}\n".encode()
        assert (
            await asyncio.wait_for(
                asyncio.to_thread(read_fifo_line, status_reader), 5
            )
            == b"signal 9\n"
        )
        assert await asyncio.wait_for(process.wait(), 5) == -signal.SIGKILL
    finally:
        os.close(status_reader)
        os.close(control_writer)
        if process.returncode is None:
            process.kill()
            await process.wait()


def test_drain_or_termination_returns_child_code_when_inspection_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    control_reader, control_writer = os.pipe()

    def failing_member_check(session_id: int) -> bool:
        raise guardian._TerminationFailure("process inspection")  # type: ignore[attr-defined]

    try:
        returncode = guardian._wait_for_drain_or_termination(  # type: ignore[attr-defined]
            guardian._StartupChild(),  # type: ignore[attr-defined, arg-type]
            101,
            control_reader,
            None,
            7,
            member_check=failing_member_check,
        )
    finally:
        os.close(control_reader)
        os.close(control_writer)

    assert returncode == 7
    assert "process inspection" in capsys.readouterr().err

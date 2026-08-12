"""Keep a debuggee's POSIX session generation alive until all members exit."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence

_INITIAL_MEMBERSHIP_POLL_SECONDS = 0.05
_MAX_MEMBERSHIP_POLL_SECONDS = 0.5
_CHILD_POLL_SECONDS = 0.1
_TERMINATE_SECONDS = 2.0
_FREEZE_SECONDS = 0.5

MemberCheck = Callable[[int], bool]
MemberLoader = Callable[[int, set[int]], dict[int, int] | None]
Wait = Callable[[float], None]


class _TerminationFailure(Exception):
    pass


class _StartupChild:
    """Minimal child facade for watchdog-owned session termination."""

    @staticmethod
    def wait() -> int:
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run one child in this guardian-owned session and mirror its exit status."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    status_descriptor: int | None = None
    control_descriptor: int | None = None
    while len(arguments) >= 2 and arguments[0] in {"--status-fd", "--control-fd"}:
        option, value = arguments[:2]
        try:
            descriptor = int(value)
        except ValueError:
            descriptor = None
        if option == "--status-fd":
            status_descriptor = descriptor
        else:
            control_descriptor = descriptor
        del arguments[:2]
    if arguments[:1] == ["--"]:
        arguments.pop(0)
    if not arguments:
        message = "guardian could not launch child: missing command"
        _report_status(status_descriptor, f"error {message}")
        print(message, file=sys.stderr)
        return 127

    signal.signal(signal.SIGTERM, _retain_session)
    watchdog: tuple[int, int, int] | None = None
    if status_descriptor is not None and control_descriptor is not None:
        watchdog = _arm_startup_watchdog(status_descriptor, control_descriptor)
        if os.read(watchdog[1], 1) != b"1":
            _close_startup_watchdog(watchdog)
            os.waitpid(watchdog[0], 0)
            return 125
    try:
        child = subprocess.Popen(arguments, process_group=0)
    except OSError as error:
        message = f"guardian could not launch {arguments[0]}: {error}"
        _report_status(status_descriptor, f"error {message}")
        print(message, file=sys.stderr)
        if watchdog is not None:
            _handoff_startup_watchdog(watchdog)
        return 127
    _report_status(status_descriptor, "ok", close=control_descriptor is None)
    if watchdog is not None:
        _handoff_startup_watchdog(watchdog)

    session_id = os.getsid(0)
    if control_descriptor is None:
        returncode = child.wait()
        _wait_for_session_drain(session_id)
    else:
        try:
            returncode = _wait_for_child_or_termination(
                child,
                session_id,
                control_descriptor,
                status_descriptor,
            )
        except _TerminationFailure:
            return 125
    if returncode < 0:
        child_signal = -returncode
        if child_signal != signal.SIGKILL:
            signal.signal(child_signal, signal.SIG_DFL)
        os.kill(os.getpid(), child_signal)
        return 128 + child_signal
    return returncode


def _arm_startup_watchdog(
    status_descriptor: int,
    control_descriptor: int,
) -> tuple[int, int, int]:
    startup_reader, startup_writer = os.pipe()
    handoff_reader, handoff_writer = os.pipe()
    acknowledgement_reader, acknowledgement_writer = os.pipe()
    watchdog_pid = os.fork()
    if watchdog_pid == 0:
        os.close(startup_reader)
        os.close(handoff_writer)
        os.close(acknowledgement_reader)
        try:
            _run_startup_watchdog(
                status_descriptor,
                control_descriptor,
                startup_writer,
                handoff_reader,
                acknowledgement_writer,
            )
        finally:
            os._exit(0)
    os.close(startup_writer)
    os.close(handoff_reader)
    os.close(acknowledgement_writer)
    return watchdog_pid, startup_reader, handoff_writer, acknowledgement_reader


def _run_startup_watchdog(
    status_descriptor: int,
    control_descriptor: int,
    startup_descriptor: int,
    handoff_descriptor: int,
    acknowledgement_descriptor: int,
) -> None:
    os.setpgid(0, 0)
    _report_status(status_descriptor, "armed", close=False)
    control_buffer = bytearray()
    started = False
    while True:
        watched_descriptors = (
            (handoff_descriptor,)
            if control_descriptor < 0
            else (control_descriptor, handoff_descriptor)
        )
        readable, _, _ = select.select(
            watched_descriptors,
            (),
            (),
        )
        if control_descriptor in readable:
            chunk = os.read(control_descriptor, 64)
            if not chunk:
                if not started:
                    return
                control_descriptor = -1
            else:
                control_buffer.extend(chunk)
                while (newline := control_buffer.find(b"\n")) >= 0:
                    command = bytes(control_buffer[: newline + 1])
                    del control_buffer[: newline + 1]
                    if command == b"start\n" and not started:
                        started = True
                        os.write(startup_descriptor, b"1")
                    elif command == b"terminate\n":
                        _terminate_startup_session(status_descriptor)
                    else:
                        _report_status(
                            status_descriptor,
                            "failed invalid guardian command",
                            close=False,
                        )
        if handoff_descriptor in readable:
            if control_descriptor >= 0 and select.select((control_descriptor,), (), (), 0)[0]:
                continue
            if control_descriptor >= 0:
                os.close(control_descriptor)
            os.write(acknowledgement_descriptor, b"1")
            return


def _terminate_startup_session(status_descriptor: int) -> None:
    session_id = os.getsid(0)
    startup_owner_pid = os.getppid()
    try:
        _freeze_startup_owner(startup_owner_pid, session_id, status_descriptor)
        _terminate_owned_session(
            _StartupChild(),  # type: ignore[arg-type]
            session_id,
            status_descriptor,
            member_loader=_live_session_members_forked,
            force_escalation=True,
            startup_owner_pid=startup_owner_pid,
        )
    finally:
        try:
            os.killpg(os.getpgrp(), signal.SIGKILL)
        except ProcessLookupError:
            pass


def _freeze_startup_owner(
    owner_pid: int,
    session_id: int,
    status_descriptor: int | None,
) -> None:
    try:
        if os.getsid(owner_pid) != session_id or os.getpgid(owner_pid) != owner_pid:
            _fail_termination(status_descriptor, "startup owner generation")
        os.kill(owner_pid, signal.SIGSTOP)
    except ProcessLookupError:
        return

    deadline = time.monotonic() + _FREEZE_SECONDS
    while time.monotonic() < deadline:
        state = _process_state_forked(owner_pid)
        if state is None:
            try:
                os.getsid(owner_pid)
            except ProcessLookupError:
                return
            _fail_termination(status_descriptor, "startup owner inspection")
        if not state or state.startswith("Z"):
            return
        if state.startswith("T"):
            return
        time.sleep(_INITIAL_MEMBERSHIP_POLL_SECONDS)
    _fail_termination(status_descriptor, "to freeze startup owner")


def _handoff_startup_watchdog(watchdog: tuple[int, int, int]) -> None:
    watchdog_pid, _, handoff_descriptor, acknowledgement_descriptor = watchdog
    try:
        os.write(handoff_descriptor, b"1")
        if os.read(acknowledgement_descriptor, 1) != b"1":
            raise _TerminationFailure("startup watchdog handoff")
    finally:
        _close_startup_watchdog(watchdog)
        os.waitpid(watchdog_pid, 0)


def _close_startup_watchdog(watchdog: tuple[int, int, int]) -> None:
    for descriptor in watchdog[1:]:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _retain_session(signum: int, frame: object) -> None:
    """Keep the SID allocated while adapter-owned members finish or are escalated."""

    del signum, frame


def _report_status(descriptor: int | None, status: str, *, close: bool = True) -> None:
    if descriptor is None:
        return
    try:
        data = (status + "\n").encode()
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view) :]
    except OSError:
        pass
    finally:
        if close:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _wait_for_session_drain(
    session_id: int,
    *,
    member_check: MemberCheck = lambda session_id: _session_has_other_live_members(session_id),
    wait: Wait = time.sleep,
) -> None:
    delay = _INITIAL_MEMBERSHIP_POLL_SECONDS
    while member_check(session_id):
        wait(delay)
        delay = min(delay * 2, _MAX_MEMBERSHIP_POLL_SECONDS)


class _Sentinel:
    __slots__ = ("_release_descriptor", "_released", "pid")

    def __init__(self, pid: int, release_descriptor: int) -> None:
        self.pid = pid
        self._release_descriptor = release_descriptor
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            os.close(self._release_descriptor)
        except OSError:
            pass
        try:
            os.kill(self.pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
        while True:
            try:
                os.waitpid(self.pid, 0)
                return
            except InterruptedError:
                continue
            except ChildProcessError:
                return


def _pin_process_group(process_group_id: int) -> _Sentinel | None:
    release_reader, release_writer = os.pipe()
    ready_reader, ready_writer = os.pipe()
    inherited_descriptors = _open_descriptors()
    sentinel_pid = os.fork()
    if sentinel_pid == 0:
        for descriptor in inherited_descriptors:
            if descriptor not in {release_reader, ready_writer}:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        started = os.read(release_reader, 1)
        if started and os.getpgrp() == process_group_id:
            os.write(ready_writer, b"1")
            os.close(ready_writer)
            os.read(release_reader, 1)
        os.close(release_reader)
        os._exit(0)

    os.close(release_reader)
    os.close(ready_writer)
    try:
        os.setpgid(sentinel_pid, process_group_id)
        os.write(release_writer, b"1")
        if os.read(ready_reader, 1) != b"1":
            raise ProcessLookupError(process_group_id)
    except OSError:
        os.close(release_writer)
        try:
            os.kill(sentinel_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(sentinel_pid, 0)
        return None
    finally:
        os.close(ready_reader)
    return _Sentinel(sentinel_pid, release_writer)


def _open_descriptors() -> tuple[int, ...]:
    for descriptor_directory in ("/dev/fd", "/proc/self/fd"):
        try:
            return tuple(
                int(name)
                for name in os.listdir(descriptor_directory)
                if name.isdecimal() and int(name) >= 3
            )
        except OSError:
            continue
    return ()


class _PinnedGroups:
    __slots__ = ("guardian_group", "sentinels")

    def __init__(self, guardian_group: int) -> None:
        self.guardian_group = guardian_group
        self.sentinels: dict[int, _Sentinel] = {}

    def discover(self, process_groups: set[int]) -> bool:
        complete = True
        for process_group in sorted(process_groups - {self.guardian_group}):
            if process_group in self.sentinels:
                continue
            sentinel = _pin_process_group(process_group)
            if sentinel is None:
                complete = False
                continue
            self.sentinels[process_group] = sentinel
        return complete

    def signal(self, sig: signal.Signals) -> None:
        self.signal_members(sig)
        try:
            os.killpg(self.guardian_group, sig)
        except ProcessLookupError:
            pass

    def signal_members(self, sig: signal.Signals) -> None:
        for process_group in sorted(self.sentinels):
            try:
                os.killpg(process_group, sig)
            except ProcessLookupError:
                pass

    def release(self) -> None:
        for sentinel in self.sentinels.values():
            sentinel.release()
        self.sentinels.clear()


def _wait_for_child_or_termination(
    child: subprocess.Popen[bytes],
    session_id: int,
    control_descriptor: int,
    status_descriptor: int | None,
) -> int:
    try:
        while child.poll() is None:
            readable, _, _ = select.select(
                (control_descriptor,),
                (),
                (),
                _CHILD_POLL_SECONDS,
            )
            if not readable:
                continue
            command = os.read(control_descriptor, 64)
            if not command:
                control_descriptor = -1
                while child.poll() is None:
                    time.sleep(_CHILD_POLL_SECONDS)
                break
            if command == b"terminate\n":
                return _terminate_owned_session(child, session_id, status_descriptor)
            _report_status(status_descriptor, "failed invalid guardian command", close=False)
        returncode = child.wait()
        if control_descriptor < 0:
            _wait_for_session_drain(session_id)
            return returncode
        return _wait_for_drain_or_termination(
            child,
            session_id,
            control_descriptor,
            status_descriptor,
            returncode,
        )
    finally:
        if control_descriptor >= 0:
            try:
                os.close(control_descriptor)
            except OSError:
                pass


def _wait_for_drain_or_termination(
    child: subprocess.Popen[bytes],
    session_id: int,
    control_descriptor: int,
    status_descriptor: int | None,
    returncode: int,
    *,
    member_check: MemberCheck = lambda session_id: _session_has_other_live_members(session_id),
) -> int:
    """Retain the control channel while members outlive the reaped root child."""

    delay = _INITIAL_MEMBERSHIP_POLL_SECONDS
    while member_check(session_id):
        readable, _, _ = select.select((control_descriptor,), (), (), delay)
        if not readable:
            delay = min(delay * 2, _MAX_MEMBERSHIP_POLL_SECONDS)
            continue
        command = os.read(control_descriptor, 64)
        if not command:
            _wait_for_session_drain(session_id, member_check=member_check)
            return returncode
        if command == b"terminate\n":
            return _terminate_owned_session(child, session_id, status_descriptor)
        _report_status(status_descriptor, "failed invalid guardian command", close=False)
    return returncode


def _terminate_owned_session(
    child: subprocess.Popen[bytes],
    session_id: int,
    status_descriptor: int | None,
    *,
    member_loader: MemberLoader | None = None,
    force_escalation: bool = False,
    startup_owner_pid: int | None = None,
) -> int:
    load_members = _live_session_members if member_loader is None else member_loader
    guardian_group = os.getpgrp()
    pinned = _PinnedGroups(guardian_group)
    observed_member_pids: set[int] = set()
    consecutive_empty_snapshots = 0
    deadline = time.monotonic() + _TERMINATE_SECONDS
    try:
        while time.monotonic() < deadline:
            members = load_members(
                session_id,
                {sentinel.pid for sentinel in pinned.sentinels.values()},
            )
            if members is None:
                _fail_termination(status_descriptor, "process inspection")
            target_groups = set(members.values()) - {guardian_group}
            observed_member_pids.update(
                pid for pid, group in members.items() if group in target_groups
            )
            pinned.discover(target_groups)
            if not target_groups:
                if force_escalation:
                    consecutive_empty_snapshots += 1
                    if consecutive_empty_snapshots < 2:
                        time.sleep(_INITIAL_MEMBERSHIP_POLL_SECONDS)
                        continue
                    pinned.release()
                    _complete_startup_escalation(
                        status_descriptor,
                        startup_owner_pid,
                        observed_member_pids,
                    )
                    return 128 + signal.SIGKILL
                returncode = child.wait()
                pinned.release()
                _report_status(status_descriptor, "terminated", close=False)
                return returncode
            consecutive_empty_snapshots = 0
            pinned.signal_members(signal.SIGTERM)
            time.sleep(_INITIAL_MEMBERSHIP_POLL_SECONDS)

        freeze_deadline = time.monotonic() + _FREEZE_SECONDS
        while time.monotonic() < freeze_deadline:
            sentinel_pids = {sentinel.pid for sentinel in pinned.sentinels.values()}
            members = load_members(session_id, sentinel_pids)
            if members is None:
                _fail_termination(status_descriptor, "process inspection")
            target_groups = set(members.values()) - {guardian_group}
            complete = pinned.discover(target_groups)
            pinned.signal_members(signal.SIGSTOP)
            sentinel_pids = {sentinel.pid for sentinel in pinned.sentinels.values()}
            frozen_members = load_members(session_id, sentinel_pids)
            if frozen_members is None:
                _fail_termination(status_descriptor, "process inspection")
            if complete and set(frozen_members.values()) - {guardian_group} <= set(pinned.sentinels):
                break
        else:
            _fail_termination(status_descriptor, "to freeze owned groups")

        pinned.signal_members(signal.SIGKILL)
        child.wait()
        pinned.release()
        if force_escalation:
            _complete_startup_escalation(
                status_descriptor,
                startup_owner_pid,
                observed_member_pids,
            )
            return 128 + signal.SIGKILL
        _report_status(status_descriptor, "escalating", close=False)
        os.killpg(guardian_group, signal.SIGKILL)
        return 128 + signal.SIGKILL
    finally:
        pinned.release()


def _complete_startup_escalation(
    status_descriptor: int | None,
    owner_pid: int | None,
    member_pids: set[int],
) -> None:
    if owner_pid is not None:
        try:
            os.kill(owner_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + _FREEZE_SECONDS
    remaining = set(member_pids)
    while remaining and time.monotonic() < deadline:
        for process_id in tuple(remaining):
            try:
                os.kill(process_id, 0)
            except ProcessLookupError:
                remaining.remove(process_id)
        if remaining:
            time.sleep(_INITIAL_MEMBERSHIP_POLL_SECONDS)
    _report_status(status_descriptor, "escalating", close=False)


def _fail_termination(status_descriptor: int | None, detail: str) -> None:
    _report_status(status_descriptor, f"failed {detail}", close=False)
    raise _TerminationFailure(detail)


def _live_session_members(
    session_id: int,
    excluded_pids: set[int] | None = None,
) -> dict[int, int] | None:
    excluded = set() if excluded_pids is None else excluded_pids
    try:
        result = subprocess.run(
            ("/bin/ps", "-axo", "pid=,pgid=,stat="),
            check=False,
            capture_output=True,
            text=True,
            start_new_session=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return _parse_live_session_members(result.stdout, session_id, excluded)


def _live_session_members_forked(
    session_id: int,
    excluded_pids: set[int],
) -> dict[int, int] | None:
    output_reader, output_writer = os.pipe()
    process_id = os.fork()
    if process_id == 0:
        try:
            os.dup2(output_writer, 1)
            os.close(output_reader)
            os.close(output_writer)
            os.execl("/bin/ps", "ps", "-axo", "pid=,pgid=,stat=")
        finally:
            os._exit(127)
    os.close(output_writer)
    output = bytearray()
    try:
        while chunk := os.read(output_reader, 64 * 1024):
            output.extend(chunk)
    finally:
        os.close(output_reader)
    _, status = os.waitpid(process_id, 0)
    if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
        return None
    return _parse_live_session_members(
        output.decode(errors="replace"),
        session_id,
        excluded_pids,
    )


def _process_state_forked(process_id: int) -> str | None:
    output_reader, output_writer = os.pipe()
    reader_pid = os.fork()
    if reader_pid == 0:
        try:
            os.dup2(output_writer, 1)
            os.close(output_reader)
            os.close(output_writer)
            os.execl(
                "/bin/ps",
                "ps",
                "-o",
                "stat=",
                "-p",
                str(process_id),
            )
        finally:
            os._exit(127)
    os.close(output_writer)
    output = bytearray()
    try:
        while chunk := os.read(output_reader, 4096):
            output.extend(chunk)
    finally:
        os.close(output_reader)
    _, status = os.waitpid(reader_pid, 0)
    if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
        return None
    return output.decode(errors="replace").strip()


def _parse_live_session_members(
    output: str,
    session_id: int,
    excluded_pids: set[int],
) -> dict[int, int]:
    members: dict[int, int] = {}
    guardian_pid = os.getpid()
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 3:
            continue
        try:
            candidate_pid = int(fields[0])
            candidate_group = int(fields[1])
        except ValueError:
            continue
        if (
            candidate_pid <= 0
            or candidate_group <= 0
            or candidate_pid == guardian_pid
            or candidate_pid in excluded_pids
            or fields[2].startswith("Z")
        ):
            continue
        try:
            if os.getsid(candidate_pid) == session_id:
                members[candidate_pid] = candidate_group
        except OSError:
            continue
    return members


def _session_has_other_live_members(session_id: int) -> bool:
    members = _live_session_members(session_id)
    if members is None:
        return True
    return bool(members)


if __name__ == "__main__":
    raise SystemExit(main())

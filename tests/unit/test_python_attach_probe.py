"""Target probing and language detection for `tdb -a PID` on Python."""

from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import Mock

import pytest

from tdb.languages import registry
from tdb.languages.base import LanguageNotSupportedError
from tdb.languages.python_attach import (
    TargetInfo,
    _trusted_to_run,
    already_debugged,
    is_python_executable,
    pid_exists,
    probe_target,
    uses_musl,
)

MODULE = "tdb.languages.python_attach"


@pytest.fixture
def alive(monkeypatch):
    monkeypatch.setattr(f"{MODULE}.pid_exists", lambda pid: True)
    monkeypatch.setattr(f"{MODULE}.already_debugged", lambda pid: False)


def _stub_probe_output(monkeypatch, tmp_path, stdout):
    """Make the target look like an interpreter that prints `stdout`."""
    exe = tmp_path / "python3"
    exe.write_bytes(b"")
    monkeypatch.setattr(f"{MODULE}._exe_of_pid", lambda pid: str(exe))
    monkeypatch.setattr(
        f"{MODULE}.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=stdout, stderr=""),
    )
    return exe


@pytest.mark.parametrize(
    "name",
    ["python", "python3", "python3.12", "python3.14t", "python.exe", "pypy3"],
)
def test_python_names_detected(name):
    assert is_python_executable(f"/usr/bin/{name}")


@pytest.mark.parametrize("name", ["uwsgi", "pythonista", "python3.12 (deleted)", "gdb"])
def test_other_names_rejected(name):
    assert not is_python_executable(f"/usr/bin/{name}")


def test_detect_executable_python_before_elf(tmp_path):
    exe = tmp_path / "python3.12"
    exe.write_bytes(b"\x7fELF" + b"\0" * 60)
    assert registry.detect_executable(str(exe)) == "python"


def test_probe_reads_current_interpreter(monkeypatch, alive):
    # sys.executable stands in for the target's interpreter. Its name may
    # be a venv symlink, but it is always a python.
    monkeypatch.setattr(f"{MODULE}._exe_of_pid", lambda pid: sys.executable)
    target = probe_target(4242)
    assert target.interpreter == sys.executable
    assert target.has_remote_exec == hasattr(sys, "remote_exec")


def test_probe_never_runs_non_python_exe(monkeypatch, tmp_path, alive):
    exe = tmp_path / "uwsgi"
    exe.write_bytes(b"")
    run = Mock()
    monkeypatch.setattr(f"{MODULE}.subprocess.run", run)
    monkeypatch.setattr(f"{MODULE}._exe_of_pid", lambda pid: str(exe))
    assert probe_target(4242) == TargetInfo(4242, None, False)
    run.assert_not_called()


def test_probe_deleted_interpreter_is_unknown(monkeypatch, alive):
    run = Mock()
    monkeypatch.setattr(f"{MODULE}.subprocess.run", run)
    monkeypatch.setattr(
        f"{MODULE}._exe_of_pid", lambda pid: "/usr/bin/python3.12 (deleted)"
    )
    assert probe_target(4242) == TargetInfo(4242, None, False)
    run.assert_not_called()


def test_probe_missing_file_is_unknown(monkeypatch, tmp_path, alive):
    run = Mock()
    monkeypatch.setattr(f"{MODULE}.subprocess.run", run)
    monkeypatch.setattr(
        f"{MODULE}._exe_of_pid", lambda pid: str(tmp_path / "python3")
    )
    assert probe_target(4242) == TargetInfo(4242, None, False)
    run.assert_not_called()


def test_probe_explicit_python_wins(monkeypatch, alive):
    exe_of_pid = Mock()
    monkeypatch.setattr(f"{MODULE}._exe_of_pid", exe_of_pid)
    assert probe_target(4242, sys.executable).interpreter == sys.executable
    exe_of_pid.assert_not_called()


def _python_named_file(tmp_path):
    ran = tmp_path / "ran"
    path = tmp_path / "python3"
    path.write_text(f"#!/bin/sh\ntouch {ran}\necho cpython 3 14 True\n")
    path.chmod(0o755)
    return path, ran


@pytest.mark.skipif(sys.platform == "win32", reason="runs a shell script")
def test_root_never_runs_a_binary_it_does_not_trust(monkeypatch, tmp_path, alive):
    path, ran = _python_named_file(tmp_path)
    monkeypatch.setattr(f"{MODULE}._exe_of_pid", lambda pid: str(path))
    monkeypatch.setattr(f"{MODULE}._euid", lambda: 0)
    assert probe_target(4242) == TargetInfo(4242, None, False)
    assert not ran.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="runs a shell script")
def test_root_runs_what_the_user_named_with_python_flag(monkeypatch, tmp_path, alive):
    path, ran = _python_named_file(tmp_path)
    monkeypatch.setattr(f"{MODULE}._euid", lambda: 0)
    assert probe_target(4242, str(path)).has_remote_exec
    assert ran.exists()


def test_a_root_owned_interpreter_is_trusted(monkeypatch):
    monkeypatch.setattr(
        f"{MODULE}.os.stat", lambda path: Mock(st_uid=0, st_mode=0o100755)
    )
    assert _trusted_to_run("/usr/bin/python3")


@pytest.mark.parametrize("uid, mode", ((1000, 0o100755), (0, 0o100775), (0, 0o100757)))
def test_writable_by_others_is_not_trusted(monkeypatch, uid, mode):
    monkeypatch.setattr(f"{MODULE}.os.stat", lambda path: Mock(st_uid=uid, st_mode=mode))
    assert not _trusted_to_run("/usr/bin/python3")


def test_a_writable_parent_is_not_trusted(monkeypatch):
    def stat(path):
        mode = 0o40777 if path == "/opt" else 0o100755
        return Mock(st_uid=0, st_mode=mode)

    monkeypatch.setattr(f"{MODULE}.os.stat", stat)
    monkeypatch.setattr(f"{MODULE}.os.path.realpath", lambda path: path)
    assert not _trusted_to_run("/opt/python3")


def test_probe_rejects_old_python(monkeypatch, tmp_path, alive):
    _stub_probe_output(monkeypatch, tmp_path, "cpython 3 10 False\n")
    with pytest.raises(LanguageNotSupportedError, match=r"3\.11"):
        probe_target(4242)


def test_probe_rejects_pypy(monkeypatch, tmp_path, alive):
    _stub_probe_output(monkeypatch, tmp_path, "pypy 3 11 False\n")
    with pytest.raises(LanguageNotSupportedError, match="CPython"):
        probe_target(4242)


def test_probe_failure_is_unknown(monkeypatch, tmp_path, alive):
    exe = _stub_probe_output(monkeypatch, tmp_path, "")
    monkeypatch.setattr(
        f"{MODULE}.subprocess.run", Mock(side_effect=OSError("exec format error"))
    )
    assert exe.exists()
    assert probe_target(4242) == TargetInfo(4242, None, False)


def test_probe_missing_pid(monkeypatch):
    monkeypatch.setattr(f"{MODULE}.pid_exists", lambda pid: False)
    with pytest.raises(LanguageNotSupportedError, match="4242"):
        probe_target(4242)


def test_pid_exists_never_signals_on_windows(monkeypatch):
    # os.kill(pid, 0) on Windows terminates the process.
    monkeypatch.setattr(f"{MODULE}.sys.platform", "win32")
    kill = Mock()
    monkeypatch.setattr(f"{MODULE}.os.kill", kill)
    assert pid_exists(4242) is True
    kill.assert_not_called()


def test_pid_exists_permission_error_means_alive(monkeypatch):
    monkeypatch.setattr(f"{MODULE}.sys.platform", "linux")
    monkeypatch.setattr(f"{MODULE}.os.kill", Mock(side_effect=PermissionError))
    assert pid_exists(4242) is True


def test_pid_exists_false_for_missing_process(monkeypatch):
    monkeypatch.setattr(f"{MODULE}.sys.platform", "linux")
    monkeypatch.setattr(f"{MODULE}.os.kill", Mock(side_effect=ProcessLookupError))
    assert pid_exists(4242) is False


TCP_HEADER = "  sl  local_address rem_address   st tx:rx tr:when retrnsmt uid timeout inode\n"


def _tcp_row(local, remote, state, inode):
    return (
        f"   0: 0100007F:{local:04X} 0100007F:{remote:04X} {state} "
        f"00000000:00000000 00:00000000 00000000  1000 0 {inode} 1\n"
    )


def _fake_proc(tmp_path, monkeypatch, *, processes, rows=(), parent=""):
    """A /proc with `processes` ({pid: (cmdline, socket inodes)}), a TCP
    table, and pid 4242 whose parent has command line `parent`."""
    proc = tmp_path / "proc"
    (proc / "net").mkdir(parents=True)
    (proc / "net" / "tcp").write_text(TCP_HEADER + "".join(rows))
    (proc / "net" / "tcp6").write_text(TCP_HEADER)
    for pid, (cmdline, inodes) in {4000: (parent, []), **processes}.items():
        (proc / str(pid) / "fd").mkdir(parents=True)
        (proc / str(pid) / "cmdline").write_bytes(cmdline.replace(" ", "\0").encode())
        for n, inode in enumerate(inodes):
            os.symlink(f"socket:[{inode}]", proc / str(pid) / "fd" / str(n))
    (proc / "4242" / "stat").write_text("4242 (a) b) S 4000 1 1 0")
    monkeypatch.setattr(f"{MODULE}.sys.platform", "linux")
    monkeypatch.setattr(f"{MODULE}._PROC", str(proc))


needs_symlinks = pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
ADAPTER = "/usr/bin/python /x/debugpy/adapter --for-server 1"


@needs_symlinks
def test_connection_to_adapter_means_already_debugged(tmp_path, monkeypatch):
    # debugpy.listen() daemonizes its adapter: it is no child of the target,
    # so the open connection between them is what ties them together.
    _fake_proc(
        tmp_path,
        monkeypatch,
        processes={4242: ("python app.py", [11]), 7000: (ADAPTER, [22])},
        rows=[_tcp_row(40000, 50000, "01", 11), _tcp_row(50000, 40000, "01", 22)],
    )
    assert already_debugged(4242)


@needs_symlinks
def test_module_style_adapter_detected(tmp_path, monkeypatch):
    _fake_proc(
        tmp_path,
        monkeypatch,
        processes={
            4242: ("python app.py", [11]),
            7000: ("python -m debugpy.adapter", [22]),
        },
        rows=[_tcp_row(40000, 50000, "01", 11), _tcp_row(50000, 40000, "01", 22)],
    )
    assert already_debugged(4242)


@needs_symlinks
def test_launcher_parent_means_already_debugged(tmp_path, monkeypatch):
    _fake_proc(
        tmp_path,
        monkeypatch,
        processes={4242: ("python app.py", [])},
        parent="python /x/debugpy/launcher 5678",
    )
    assert already_debugged(4242)


@needs_symlinks
def test_connection_to_other_program_is_not_debugged(tmp_path, monkeypatch):
    _fake_proc(
        tmp_path,
        monkeypatch,
        processes={4242: ("python app.py", [11]), 7000: ("redis-server", [22])},
        rows=[_tcp_row(40000, 6379, "01", 11), _tcp_row(6379, 40000, "01", 22)],
    )
    assert not already_debugged(4242)


@needs_symlinks
def test_unrelated_adapter_is_not_ours(tmp_path, monkeypatch):
    # Another session's adapter on this machine must not trip the check.
    _fake_proc(
        tmp_path,
        monkeypatch,
        processes={4242: ("python app.py", [11]), 7000: (ADAPTER, [22])},
        rows=[_tcp_row(40000, 6379, "01", 11), _tcp_row(50000, 40001, "01", 22)],
    )
    assert not already_debugged(4242)


@needs_symlinks
def test_after_detach_target_can_be_attached_again(tmp_path, monkeypatch):
    # tdb's adapter is gone and the socket with it: re-attach must work.
    _fake_proc(
        tmp_path,
        monkeypatch,
        processes={4242: ("python app.py", [])},
    )
    assert not already_debugged(4242)


def test_vanished_process_is_not_debugged(tmp_path, monkeypatch):
    monkeypatch.setattr(f"{MODULE}.sys.platform", "linux")
    monkeypatch.setattr(f"{MODULE}._PROC", str(tmp_path / "missing"))
    assert not already_debugged(4242)


def test_not_checked_without_proc(monkeypatch):
    monkeypatch.setattr(f"{MODULE}.sys.platform", "darwin")
    assert not already_debugged(4242)


def _fake_maps(tmp_path, monkeypatch, text):
    (tmp_path / "4242").mkdir()
    (tmp_path / "4242" / "maps").write_text(text)
    monkeypatch.setattr(f"{MODULE}._PROC", str(tmp_path))


def test_musl_target_detected(tmp_path, monkeypatch):
    _fake_maps(
        tmp_path,
        monkeypatch,
        "7f00-7f01 r-xp 00000000 08:01 12 /lib/ld-musl-x86_64.so.1\n",
    )
    assert uses_musl(4242)


def test_glibc_target_is_not_musl(tmp_path, monkeypatch):
    _fake_maps(
        tmp_path,
        monkeypatch,
        "7f00-7f01 r-xp 00000000 08:01 12 /usr/lib/libc.so.6\n",
    )
    assert not uses_musl(4242)


def test_unreadable_maps_is_not_musl(tmp_path, monkeypatch):
    monkeypatch.setattr(f"{MODULE}._PROC", str(tmp_path))
    assert not uses_musl(4242)


def test_probe_refuses_already_debugged_target(monkeypatch, alive):
    monkeypatch.setattr(f"{MODULE}.already_debugged", lambda pid: True)
    with pytest.raises(LanguageNotSupportedError, match="tdb -r"):
        probe_target(4242)


def test_detect_deleted_interpreter_is_python():
    # After a package upgrade /proc/PID/exe names a file that is gone.
    assert registry.detect_executable("/usr/bin/python3.12 (deleted)") == "python"

"""Target probing and language detection for `tdb -a PID` on Python."""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import Mock

import pytest

from tdb.languages import registry
from tdb.languages.base import LanguageNotSupportedError
from tdb.languages.python_attach import (
    TargetInfo,
    is_python_executable,
    pid_exists,
    probe_target,
)

MODULE = "tdb.languages.python_attach"


@pytest.fixture
def alive(monkeypatch):
    monkeypatch.setattr(f"{MODULE}.pid_exists", lambda pid: True)


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

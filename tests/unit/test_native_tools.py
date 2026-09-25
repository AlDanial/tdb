"""gdb / lldb-dap discovery and version probing (`tdb --info`), and the
basename mapping behind the path form of `--adapter` (gdb, lldb-dap, dlv)."""

import os
import shutil
import subprocess

import pytest

from tdb.languages import native_tools as nt


# --- adapter_id_for_executable -----------------------------------------------


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/usr/bin/gdb", "gdb"),
        ("/usr/bin/lldb-dap", "lldb-dap"),
        ("/usr/bin/lldb-dap-21", "lldb-dap"),
        ("/usr/bin/gdb-multiarch", "gdb"),
        ("/opt/llvm/bin/lldb-dap.exe", "lldb-dap"),
        ("C:\\msys64\\mingw64\\bin\\gdb.exe", "gdb"),
        ("./gdb", "gdb"),
        ("/home/me/go/bin/dlv", "dlv"),
        ("C:\\Users\\me\\go\\bin\\dlv.exe", "dlv"),
        ("/usr/bin/dlv-dap", "dlv"),
        ("/usr/bin/lldb", None),
        ("/usr/bin/mygdb", None),
    ],
)
def test_adapter_id_for_executable(path, expected):
    assert nt.adapter_id_for_executable(path) == expected


# --- is_executable_path --------------------------------------------------------


@pytest.mark.parametrize("value", ["gdb", "lldb-dap", "ocamlearlybird", ""])
def test_bare_adapter_ids_are_not_paths(value):
    assert nt.is_executable_path(value) is False


@pytest.mark.parametrize(
    "value", ["/usr/bin/gdb", "./gdb", "bin/lldb-dap", "C:\\tools\\gdb.exe"]
)
def test_values_with_separators_are_paths(value):
    assert nt.is_executable_path(value) is True


# --- find_native_debugger -------------------------------------------------------


def test_find_prefers_config_override_over_path(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gdb")
    assert nt.find_native_debugger("gdb", {"gdb": "/opt/gdb"}) == "/opt/gdb"


def test_find_falls_back_to_path(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    assert nt.find_native_debugger("lldb-dap", {}) == "/usr/bin/lldb-dap"


def test_find_returns_none_when_absent(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert nt.find_native_debugger("gdb", None) is None


# --- debugger_version -------------------------------------------------------------


def test_version_is_first_nonblank_line(monkeypatch):
    def fake_run(argv, **kw):
        assert argv == ["/usr/bin/gdb", "--version"]
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="\nGNU gdb (Ubuntu 17.1-2ubuntu1) 17.1\nCopyright...\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert nt.debugger_version("/usr/bin/gdb") == "GNU gdb (Ubuntu 17.1-2ubuntu1) 17.1"


def test_version_falls_back_to_stderr(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv, 0, stdout="", stderr="lldb-dap: LLVM version 21.1.8\n"
        ),
    )
    assert nt.debugger_version("/usr/bin/lldb-dap") == "lldb-dap: LLVM version 21.1.8"


@pytest.mark.parametrize("exc", [OSError("boom"), subprocess.TimeoutExpired(["x"], 5)])
def test_version_unavailable_on_failure(monkeypatch, exc):
    def fake_run(argv, **kw):
        raise exc

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert nt.debugger_version("/usr/bin/gdb") is None


def test_version_probe_never_hangs(monkeypatch):
    """The probe must pass a timeout so a wedged debugger can't stall --info."""
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 0, stdout="v1\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    nt.debugger_version("/usr/bin/gdb")
    assert seen.get("timeout")


# --- native_debugger_report -----------------------------------------------------------


def test_report_lists_path_and_version_for_each_debugger(monkeypatch):
    monkeypatch.setattr(
        nt, "find_native_debugger", lambda aid, paths: f"/usr/bin/{aid}"
    )
    monkeypatch.setattr(
        nt, "debugger_version", lambda exe: f"VERSION-OF-{os.path.basename(exe)}"
    )
    report = nt.native_debugger_report({})
    assert "/usr/bin/gdb" in report
    assert "VERSION-OF-gdb" in report
    assert "/usr/bin/lldb-dap" in report
    assert "VERSION-OF-lldb-dap" in report


def test_report_says_not_found(monkeypatch):
    monkeypatch.setattr(nt, "find_native_debugger", lambda aid, paths: None)
    report = nt.native_debugger_report({})
    assert report.count("not found on PATH") == 2


def test_report_tolerates_missing_version(monkeypatch):
    monkeypatch.setattr(
        nt, "find_native_debugger", lambda aid, paths: f"/usr/bin/{aid}"
    )
    monkeypatch.setattr(nt, "debugger_version", lambda exe: None)
    report = nt.native_debugger_report({})
    assert "/usr/bin/gdb" in report
    assert "version unknown" in report

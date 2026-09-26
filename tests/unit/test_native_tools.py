"""gdb / lldb-dap discovery and version probing (`tdb --info`), and the
basename mapping behind the path form of `--adapter` (gdb, lldb-dap, dlv)."""

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


# --- version_number -------------------------------------------------------------


@pytest.mark.parametrize(
    "line, expected",
    [
        ("Python 3.14.4", "3.14.4"),
        ("GNU gdb (Ubuntu 17.1-2ubuntu1) 17.1", "17.1"),
        ("lldb-dap: Ubuntu LLVM version 21.1.8", "21.1.8"),
        (
            "This is perl 5, version 40, subversion 1 (v5.40.1) built for x86_64",
            "5.40.1",
        ),
        ("ruby 3.3.8 (2025-04-09 revision b200bad6cd) [x86_64-linux-gnu]", "3.3.8"),
        ("GNU bash, version 5.3.9(1)-release (x86_64-pc-linux-gnu)", "5.3.9(1)-release"),
        ("tcsh 6.24.13 (Astron) 2024-06-12 (x86_64-unknown-linux) options", "6.24.13"),
        ("PowerShell 7.6.5", "7.6.5"),
        ("no digits here", None),
        (None, None),
    ],
)
def test_version_number(line, expected):
    assert nt.version_number(line) == expected


def test_tool_version_number_probes_and_extracts(monkeypatch):
    def fake_run(argv, **kw):
        assert argv == ["/usr/bin/bash", "--version"]
        return subprocess.CompletedProcess(
            argv, 0, stdout="GNU bash, version 5.3.9(1)-release (x86_64)\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert nt.tool_version_number("/usr/bin/bash") == "5.3.9(1)-release"


def test_tool_version_number_custom_args_scan_all_lines(monkeypatch):
    """`dlv version` prints the number on its second line."""

    def fake_run(argv, **kw):
        assert argv == ["/home/me/go/bin/dlv", "version"]
        return subprocess.CompletedProcess(
            argv, 0, stdout="Delve Debugger\nVersion: 1.27.1\nBuild: $Id: 38e5 $\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert nt.tool_version_number("/home/me/go/bin/dlv", ("version",)) == "1.27.1"


def test_tool_version_number_none_when_probe_fails(monkeypatch):
    def fake_run(argv, **kw):
        raise OSError("boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert nt.tool_version_number("/usr/bin/bash") is None

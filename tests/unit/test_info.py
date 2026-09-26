"""`tdb --info` report layout (tdb.info)."""

from __future__ import annotations

import os
import sys

import pytest

from tdb import info
from tdb.languages import native_tools as nt


@pytest.fixture
def stub_tools(monkeypatch):
    """Every tool is found under /stub and reports version 99.2.3."""
    monkeypatch.setattr(nt, "find_native_debugger", lambda name, paths: f"/stub/{name}")
    monkeypatch.setattr(
        nt, "version_output", lambda exe, args: f"{os.path.basename(exe)} 99.2.3\n"
    )


def _section(text: str, title: str) -> str:
    """The lines under `title` up to the next blank line."""
    body = text.split(f"\n{title}\n", 1)[1]
    return body.split("\n\n", 1)[0]


def test_row_is_indented_and_padded():
    assert info.row("gdb executable", "/usr/bin/gdb") == (
        "    gdb executable           : /usr/bin/gdb"
    )


def test_tool_sections_show_path_and_version(stub_tools):
    text = info.info_text()
    assert _section(text, "GDB").splitlines() == [
        "    gdb executable           : /stub/gdb",
        "    version                  : 99.2.3",
    ]
    assert _section(text, "lldb-dap").splitlines() == [
        "    lldb-dap                 : /stub/lldb-dap",
        "    version                  : 99.2.3",
    ]
    assert _section(text, "Ruby").splitlines() == [
        "    ruby executable          : /stub/ruby",
        "    version                  : 99.2.3",
        "    rdbg executable          : /stub/rdbg",
        "    version                  : 99.2.3",
    ]
    assert _section(text, "bash").splitlines()[0] == "    bash executable          : /stub/bash"
    assert _section(text, "tcsh").splitlines()[0] == "    tcsh executable          : /stub/tcsh"
    assert _section(text, "PowerShell").splitlines()[0] == (
        "    pwsh executable          : /stub/pwsh"
    )


def test_missing_tool_has_no_version_row(monkeypatch):
    monkeypatch.setattr(nt, "find_native_debugger", lambda name, paths: None)
    text = info.info_text()
    assert _section(text, "GDB").splitlines() == [
        "    gdb executable           : not found on PATH",
    ]


def test_unknown_version_is_reported(monkeypatch):
    monkeypatch.setattr(nt, "find_native_debugger", lambda name, paths: f"/stub/{name}")
    monkeypatch.setattr(nt, "version_output", lambda exe, args: None)
    text = info.info_text()
    assert "    version                  : unknown" in _section(text, "GDB")


def test_go_and_ocaml_sections(monkeypatch):
    """dlv reports via `dlv version` (number on line 2); ocamlearlybird
    prints a bare number to `--version`."""
    monkeypatch.setattr(nt, "find_native_debugger", lambda name, paths: f"/stub/{name}")

    def fake_output(exe, args):
        if exe.endswith("dlv"):
            assert args == ("version",)
            return "Delve Debugger\nVersion: 1.27.1\nBuild: $Id: 38e5 $\n"
        assert args == ("--version",)
        return "1.3.6\n"

    monkeypatch.setattr(nt, "version_output", fake_output)
    text = info.info_text()
    assert _section(text, "Go").splitlines() == [
        "    dlv executable           : /stub/dlv",
        "    version                  : 1.27.1",
    ]
    assert _section(text, "OCaml").splitlines() == [
        "    ocamlearlybird executable: /stub/ocamlearlybird",
        "    version                  : 1.3.6",
    ]
    assert text.index("\nRuby\n") < text.index("\nGo\n") < text.index("\nOCaml\n") < text.index("\nbash\n")


def test_config_override_wins_over_path(monkeypatch, stub_tools):
    """The config.json adapters map feeds the lookup, so a null/absent
    entry falls through to PATH and a set one is used verbatim."""
    from tdb import persist

    monkeypatch.setattr(
        persist, "load_config",
        lambda: persist.TdbConfig(adapters={"gdb": "/opt/gdb", "lldb-dap": None}),
    )
    seen = {}

    def fake_find(name, paths):
        seen[name] = paths
        return (paths or {}).get(name) or f"/path/{name}"

    monkeypatch.setattr(nt, "find_native_debugger", fake_find)
    text = info.info_text()
    assert "    gdb executable           : /opt/gdb" in text
    assert "    lldb-dap                 : /path/lldb-dap" in text
    assert "    perl executable          : /path/perl" in text
    assert seen["perl"] == {"gdb": "/opt/gdb", "lldb-dap": None}


def test_python_section_uses_running_interpreter(stub_tools):
    text = info.info_text()
    lines = _section(text, "Python").splitlines()
    assert lines[0] == f"    python executable        : {sys.executable}"
    assert lines[1] == "    version                  : " + ".".join(
        str(n) for n in sys.version_info[:3]
    )


def test_tdb_section_lists_files(stub_tools, tmp_path, monkeypatch):
    from tdb import __version__, persist
    from tdb.app_helpers import install_dir

    log = tmp_path / "tdb.log"
    log.write_bytes(b"x" * 15084)
    monkeypatch.setenv("TDB_LOG_DIR", str(tmp_path))
    lines = _section(info.info_text(), "textual-debugger").splitlines()
    assert lines == [
        f"    tdb executable           : {install_dir()}",
        f"    version                  : {__version__}",
        f"    config file              : {persist.CONFIG_FILE}",
        f"    breakpoints file         : {persist.STATE_FILE}",
        f"    log file                 : {log} [15 KB]",
    ]


def test_missing_log_file_is_marked(stub_tools, tmp_path, monkeypatch):
    monkeypatch.setenv("TDB_LOG_DIR", str(tmp_path))
    text = info.info_text()
    assert f"    log file                 : {tmp_path / 'tdb.log'} [not found]" in text


@pytest.mark.parametrize(
    "size, label", [(0, "0 B"), (512, "512 B"), (1024, "1 KB"), (15084, "15 KB"),
                    (2 * 1024 * 1024, "2048 KB")]
)
def test_size_label(size, label):
    assert info.size_label(size) == label


def test_perl_section_has_padwalker_rows_and_note(stub_tools):
    from tdb.adapters.perl import padwalker as pw

    lines = _section(info.info_text(), "Perl").splitlines()
    assert lines[:2] == [
        "    perl executable          : /stub/perl",
        "    version                  : 99.2.3",
    ]
    assert f"    PadWalker sources        : {pw.padwalker_dir()}" in lines
    assert f"    PadWalker build cache    : {pw.cache_root()}" in lines
    assert lines[-2].startswith("    (tdb builds PadWalker")
    assert lines[-1].startswith("     prepends the cache directory")


def test_log_file_honors_tdb_log_dir(tmp_path, monkeypatch):
    from tdb import persist

    monkeypatch.setenv("TDB_LOG_DIR", str(tmp_path))
    assert persist.log_file() == tmp_path / "tdb.log"
    monkeypatch.delenv("TDB_LOG_DIR")
    assert persist.log_file() == persist.CONFIG_DIR / "tdb.log"


# --- minimum version check ------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("17.1", (17, 1)),
        ("5.3.9(1)-release", (5, 3, 9)),
        ("1.27.1", (1, 27, 1)),
        ("14", (14,)),
        ("", ()),
        (None, ()),
    ],
)
def test_version_tuple(text, expected):
    assert info.version_tuple(text) == expected


def test_minimum_versions_cover_documented_floors():
    assert info.MINIMUM_VERSIONS == {
        "python": (3, 11),
        "gdb": (14,),
        "lldb-dap": (17,),
        "perl": (5, 18),
        "dlv": (1, 21),
        "rdbg": (1, 9),
        "bash": (4, 4),
        "pwsh": (7, 0),
    }


def test_version_row_flags_versions_below_minimum():
    assert info.version_row("gdb", "13.2") == (
        "    version                  : 13.2 DOES NOT MEET MINIMUM REQUIRED VERSION OF 14"
    )
    assert info.version_row("bash", "4.3.48(1)-release") == (
        "    version                  : 4.3.48(1)-release "
        "DOES NOT MEET MINIMUM REQUIRED VERSION OF 4.4"
    )


def test_version_row_accepts_versions_at_or_above_minimum():
    assert info.version_row("gdb", "14") == "    version                  : 14"
    assert info.version_row("gdb", "17.1") == "    version                  : 17.1"
    assert info.version_row("bash", "4.4") == "    version                  : 4.4"
    assert info.version_row("perl", "5.40.1") == "    version                  : 5.40.1"


def test_version_row_without_minimum_or_version():
    assert info.version_row("ruby", "2.7.0") == "    version                  : 2.7.0"
    assert info.version_row("gdb", None) == "    version                  : unknown"


def test_report_flags_old_tool(monkeypatch):
    monkeypatch.setattr(nt, "find_native_debugger", lambda name, paths: f"/stub/{name}")
    monkeypatch.setattr(
        nt, "version_output",
        lambda exe, args: "GNU bash, version 4.3.48(1)-release\n"
        if exe.endswith("bash") else "99.9.9\n",
    )
    text = info.info_text()
    assert _section(text, "bash").splitlines() == [
        "    bash executable          : /stub/bash",
        "    version                  : 4.3.48(1)-release "
        "DOES NOT MEET MINIMUM REQUIRED VERSION OF 4.4",
    ]
    assert "DOES NOT MEET" not in _section(text, "GDB")


def test_python_section_flags_old_interpreter(monkeypatch, stub_tools):
    monkeypatch.setattr(info, "_python_version", lambda: "3.9.2")
    lines = _section(info.info_text(), "Python").splitlines()
    assert lines[1] == (
        "    version                  : 3.9.2 DOES NOT MEET MINIMUM REQUIRED VERSION OF 3.11"
    )


def test_ruby_section_flags_old_rdbg(monkeypatch):
    monkeypatch.setattr(nt, "find_native_debugger", lambda name, paths: f"/stub/{name}")
    monkeypatch.setattr(
        nt, "version_output",
        lambda exe, args: "rdbg 1.8.0\n" if exe.endswith("rdbg") else "ruby 3.3.8 (x86_64)\n",
    )
    assert _section(info.info_text(), "Ruby").splitlines() == [
        "    ruby executable          : /stub/ruby",
        "    version                  : 3.3.8",
        "    rdbg executable          : /stub/rdbg",
        "    version                  : 1.8.0 DOES NOT MEET MINIMUM REQUIRED VERSION OF 1.9",
    ]


def test_ruby_section_when_only_rdbg_is_missing(monkeypatch, stub_tools):
    monkeypatch.setattr(
        nt, "find_native_debugger",
        lambda name, paths: None if name == "rdbg" else f"/stub/{name}",
    )
    assert _section(info.info_text(), "Ruby").splitlines() == [
        "    ruby executable          : /stub/ruby",
        "    version                  : 99.2.3",
        "    rdbg executable          : not found on PATH",
    ]

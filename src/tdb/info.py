"""The `tdb --info` report: the About text followed by one section per
tool tdb runs (its own files, then each language's interpreter or native
debugger), each as an indented `label : value` table.

Read-only: never triggers a PadWalker build; the only subprocesses are
`<tool> --version` probes, each bounded by a timeout.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

LABEL_WIDTH = 25
NOT_FOUND = "not found on PATH"

# Oldest version of each tool tdb works with, keyed by executable name.
# Sources: pyproject requires-python; GDB's DAP mode (gdb -i dap) arrived
# in 14; lldb-dap ships with LLVM 17; perl5db features used by the perl
# adapter need 5.18; `dlv dap` needs Delve 1.21; the bash harness needs
# 4.4; the Ruby adapter needs the debug gem's rdbg >= 1.9; the PowerShell
# adapter refuses pwsh below MIN_PWSH (7.0). Tools absent here (ruby,
# tcsh, ocamlearlybird) have no documented floor.
MINIMUM_VERSIONS: dict[str, tuple[int, ...]] = {
    "python": (3, 11),
    "gdb": (14,),
    "lldb-dap": (17,),
    "perl": (5, 18),
    "dlv": (1, 21),
    "rdbg": (1, 9),
    "bash": (4, 4),
    "pwsh": (7, 0),
}

_LEADING_NUMBERS = re.compile(r"^\d+(?:\.\d+)*")


def version_tuple(text: str | None) -> tuple[int, ...]:
    """``"5.3.9(1)-release"`` -> ``(5, 3, 9)``; ``()`` when there is no
    leading dotted number to compare."""
    m = _LEADING_NUMBERS.match(text or "")
    return tuple(int(n) for n in m.group(0).split(".")) if m else ()


def row(label: str, value: object) -> str:
    return f"    {label:<{LABEL_WIDTH}}: {value}"


def size_label(size: int) -> str:
    """``"512 B"`` under a kilobyte, else whole kilobytes (``"15 KB"``)."""
    if size < 1024:
        return f"{size} B"
    return f"{round(size / 1024)} KB"


def version_row(name: str, version: str | None) -> str:
    """The ``version`` row for tool ``name``, flagged when the version is
    below MINIMUM_VERSIONS[name]. Tuple comparison pads naturally:
    ``(14,) <= (14, 1)`` and ``(4, 3, 48) < (4, 4)``."""
    if version is None:
        return row("version", "unknown")
    minimum = MINIMUM_VERSIONS.get(name)
    found = version_tuple(version)
    if minimum and found and found < minimum:
        floor = ".".join(str(n) for n in minimum)
        return row("version", f"{version} DOES NOT MEET MINIMUM REQUIRED VERSION OF {floor}")
    return row("version", version)


def _python_version() -> str:
    return ".".join(str(n) for n in sys.version_info[:3])


def _log_row(path: Path) -> str:
    try:
        suffix = f"[{size_label(path.stat().st_size)}]"
    except OSError:
        suffix = "[not found]"
    return row("log file", f"{path} {suffix}")


def tool_rows(
    label: str,
    name: str,
    adapter_paths: dict[str, str | None] | None,
    version_args: tuple[str, ...] = ("--version",),
) -> list[str]:
    """``label : <path>`` plus a ``version`` row, or a single
    ``not found on PATH`` row. ``name`` is both the executable looked up
    on PATH and its key in config.json's ``adapters`` map."""
    from tdb.languages import native_tools as nt

    exe = nt.find_native_debugger(name, adapter_paths)
    if exe is None:
        return [row(label, NOT_FOUND)]
    return [row(label, exe), version_row(name, nt.tool_version_number(exe, version_args))]


def info_text() -> str:
    from tdb import __version__
    from tdb.adapters.perl.padwalker import cache_root, padwalker_dir
    from tdb.app_helpers import about_text, install_dir
    from tdb.persist import CONFIG_FILE, STATE_FILE, load_config, log_file

    adapters = load_config().adapters

    sections: list[tuple[str, list[str]]] = [
        (
            "textual-debugger",
            [
                row("tdb executable", install_dir()),
                row("version", __version__),
                row("config file", CONFIG_FILE),
                row("breakpoints file", STATE_FILE),
                _log_row(log_file()),
            ],
        ),
        (
            "Python",
            [
                row("python executable", sys.executable),
                version_row("python", _python_version()),
            ],
        ),
        ("GDB", tool_rows("gdb executable", "gdb", adapters)),
        ("lldb-dap", tool_rows("lldb-dap", "lldb-dap", adapters)),
        (
            "Perl",
            tool_rows("perl executable", "perl", adapters)
            + [
                row("PadWalker sources", padwalker_dir()),
                row("PadWalker build cache", cache_root()),
                "    (tdb builds PadWalker for each perl it launches, caches it, and",
                "     prepends the cache directory to the debuggee's PERL5LIB)",
            ],
        ),
        (
            "Ruby",
            tool_rows("ruby executable", "ruby", adapters)
            + tool_rows("rdbg executable", "rdbg", adapters),
        ),
        # `dlv --version` is an error; `dlv version` prints the number on
        # its second line.
        ("Go", tool_rows("dlv executable", "dlv", adapters, ("version",))),
        ("OCaml", tool_rows("ocamlearlybird executable", "ocamlearlybird", adapters)),
        ("bash", tool_rows("bash executable", "bash", adapters)),
        ("tcsh", tool_rows("tcsh executable", "tcsh", adapters)),
        ("PowerShell", tool_rows("pwsh executable", "pwsh", adapters)),
    ]
    blocks = [about_text(markup=False)]
    for title, lines in sections:
        blocks.append("\n".join([title, *lines]))
    return "\n\n".join(blocks)

"""Pure helpers for the PowerShell proxy: arg quoting, the exit-code
sentinel printed by tdb_launch.ps1, and classification of pwsh's stdout
lines (prompt echo to drop, ConciseView error blocks to tag as stderr
so tdb's fatal-error modal can see them)."""

from __future__ import annotations

import re
from pathlib import Path

LAUNCHER = Path(__file__).with_name("tdb_launch.ps1")

# \x1e (record separator) keeps the sentinel from colliding with any
# plausible script output. Must match tdb_launch.ps1's Write-Host.
EXIT_SENTINEL_PREFIX = "\x1etdb-exit:"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# The fake prompt PSES's temporary console echoes when the script starts:
#   PS /cwd> . '/path/tdb_launch.ps1' '/path/script.ps1' 'arg'
_PROMPT_ECHO_RE = re.compile(r"^PS .*> \. '")
# ConciseView header: "Exception: /p/s.ps1:2", "Get-Item: /p/s.ps1:1"
_ERROR_HEAD_RE = re.compile(r"^[A-Za-z][\w.-]*: .+?:\d+\s*$")
# Rows that belong to the block that follows a header.
_ERROR_CONT_RE = re.compile(r"^\s*(?:Line \||\d+ \||\|)")


def quote_ps_arg(s: str) -> str:
    """PowerShell single-quoted literal (the only escape is '' for ')."""
    return "'" + s.replace("'", "''") + "'"


def parse_exit_sentinel(line: str) -> int | None:
    text = line.rstrip("\r\n")
    if not text.startswith(EXIT_SENTINEL_PREFIX):
        return None
    tail = text[len(EXIT_SENTINEL_PREFIX) :]
    try:
        return int(tail)
    except ValueError:
        return None


class OutputClassifier:
    """Stateful per-line classifier for pwsh's stdout.

    classify() returns the DAP output category for the line, or None to
    drop it. State: the first prompt echo is dropped once; a ConciseView
    header opens an error block that stays "stderr" while continuation
    rows keep coming.
    """

    def __init__(self) -> None:
        self._prompt_seen = False
        self._in_error = False

    def classify(self, line: str) -> str | None:
        text = _ANSI_RE.sub("", line).rstrip("\r\n")
        if not self._prompt_seen and _PROMPT_ECHO_RE.match(text):
            self._prompt_seen = True
            return None
        if _ERROR_HEAD_RE.match(text):
            self._in_error = True
            return "stderr"
        if self._in_error:
            if _ERROR_CONT_RE.match(text):
                return "stderr"
            self._in_error = False
        return "stdout"

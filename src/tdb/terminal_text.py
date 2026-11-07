"""Keeping a debuggee's text from acting as commands to tdb's own terminal.

Rich strips a few control characters from text but leaves ESC, so an escape
sequence in a variable or in program output goes straight to the terminal.
Under `sudo tdb -a PID` the debuggee's user is hostile, and OSC 52 alone can
write the admin's clipboard.
"""

from __future__ import annotations

import errno
import re
import sys
from typing import Any

from rich.text import Text

# ESC or its 8-bit form. String commands run to a terminator, or to the end of
# the text if there is none: a terminal would wait for it.
_STRING = r"(?:\x1b[\]PX^_]|[\x9d\x90\x98\x9e\x9f])(?:.*?)(?:\x07|\x1b\\|\x9c|\Z)"
_CSI = r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
_OTHER = r"\x1b[ -/]*[0-~]"
_SEQUENCE = re.compile(f"{_STRING}|{_CSI}|{_OTHER}", re.DOTALL)
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_COLOUR = re.compile(r"\x1b\[[0-9;]*m")


def strip_terminal_controls(text: str) -> str:
    """`text` with terminal sequences and control characters gone, keeping
    tabs and newlines."""
    return _CONTROLS.sub("", _SEQUENCE.sub("", text))


def _keep_colour(match: re.Match[str]) -> str:
    sequence = match.group()
    return sequence if _COLOUR.fullmatch(sequence) else ""


def _keep_colour_escape(match: re.Match[str]) -> str:
    start = match.start()
    return match.group() if _COLOUR.match(match.string, start) else ""


def console_text(text: str, style: str = "") -> Text:
    """Program output for display: its colours are decoded by Rich, which is
    what makes them safe, and anything else a terminal would act on goes."""
    if "\x1b" not in text:
        return Text(strip_terminal_controls(text), style=style)
    kept = _SEQUENCE.sub(_keep_colour, text)
    # Only the ESC of a colour may stay, for Rich to decode.
    kept = _CONTROLS.sub(_keep_colour_escape, kept)
    decoded = Text.from_ansi(kept, style=style)
    if text.endswith("\n"):
        decoded.append("\n")
    return decoded


def terminal_hung_up() -> bool:
    """True if the terminal tdb runs on has gone. SIGHUP usually says so, but
    not to a process outside its foreground group, nor one ignoring it."""
    try:
        import termios

        termios.tcgetattr(sys.__stdin__.fileno())
    except termios.error as exc:
        return exc.args[0] == errno.EIO
    except (AttributeError, ImportError, OSError, ValueError):
        pass
    return False


def clean_message(value: Any) -> Any:
    """A decoded DAP message, or part of one, with every string stripped."""
    if isinstance(value, str):
        return strip_terminal_controls(value)
    if isinstance(value, list):
        return [clean_message(item) for item in value]
    if isinstance(value, dict):
        return {clean_message(k): clean_message(v) for k, v in value.items()}
    return value

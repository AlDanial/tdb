"""Keeping a debuggee's text from acting as commands to tdb's own terminal.

Rich strips a few control characters from text but leaves ESC, so an escape
sequence in a variable or in program output goes straight to the terminal.
Under `sudo tdb -a PID` the debuggee's user is hostile, and OSC 52 alone can
write the admin's clipboard.
"""

from __future__ import annotations

import contextlib
import errno
import re
import sys
from typing import Any

from rich.text import Text

# String commands run to a terminator, or, after ESC, to the end of the text
# if there is none: a terminal would wait for it. An 8-bit introducer needs
# its terminator, since a lone one is as likely a stray byte of mis-decoded
# text; on its own it goes with the other controls, and what follows is text.
_STRING = (
    r"(?:\x1b[\]PX^_](?:.*?)(?:\x07|\x1b\\|\x9c|\Z))"
    r"|(?:[\x9d\x90\x98\x9e\x9f](?:.*?)(?:\x07|\x1b\\|\x9c))"
)
_CSI = r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
_OTHER = r"\x1b[ -/]*[0-~]"
_SEQUENCE = re.compile(f"{_STRING}|{_CSI}|{_OTHER}", re.DOTALL)
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_COLOUR = re.compile(r"\x1b\[[0-9;]*m")


def strip_terminal_controls(text: str) -> str:
    """`text` with terminal sequences and control characters gone, keeping
    tabs and newlines."""
    return _CONTROLS.sub("", _SEQUENCE.sub("", text))


# C0 controls and DEL have Unicode "control pictures"; C1 controls do not.
_PICTURES = {code: chr(0x2400 + code) for code in range(0x20)}
_PICTURES[0x7F] = "\u2421"
_PICTURES.update({code: "\ufffd" for code in range(0x80, 0xA0)})
for _kept in "\t\n\r":
    del _PICTURES[ord(_kept)]


def visible_controls(text: str) -> str:
    """`text` with each control character shown as a symbol in its place, for
    text worth reading as it is, like source code: a column per character
    stays a column per character, and the terminal sees nothing to obey."""
    return text.translate(_PICTURES)


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
    # Rich keeps a final line break, but not every version has.
    if text.endswith("\n") and not decoded.plain.endswith("\n"):
        decoded.append("\n")
    return decoded


def terminal_hung_up() -> bool:
    """True if the terminal tdb runs on has gone. SIGHUP usually says so, but
    not to a process outside its foreground group, nor one ignoring it.
    stderr is checked as well as stdin: Textual draws on it, and under nohup
    stdin is not the terminal at all."""
    try:
        import termios
    except ImportError:  # Windows
        return False
    for stream in (sys.__stdin__, sys.__stderr__):
        try:
            termios.tcgetattr(stream.fileno())
        except termios.error as exc:
            if exc.args[0] == errno.EIO:
                return True
        except (AttributeError, OSError, ValueError):
            pass
    return False


def _draining_run(self: Any) -> None:
    """Textual's WriterThread.run, except that a terminal gone (EIO) stops the
    writing, not the thread. Textual's own run dies on it, and its queue then
    fills and blocks every later write, shutdown's included, for good."""
    get = self._queue.get
    qsize = self._queue.qsize
    gone = False
    while True:
        text = get()
        if text is None:
            break
        if gone:
            continue
        try:
            self._file.write(text)
            if qsize() == 0:
                self._file.flush()
        except OSError:
            gone = True
    if not gone:
        with contextlib.suppress(OSError):
            self._file.flush()


def keep_writer_draining() -> None:
    """Make every Textual WriterThread (Linux and Windows drivers alike) keep
    taking writes once the terminal has gone."""
    from textual.drivers._writer_thread import WriterThread

    WriterThread.run = _draining_run  # type: ignore[method-assign]


def clean_message(value: Any) -> Any:
    """A decoded DAP message, or part of one, with every string stripped."""
    if isinstance(value, str):
        return strip_terminal_controls(value)
    if isinstance(value, list):
        return [clean_message(item) for item in value]
    if isinstance(value, dict):
        return {clean_message(k): clean_message(v) for k, v in value.items()}
    return value

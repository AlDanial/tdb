"""What a debuggee sends must not reach tdb's own terminal as commands.

Under `sudo tdb -a PID` the debuggee's user is hostile: OSC 52 would write the
admin's clipboard, and whatever is pasted from it goes into a root shell.
"""

from __future__ import annotations

import errno
import threading

import pytest
from rich.text import Text

from tdb.dap.messages import Event, Response, parse_message
from tdb.terminal_text import (
    console_text,
    keep_writer_draining,
    strip_terminal_controls,
)

OSC_52 = "\x1b]52;c;ZXZpbA==\x1b\\"
OSC_TITLE_BEL = "\x1b]0;title\x07"
CSI = "\x1b[2J"
DCS = "\x1bPq#0\x1b\\"


@pytest.mark.parametrize(
    "sequence", [OSC_52, OSC_TITLE_BEL, CSI, DCS, "\x1bc", "\x9b2J"]
)
def test_sequences_go(sequence):
    assert strip_terminal_controls(f"a{sequence}b") == "ab"


def test_a_lone_escape_goes():
    assert strip_terminal_controls("a\x1b") == "a"
    assert console_text("a\x1b").plain == "a"


def test_ordinary_text_stays():
    text = "tabs\tand\nnewlines, é and 日本語"
    assert strip_terminal_controls(text) == text


def test_an_unterminated_sequence_goes_too():
    assert "\x1b" not in strip_terminal_controls("a\x1b]52;c;ZXZpbA==")


def test_every_control_goes():
    text = (
        "".join(map(chr, range(0x20))) + "\x7f" + "".join(map(chr, range(0x80, 0xA0)))
    )
    assert strip_terminal_controls(text) == "\t\n"


def test_console_text_keeps_colours():
    text = console_text("\x1b[31mred\x1b[0m plain")
    assert text.plain == "red plain"
    assert any(span.style.color for span in text.spans)


@pytest.mark.parametrize("sequence", [OSC_52, OSC_TITLE_BEL, CSI, DCS, "\x1bc"])
def test_console_text_drops_everything_else(sequence):
    assert console_text(f"a{sequence}b").plain == "ab"


def test_console_text_applies_the_style():
    assert isinstance(console_text("x", style="red"), Text)
    assert str(console_text("x", style="red").style) == "red"


def test_console_text_keeps_line_breaks():
    assert console_text("one\ntwo\n").plain == "one\ntwo\n"


def _message(**fields):
    return fields


def test_responses_from_the_debuggee_are_clean():
    message = parse_message(
        _message(
            seq=1,
            type="response",
            request_seq=1,
            success=True,
            command="variables",
            body={"variables": [{"name": f"x{OSC_52}", "value": f"1{OSC_52}"}]},
        )
    )
    assert isinstance(message, Response)
    assert message.body == {"variables": [{"name": "x", "value": "1"}]}


def test_events_from_the_debuggee_are_clean():
    message = parse_message(
        _message(seq=1, type="event", event="stopped", body={"text": f"a{CSI}"})
    )
    assert isinstance(message, Event)
    assert message.body == {"text": "a"}


def test_program_output_is_left_for_the_console():
    # Its colours are decoded there.
    message = parse_message(
        _message(
            seq=1, type="event", event="output",
            body={"category": "stdout", "output": "\x1b[31mred\x1b[0m"},
        )
    )  # fmt: skip
    assert message.body["output"] == "\x1b[31mred\x1b[0m"


class _GoneTerminal:
    def write(self, text):
        raise OSError(errno.EIO, "Input/output error")

    def flush(self):
        raise OSError(errno.EIO, "Input/output error")

    def fileno(self):
        return 1


def test_writes_never_block_once_the_terminal_is_gone():
    from textual.drivers._writer_thread import MAX_QUEUED_WRITES, WriterThread

    keep_writer_draining()
    writer = WriterThread(_GoneTerminal())
    writer.start()
    finished = threading.Event()

    def flood():
        for _ in range(MAX_QUEUED_WRITES * 3):
            writer.write("frame")
        writer.stop()
        finished.set()

    threading.Thread(target=flood, daemon=True).start()
    assert finished.wait(5), "a write or stop() blocked on the dead writer"


def test_hang_up_check_survives_a_platform_without_termios(monkeypatch):
    """Windows has no termios; the check runs every two seconds from start-up."""
    import sys

    from tdb.terminal_text import terminal_hung_up

    monkeypatch.setitem(sys.modules, "termios", None)
    assert terminal_hung_up() is False


@pytest.mark.skipif(not hasattr(__import__("os"), "openpty"), reason="needs a pty")
def test_hang_up_is_seen_on_the_terminal_tdb_draws_on(monkeypatch, tmp_path):
    """Under nohup, stdin is not the terminal; the screen still goes to stderr."""
    import os
    import sys

    from tdb.terminal_text import terminal_hung_up

    master, slave = os.openpty()
    os.close(master)
    with open(os.devnull) as quiet, os.fdopen(slave, "w") as screen:
        monkeypatch.setattr(sys, "__stdin__", quiet)
        monkeypatch.setattr(sys, "__stderr__", screen)
        assert terminal_hung_up() is True


def test_coloured_output_keeps_exactly_its_own_line_break():
    assert console_text("\x1b[31mred\x1b[0m\n").plain == "red\n"
    assert console_text("\x1b[31mred\x1b[0m").plain == "red"


def test_a_lone_8_bit_introducer_does_not_take_the_rest_of_the_text():
    assert strip_terminal_controls("caf\x9d keep this") == "caf keep this"
    assert strip_terminal_controls("a\x9d0;title\x9cb") == "ab"

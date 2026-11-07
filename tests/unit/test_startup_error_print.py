"""A startup error can quote the debuggee or its adapter (a dying gdb's
stderr, a target's reply), and it is printed straight to the terminal."""

from types import SimpleNamespace

import pytest

from tdb.cli import _exit_on_startup_error


def test_startup_errors_reach_the_terminal_without_escapes(capsys):
    app = SimpleNamespace(
        _startup_error="tdb: gdb died: \x1b]52;c;ZXZpbA==\x07bad", return_code=2
    )
    with pytest.raises(SystemExit) as exit_:
        _exit_on_startup_error(app)
    assert exit_.value.code == 2
    assert capsys.readouterr().err == "tdb: gdb died: bad\n"


def test_no_startup_error_is_a_no_op(capsys):
    _exit_on_startup_error(SimpleNamespace(_startup_error=None, return_code=0))
    assert capsys.readouterr().err == ""

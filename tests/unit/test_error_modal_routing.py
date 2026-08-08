"""The stderr error modal is driven by the active language profile."""

import pytest

from tdb.app import TdbApp
from tdb.persist import TdbConfig

PY_TB = """Traceback (most recent call last):
  File "/app/main.py", line 3, in <module>
    boom()
ZeroDivisionError: division by zero
"""

PERL_DIE = "Illegal division by zero at /w/x.pl line 4.\n"

CHAINED_PY_TB = """Traceback (most recent call last):
  File "/app/a.py", line 2, in <module>
    inner()
ValueError: first

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "/app/b.py", line 9, in <module>
    outer()
RuntimeError: second
"""


async def _pushed_modal(app, stderr_text):
    pushed = []
    app.push_screen = lambda screen, callback=None: pushed.append(screen)
    app._stderr_buffer.clear()
    app._stderr_buffer.append(stderr_text)
    app._dap._check_stderr_traceback()
    return pushed


async def test_python_traceback_still_shows_modal():
    app = TdbApp(program="", config=TdbConfig())
    async with app.run_test() as pilot:
        await pilot.pause()
        pushed = await _pushed_modal(app, PY_TB)
        assert pushed, "python traceback should push a modal"
        assert "ZeroDivisionError" in app.panels.last_exception_text


async def test_perl_die_shows_modal_with_frames():
    from tdb.languages import registry

    app = TdbApp(program="", config=TdbConfig(), profile=registry.resolve("perl"))
    async with app.run_test() as pilot:
        await pilot.pause()
        pushed = await _pushed_modal(app, PERL_DIE)
        assert pushed, "perl die should push a modal"
        assert "Illegal division by zero" in app.panels.last_exception_text
        assert "/w/x.pl" in app.panels.last_frames_text
        # Code View / stack navigated to the failing frame
        assert app.controller.state.stack_frames
        assert app.controller.state.stack_frames[0].line == 4


async def test_chained_python_traceback_modal_body_keeps_raw_detail():
    """The modal body for Python must not lose information relative to
    the pre-refactor inline implementation: source-snippet lines
    (e.g. "    outer()") and the chained-exception separator sentence
    ("The above exception was the direct cause...") must both still be
    present in the cached/displayed frames text, not just the
    structured File/line/func summary."""
    app = TdbApp(program="", config=TdbConfig())
    async with app.run_test() as pilot:
        await pilot.pause()
        pushed = await _pushed_modal(app, CHAINED_PY_TB)
        assert pushed, "chained python traceback should push a modal"
        body = app.panels.last_frames_text
        assert "    outer()" in body
        assert (
            "The above exception was the direct cause of the following exception:"
            in body
        )


async def test_non_error_stderr_shows_nothing():
    from tdb.languages import registry

    app = TdbApp(program="", config=TdbConfig(), profile=registry.resolve("perl"))
    async with app.run_test() as pilot:
        await pilot.pause()
        pushed = await _pushed_modal(app, "just a log line\n")
        assert pushed == []

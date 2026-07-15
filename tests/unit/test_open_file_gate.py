"""File > Open only makes sense for Python sessions: the picker filters
to .py files and relaunches the *current* profile, so offering it for a
non-Python (e.g. cpp) session is a dead end — wrong files shown, wrong
adapter used to relaunch. Both the menu label and the action handler
must be gated (Task-12-style double gate: a keybinding bypasses the
label, so the handler has to guard too)."""

import pytest
from textual.css.query import NoMatches

from tdb.app import TdbApp
from tdb.persist import TdbConfig


async def _bare_profile_for_app():
    from tests.unit.test_controller_actions import _bare_profile

    return _bare_profile()


async def test_open_file_label_hidden_for_non_python_profile():
    app = TdbApp(program="", config=TdbConfig(), profile=await _bare_profile_for_app())
    async with app.run_test() as pilot:
        await pilot.pause()
        with pytest.raises(NoMatches):
            app.query_one("#open-file-label")


async def test_open_file_label_shown_for_python_profile():
    app = TdbApp(program="", config=TdbConfig())
    async with app.run_test() as pilot:
        await pilot.pause()
        # Should not raise — the label exists for the default (python)
        # profile.
        app.query_one("#open-file-label")


async def test_action_open_file_noop_for_non_python_profile():
    app = TdbApp(program="", config=TdbConfig(), profile=await _bare_profile_for_app())
    async with app.run_test() as pilot:
        await pilot.pause()

        pushed = []
        notified = []
        app.push_screen = lambda screen, callback=None: pushed.append(screen)
        app.notify = lambda msg, **kw: notified.append((msg, kw.get("severity")))

        app.action_open_file()

        assert pushed == []
        assert len(notified) == 1
        msg, severity = notified[0]
        assert severity == "warning"

"""File > Open is available for every language profile: the picker
filters to the profile's own extensions (registry.extensions_for) and
the dismiss callback validates the picked file matches the current
profile (registry.matches_language) before relaunching. The only
remaining gate is remote-attach / tdb.breakpoint() sessions, which have
nothing to relaunch (mirrors _restart_session's own guard,
controller.supports_restart)."""

from tdb.app import TdbApp
from tdb.persist import TdbConfig
from tdb.widgets.modals import _OpenFileModal


async def _bare_profile_for_app():
    from tests.unit.test_controller_actions import _bare_profile

    return _bare_profile()


async def test_open_file_label_shown_for_non_python_profile():
    app = TdbApp(program="", config=TdbConfig(), profile=await _bare_profile_for_app())
    async with app.run_test() as pilot:
        await pilot.pause()
        # Should not raise — the label exists for every profile now.
        app.query_one("#open-file-label")


async def test_open_file_label_shown_for_python_profile():
    app = TdbApp(program="", config=TdbConfig())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#open-file-label")


async def test_action_open_file_pushes_modal_for_non_python_profile():
    app = TdbApp(program="", config=TdbConfig(), profile=await _bare_profile_for_app())
    async with app.run_test() as pilot:
        await pilot.pause()

        pushed = []
        app.push_screen = lambda screen, callback=None: pushed.append(screen)

        app.action_open_file()

        assert len(pushed) == 1
        assert isinstance(pushed[0], _OpenFileModal)


async def test_action_open_file_noop_when_remote_attach(monkeypatch):
    app = TdbApp(program="", config=TdbConfig())
    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(
            type(app.controller), "supports_restart", property(lambda self: False)
        )

        pushed = []
        notified = []
        app.push_screen = lambda screen, callback=None: pushed.append(screen)
        app.notify = lambda msg, **kw: notified.append((msg, kw.get("severity")))

        app.action_open_file()

        assert pushed == []
        assert len(notified) == 1
        msg, severity = notified[0]
        assert severity == "warning"


async def test_action_open_file_dismiss_rejects_mismatched_language(
    monkeypatch, tmp_path
):
    """The dismiss callback validates the picked file against the
    current profile even though the modal already filtered by
    suffix (defense in depth, and cpp has no suffix filter at all)."""
    app = TdbApp(program="", config=TdbConfig())
    async with app.run_test() as pilot:
        await pilot.pause()

        restarted = []
        notified = []
        monkeypatch.setattr(app, "_restart_session", lambda **kw: restarted.append(kw))
        app.notify = lambda msg, **kw: notified.append((msg, kw.get("severity")))

        captured_callback = {}

        def fake_push_screen(screen, callback=None):
            captured_callback["cb"] = callback

        app.push_screen = fake_push_screen

        app.action_open_file()
        wrong = tmp_path / "other.rb"
        wrong.write_text("puts 1\n")
        captured_callback["cb"](str(wrong))

        assert restarted == []
        assert len(notified) == 1
        assert notified[0][1] == "warning"


async def test_action_open_file_dismiss_relaunches_matching_language(
    monkeypatch, tmp_path
):
    """A pick that matches the current profile's language reaches
    _restart_session — the mirror image of the mismatch test above."""
    app = TdbApp(program="", config=TdbConfig())
    async with app.run_test() as pilot:
        await pilot.pause()

        restarted = []
        notified = []
        monkeypatch.setattr(app, "_restart_session", lambda **kw: restarted.append(kw))
        app.notify = lambda msg, **kw: notified.append((msg, kw.get("severity")))

        captured_callback = {}

        def fake_push_screen(screen, callback=None):
            captured_callback["cb"] = callback

        app.push_screen = fake_push_screen

        app.action_open_file()
        right = tmp_path / "other.py"
        right.write_text("x = 1\n")
        captured_callback["cb"](str(right))

        assert notified == []
        assert restarted == [{"new_program": str(right), "start_immediately": False}]

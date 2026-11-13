"""_QuitConfirmModal: q / d / ESC choices and wording."""

from textual.app import App
from textual.widgets import Static

from tdb.widgets.modals import _QuitConfirmModal


class _Host(App):
    def __init__(self, **kw):
        super().__init__()
        self._kw = kw
        self.results = []

    def on_mount(self):
        self.push_screen(_QuitConfirmModal(**self._kw), self.results.append)


def _text(app) -> str:
    return "\n".join(str(s.render()) for s in app.screen.query(Static))


async def test_modal_three_lines_when_detachable():
    app = _Host(can_detach=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = _text(app)
        assert "q: quit (ends the program)" in text
        assert "d: run detached" in text
        assert "ESC: cancel" in text


async def test_modal_attach_wording():
    app = _Host(can_detach=True, ends_program=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = _text(app)
        assert "q: quit" in text
        assert "(ends the program)" not in text


async def test_modal_without_detach_ignores_d():
    app = _Host(can_detach=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "run detached" not in _text(app)
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, _QuitConfirmModal)
        assert app.results == []


async def _press(key, **kw):
    app = _Host(**kw)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press(key)
        await pilot.pause()
    return app.results


async def test_q_d_escape_results():
    assert await _press("q", can_detach=True) == ["quit"]
    assert await _press("d", can_detach=True) == ["detach"]
    assert await _press("escape", can_detach=True) == [None]

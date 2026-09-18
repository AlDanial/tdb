"""Emacs key layer for CodeEditor."""

from __future__ import annotations

from textual.app import App

from tdb.widgets.code_editor import CodeEditor, EmacsLayer


class _EdApp(App):
    def __init__(self, text: str = "alpha beta\ngamma\n"):
        super().__init__()
        self._text = text
        self.leave = 0
        self.saves = 0
        self.searches: list[bool] = []

    def compose(self):
        yield CodeEditor(self._text, scheme="emacs", id="ed")

    def on_mount(self):
        ed = self.query_one("#ed", CodeEditor)
        ed._set_layer(EmacsLayer(ed))
        ed.focus()

    def on_code_editor_leave_requested(self, msg):
        self.leave += 1

    def on_code_editor_save_requested(self, msg):
        self.saves += 1

    def on_code_editor_search_requested(self, msg):
        self.searches.append(msg.backward)


async def test_ctrl_npfb_move():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("ctrl+f", "ctrl+f", "ctrl+n")
        assert ed.cursor_location == (1, 2)
        await pilot.press("ctrl+b", "ctrl+p")
        assert ed.cursor_location == (0, 1)
        assert ed.text == "alpha beta\ngamma\n"  # ctrl+f did NOT delete a word


async def test_line_and_buffer_ends():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("ctrl+e")
        assert ed.cursor_location == (0, 10)
        await pilot.press("ctrl+a")
        assert ed.cursor_location == (0, 0)
        await pilot.press("alt+greater_than_sign")
        assert ed.cursor_location == ed.document.end
        await pilot.press("alt+less_than_sign")
        assert ed.cursor_location == (0, 0)


async def test_word_motions_alt_and_ctrl_arrow_forms():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("alt+f")
        first = ed.cursor_location
        assert first[1] > 0
        await pilot.press("ctrl+left")
        assert ed.cursor_location == (0, 0)
        await pilot.press("ctrl+right")
        assert ed.cursor_location == first


async def test_kill_and_yank():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        ed.move_cursor((0, 6))
        await pilot.press("ctrl+k")
        assert ed.text == "alpha \ngamma\n"
        await pilot.press("ctrl+k")  # at end of line: kills the newline
        assert ed.text == "alpha gamma\n"
        await pilot.press("ctrl+y")
        assert ed.text == "alpha \ngamma\n"


async def test_ctrl_d_and_undo():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("ctrl+d")
        assert ed.text == "lpha beta\ngamma\n"
        await pilot.press("ctrl+underscore")
        assert ed.text == "alpha beta\ngamma\n"


async def test_ctrl_x_chords_and_ctrl_g():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("ctrl+x", "ctrl+s")
        assert app.saves == 1
        await pilot.press("ctrl+x", "ctrl+c")
        assert app.leave == 1
        await pilot.press("ctrl+x", "ctrl+g", "ctrl+s")
        assert app.saves == 1  # chord cancelled; ctrl+s is search on emacs
        assert app.searches == [False]
        await pilot.press("ctrl+x", "z")
        assert ed.text == "alpha beta\ngamma\n"  # unknown chord swallowed
        await pilot.press("ctrl+r")
        assert app.searches == [False, True]


async def test_plain_ctrl_s_is_search_not_save():
    app = _EdApp()
    async with app.run_test() as pilot:
        await pilot.press("ctrl+s")
        assert app.saves == 0
        assert app.searches == [False]


async def test_ctrl_p_claimed_from_app_on_emacs_scheme():
    """emacs binds ctrl+p to cursor-up, so it must strip the App's
    ctrl+p (command palette) priority binding out of the chain."""
    app = _EdApp()
    async with app.run_test():
        ed = app.query_one("#ed", CodeEditor)
        assert ed._layer is not None
        assert ed.check_consume_key("ctrl+p") is True


async def test_escape_leaves():
    app = _EdApp()
    async with app.run_test() as pilot:
        await pilot.press("escape")
        assert app.leave == 1

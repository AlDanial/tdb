"""CodeEditor: the TextArea subclass behind Code View Edit mode, in
its Notepad-style (scheme="default") configuration. Layers for emacs
and vim have their own test modules."""

from __future__ import annotations

from textual.app import App

from tdb.widgets.code_editor import (
    CodeEditor,
    _UnsavedChangesModal,
    editor_language_for,
)


class _EdApp(App):
    def __init__(self, text: str = "a = 1\nb = 2\n", scheme: str = "default"):
        super().__init__()
        self._text = text
        self._scheme = scheme
        self.leave: list[bool] = []
        self.saves = 0
        self.searches: list[bool] = []

    def compose(self):
        yield CodeEditor(self._text, scheme=self._scheme, id="ed")

    def on_mount(self):
        self.query_one("#ed", CodeEditor).focus()

    def on_code_editor_leave_requested(self, msg: CodeEditor.LeaveRequested):
        self.leave.append(msg.discard)

    def on_code_editor_save_requested(self, msg: CodeEditor.SaveRequested):
        self.saves += 1

    def on_code_editor_search_requested(self, msg: CodeEditor.SearchRequested):
        self.searches.append(msg.backward)


def test_editor_language_for_without_tree_sitter(monkeypatch):
    import textual._tree_sitter as ts

    monkeypatch.setattr(ts, "TREE_SITTER", False)
    assert editor_language_for("python") is None


def test_editor_language_for_with_tree_sitter(monkeypatch):
    import textual._tree_sitter as ts

    monkeypatch.setattr(ts, "TREE_SITTER", True)
    assert editor_language_for("python") == "python"
    assert editor_language_for("bash") == "bash"
    assert editor_language_for("perl") is None  # not a Textual builtin
    assert editor_language_for(None) is None


async def test_typing_edits_and_marks_dirty():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        assert ed.is_dirty is False
        ed.move_cursor((0, 5))
        await pilot.press("2")
        assert ed.text == "a = 12\nb = 2\n"
        assert ed.is_dirty is True
        ed.mark_clean()
        assert ed.is_dirty is False


async def test_arrows_reach_textarea_bindings():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("down", "end")
        assert ed.cursor_location == (1, 5)


async def test_escape_posts_leave_requested():
    app = _EdApp()
    async with app.run_test() as pilot:
        await pilot.press("escape")
        assert app.leave == [False]
        # Focus stayed on the editor (TextArea's own Esc->focus_next
        # was pre-empted).
        assert app.focused is app.query_one("#ed", CodeEditor)


async def test_ctrl_s_posts_save_requested_on_default_scheme():
    app = _EdApp()
    async with app.run_test() as pilot:
        await pilot.press("ctrl+s")
        assert app.saves == 1


async def test_insert_key_is_swallowed():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("insert")
        assert ed.text == "a = 1\nb = 2\n"


async def test_tab_inserts_indentation():
    app = _EdApp(text="x\n")
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("tab")
        assert ed.text.startswith("    x")


async def test_find_moves_cursor_and_wraps():
    app = _EdApp(text="alpha\nbeta\ngamma\nbeta\n")
    async with app.run_test():
        ed = app.query_one("#ed", CodeEditor)
        assert ed.find("beta", backward=False) is True
        assert ed.cursor_location == (1, 0)
        assert ed.find("beta", backward=False) is True
        assert ed.cursor_location == (3, 0)
        assert ed.find("beta", backward=False) is True  # wraps
        assert ed.cursor_location == (1, 0)
        assert ed.find("BETA", backward=True) is True  # case-insensitive
        assert ed.cursor_location == (3, 0)
        assert ed.find("zzz", backward=False) is False


async def test_submode_is_none_for_default_scheme():
    app = _EdApp()
    async with app.run_test():
        ed = app.query_one("#ed", CodeEditor)
        assert ed.submode is None
        assert ed.command_text == ""


class _ModalApp(App):
    def __init__(self):
        super().__init__()
        self.result: str | None = None

    def on_mount(self):
        self.push_screen(_UnsavedChangesModal("prog.py"), callback=self._done)

    def _done(self, result: str | None):
        self.result = result


async def test_unsaved_modal_keys():
    for key, expected in (("s", "save"), ("d", "discard"), ("escape", "cancel")):
        app = _ModalApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, _UnsavedChangesModal)
            await pilot.press(key)
            await pilot.pause()
            assert app.result == expected

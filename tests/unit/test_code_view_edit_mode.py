"""CodeView Edit mode: the Esc cycle, entering/leaving the editor,
save, dirty state, refusal cases, and deferred source loads."""

from __future__ import annotations

from pathlib import Path

from textual.app import App

from tdb.keybindings import KeybindingConfig, Mode
from tdb.widgets.code_editor import CodeEditor, _UnsavedChangesModal
from tdb.widgets.code_view import CodeView


class _CVApp(App):
    def __init__(self, scheme: str = "default"):
        super().__init__()
        self._scheme = scheme
        self.saved: list[tuple[str, list[str], list[str]]] = []
        self.mode_changes: list[str] = []

    def compose(self):
        yield CodeView(id="cv")

    def on_mount(self):
        cv = self.query_one("#cv", CodeView)
        cv.keybindings = KeybindingConfig.from_scheme(self._scheme)
        cv.focus()

    def on_code_view_file_saved(self, msg: CodeView.FileSaved):
        self.saved.append((msg.path, msg.old_lines, msg.new_lines))

    def on_code_view_mode_changed(self, msg: CodeView.ModeChanged):
        self.mode_changes.append(self.query_one("#cv", CodeView).mode_label())


def _write(tmp_path, text="x = 1\ny = 2\n"):
    p = tmp_path / "prog.py"
    p.write_text(text, encoding="utf-8")
    return str(p)


async def test_esc_cycles_debug_navigate_edit_debug(tmp_path):
    app = _CVApp()
    async with app.run_test() as pilot:
        cv = app.query_one("#cv", CodeView)
        cv.load_file(_write(tmp_path))
        assert cv.mode == Mode.DEBUG
        await pilot.press("escape")
        assert cv.mode == Mode.NAVIGATION
        await pilot.press("escape")
        await pilot.pause()
        assert cv.mode == Mode.EDIT
        assert cv.is_editing
        assert isinstance(app.focused, CodeEditor)
        await pilot.press("escape")
        await pilot.pause()
        assert cv.mode == Mode.DEBUG
        assert not cv.is_editing
        assert app.focused is cv


async def test_edit_refused_without_file_skips_to_debug():
    app = _CVApp()
    async with app.run_test() as pilot:
        cv = app.query_one("#cv", CodeView)
        await pilot.press("escape", "escape")
        await pilot.pause()
        assert cv.mode == Mode.DEBUG
        assert cv.edit_refusal_reason() is not None


async def test_edit_refused_for_in_memory_source_and_when_disabled(tmp_path):
    app = _CVApp()
    async with app.run_test():
        cv = app.query_one("#cv", CodeView)
        cv.load_content("x = 1\n", "/remote/prog.py")
        assert "not on this machine" in cv.edit_refusal_reason()
        cv.load_file(_write(tmp_path))
        assert cv.edit_refusal_reason() is None
        cv.edit_enabled = False
        assert cv.edit_refusal_reason() is not None
        assert await cv.enter_edit_mode() is False


async def test_editor_opens_at_cursor_line_and_leaves_cursor_where_editor_was(tmp_path):
    app = _CVApp()
    async with app.run_test() as pilot:
        cv = app.query_one("#cv", CodeView)
        cv.load_file(_write(tmp_path, "a\nb\nc\nd\n"))
        cv.cursor_line = 3
        assert await cv.enter_edit_mode()
        await pilot.pause()
        ed = app.query_one(CodeEditor)
        assert ed.cursor_location == (2, 0)
        ed.move_cursor((0, 0))
        cv.leave_edit_mode()
        await pilot.pause()
        assert cv.cursor_line == 1


async def test_typing_marks_dirty_and_save_writes_file(tmp_path):
    app = _CVApp()
    async with app.run_test() as pilot:
        cv = app.query_one("#cv", CodeView)
        path = _write(tmp_path)
        cv.load_file(path)
        await pilot.press("escape", "escape")
        await pilot.pause()
        ed = app.query_one(CodeEditor)
        ed.move_cursor((0, 5))
        await pilot.press("0")
        assert cv.is_dirty
        assert cv.mode_label() == "Edit*"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not cv.is_dirty
        assert cv.mode_label() == "Edit"
        assert Path(path).read_text(encoding="utf-8") == "x = 10\ny = 2\n"
        assert app.saved == [(path, ["x = 1", "y = 2"], ["x = 10", "y = 2"])]


async def test_save_then_leave_reinstalls_source_for_rerender(tmp_path):
    app = _CVApp()
    async with app.run_test() as pilot:
        cv = app.query_one("#cv", CodeView)
        path = _write(tmp_path)
        cv.load_file(path)
        await pilot.press("escape", "escape")
        await pilot.pause()
        await pilot.press("z")
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert cv.lines()[0] == "zx = 1"
        assert cv._line_text(0).plain.endswith("zx = 1")


async def test_leave_with_dirty_buffer_prompts_and_cancel_keeps_editing(tmp_path):
    app = _CVApp()
    async with app.run_test() as pilot:
        cv = app.query_one("#cv", CodeView)
        path = _write(tmp_path)
        cv.load_file(path)
        await pilot.press("escape", "escape")
        await pilot.pause()
        await pilot.press("z")
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, _UnsavedChangesModal)
        await pilot.press("escape")  # cancel
        await pilot.pause()
        assert cv.is_editing and cv.is_dirty


async def test_leave_with_dirty_buffer_discard_reloads_disk(tmp_path):
    app = _CVApp()
    async with app.run_test() as pilot:
        cv = app.query_one("#cv", CodeView)
        path = _write(tmp_path)
        cv.load_file(path)
        await pilot.press("escape", "escape")
        await pilot.pause()
        await pilot.press("z")
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        assert not cv.is_editing
        assert cv.lines() == ["x = 1", "y = 2"]
        assert Path(path).read_text(encoding="utf-8") == "x = 1\ny = 2\n"


async def test_leave_with_dirty_buffer_save_writes_and_leaves(tmp_path):
    app = _CVApp()
    async with app.run_test() as pilot:
        cv = app.query_one("#cv", CodeView)
        path = _write(tmp_path)
        cv.load_file(path)
        await pilot.press("escape", "escape")
        await pilot.pause()
        await pilot.press("z")
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        assert not cv.is_editing
        assert cv.lines()[0] == "zx = 1"
        assert Path(path).read_text(encoding="utf-8") == "zx = 1\ny = 2\n"


async def test_save_failure_notifies_and_stays_dirty(tmp_path, monkeypatch):
    import tdb.widgets.code_view as cvmod

    def boom(path, text):
        raise PermissionError("read-only filesystem")

    monkeypatch.setattr(cvmod, "atomic_write_text", boom)
    app = _CVApp()
    async with app.run_test() as pilot:
        cv = app.query_one("#cv", CodeView)
        cv.load_file(_write(tmp_path))
        notes: list[str] = []
        app.notify = lambda msg, **kw: notes.append(msg)
        await pilot.press("escape", "escape")
        await pilot.pause()
        await pilot.press("z")
        assert cv.save_file() is False
        assert cv.is_dirty
        assert any("read-only" in n for n in notes)
        assert app.saved == []


async def test_load_for_other_file_is_deferred_while_editing(tmp_path):
    app = _CVApp()
    async with app.run_test() as pilot:
        cv = app.query_one("#cv", CodeView)
        first = _write(tmp_path)
        other = str(tmp_path / "other.py")
        Path(other).write_text("o = 1\n")
        cv.load_file(first)
        await pilot.press("escape", "escape")
        await pilot.pause()
        cv.load_file(other)
        assert cv.is_editing
        assert cv.source_path == first
        cv.leave_edit_mode()
        await pilot.pause()
        assert cv.source_path == other
        assert cv.lines() == ["o = 1"]


async def test_arrow_keys_reach_editor_in_edit_mode(tmp_path):
    app = _CVApp()
    async with app.run_test() as pilot:
        cv = app.query_one("#cv", CodeView)
        cv.load_file(_write(tmp_path, "a\nb\nc\n"))
        await pilot.press("escape", "escape")
        await pilot.pause()
        ed = app.query_one(CodeEditor)
        await pilot.press("down", "down")
        assert ed.cursor_location == (2, 0)
        assert cv.cursor_line == 1  # CodeView did not intercept


async def test_revert_to_disk_restores_text_and_stays_editing(tmp_path):
    app = _CVApp()
    async with app.run_test() as pilot:
        cv = app.query_one("#cv", CodeView)
        cv.load_file(_write(tmp_path))
        await pilot.press("escape", "escape")
        await pilot.pause()
        await pilot.press("z")
        assert cv.is_dirty
        assert cv.revert_to_disk() is True
        await pilot.pause()
        assert cv.is_editing and not cv.is_dirty
        assert app.query_one(CodeEditor).text == "x = 1\ny = 2\n"


async def test_focus_on_code_view_redirects_to_editor(tmp_path):
    app = _CVApp()
    async with app.run_test() as pilot:
        cv = app.query_one("#cv", CodeView)
        cv.load_file(_write(tmp_path))
        await pilot.press("escape", "escape")
        await pilot.pause()
        cv.focus()
        await pilot.pause()
        assert isinstance(app.focused, CodeEditor)

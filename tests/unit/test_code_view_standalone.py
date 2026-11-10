"""CodeView.standalone (`tdb --vi FILE`): leaving Edit mode posts
EditClosed instead of tearing the editor down; confirm_unsaved and
load_new_file."""

from __future__ import annotations

from pathlib import Path

from textual.app import App

import tdb.widgets.code_view as code_view_mod
from tdb.keybindings import KeybindingConfig, Mode
from tdb.widgets.code_editor import CodeEditor, _UnsavedChangesModal
from tdb.widgets.code_view import CodeView

TEXT = "x = 1\ny = 2\n"


class _CVApp(App):
    def __init__(self, scheme: str = "vim", standalone: bool = True):
        super().__init__()
        self._scheme = scheme
        self._standalone = standalone
        self.closed = 0

    def compose(self):
        yield CodeView(id="cv")

    def on_mount(self):
        self.cv = cv = self.query_one("#cv", CodeView)
        cv.keybindings = KeybindingConfig.from_scheme(self._scheme)
        cv.standalone = self._standalone
        cv.focus()

    def on_code_view_edit_closed(self, msg: CodeView.EditClosed):
        self.closed += 1


def _write(tmp_path, text=TEXT) -> Path:
    p = tmp_path / "prog.py"
    p.write_text(text, encoding="utf-8")
    return p


async def _edit(pilot, cv: CodeView, path: Path) -> None:
    cv.load_file(str(path))
    assert await cv.enter_edit_mode()
    await pilot.pause()


async def _type(pilot, keys: str) -> None:
    for ch in keys:
        await pilot.press(
            {"\r": "enter", ":": "colon", "!": "exclamation_mark"}.get(ch, ch)
        )
    await pilot.pause()


async def test_view_standalone_q_clean_posts_editclosed(tmp_path):
    app = _CVApp()
    async with app.run_test() as pilot:
        cv = app.cv
        await _edit(pilot, cv, _write(tmp_path))
        assert cv._editor.standalone
        await _type(pilot, ":q\r")
        await pilot.pause()
        assert app.closed == 1
        assert cv.is_editing
        assert cv._editor is not None and cv._editor.is_mounted
        assert cv.mode == Mode.EDIT


async def test_view_standalone_escape_never_closes(tmp_path):
    app = _CVApp()
    async with app.run_test() as pilot:
        cv = app.cv
        await _edit(pilot, cv, _write(tmp_path))
        await pilot.press("escape", "escape")
        # Focus somehow on the container itself: Esc still must not exit.
        cv.focus()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.closed == 0
        assert cv.is_editing


async def test_view_standalone_q_dirty_prompts(tmp_path):
    app = _CVApp()
    async with app.run_test() as pilot:
        cv = app.cv
        await _edit(pilot, cv, _write(tmp_path))
        await _type(pilot, "x")
        assert cv._editor.is_dirty
        await _type(pilot, ":q\r")
        await pilot.pause()
        assert isinstance(app.screen, _UnsavedChangesModal)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, _UnsavedChangesModal)
        assert app.closed == 0
        assert cv.is_editing
        assert app.focused is cv._editor


async def test_view_standalone_q_dirty_save(tmp_path):
    app = _CVApp()
    async with app.run_test() as pilot:
        cv = app.cv
        path = _write(tmp_path)
        await _edit(pilot, cv, path)
        await _type(pilot, "x")
        await _type(pilot, ":q\r")
        await pilot.pause()
        assert isinstance(app.screen, _UnsavedChangesModal)
        await pilot.press("s")
        await pilot.pause()
        assert path.read_text(encoding="utf-8") == " = 1\ny = 2\n"
        assert app.closed == 1
        assert cv.is_editing


async def test_view_standalone_q_dirty_discard(tmp_path):
    app = _CVApp()
    async with app.run_test() as pilot:
        cv = app.cv
        path = _write(tmp_path)
        await _edit(pilot, cv, path)
        await _type(pilot, "x")
        await _type(pilot, ":q\r")
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        assert path.read_text(encoding="utf-8") == TEXT
        assert app.closed == 1


async def test_view_standalone_q_bang_discards(tmp_path):
    app = _CVApp()
    async with app.run_test() as pilot:
        cv = app.cv
        path = _write(tmp_path)
        await _edit(pilot, cv, path)
        await _type(pilot, "x")
        await _type(pilot, ":q!\r")
        await pilot.pause()
        assert not isinstance(app.screen, _UnsavedChangesModal)
        assert app.closed == 1
        assert path.read_text(encoding="utf-8") == TEXT
        assert cv.is_editing


async def test_view_standalone_discard_edits(tmp_path):
    # Edit menu "Discard and Exit Edit Mode".
    app = _CVApp()
    async with app.run_test() as pilot:
        cv = app.cv
        path = _write(tmp_path)
        await _edit(pilot, cv, path)
        await _type(pilot, "x")
        cv.discard_edits()
        await pilot.pause()
        assert app.closed == 1
        assert path.read_text(encoding="utf-8") == TEXT
        assert cv.is_editing


async def test_view_standalone_wq(tmp_path):
    app = _CVApp()
    async with app.run_test() as pilot:
        cv = app.cv
        path = _write(tmp_path)
        await _edit(pilot, cv, path)
        await _type(pilot, "x")
        await _type(pilot, ":wq\r")
        await pilot.pause()
        assert not isinstance(app.screen, _UnsavedChangesModal)
        assert path.read_text(encoding="utf-8") == " = 1\ny = 2\n"
        assert app.closed == 1


async def test_view_standalone_x(tmp_path):
    app = _CVApp()
    async with app.run_test() as pilot:
        cv = app.cv
        path = _write(tmp_path)
        await _edit(pilot, cv, path)
        await _type(pilot, "x")
        await _type(pilot, ":x\r")
        await pilot.pause()
        assert path.read_text(encoding="utf-8") == " = 1\ny = 2\n"
        assert app.closed == 1


async def test_non_standalone_q_still_tears_down(tmp_path):
    app = _CVApp(standalone=False)
    async with app.run_test() as pilot:
        cv = app.cv
        await _edit(pilot, cv, _write(tmp_path))
        assert not cv._editor.standalone
        await _type(pilot, ":q\r")
        await pilot.pause()
        assert app.closed == 0
        assert not cv.is_editing
        assert cv.mode == Mode.DEBUG


async def test_load_new_file_touches_nothing(tmp_path):
    app = _CVApp()
    async with app.run_test() as pilot:
        cv = app.cv
        p = tmp_path / "new.py"
        cv.load_new_file(str(p))
        assert cv.source_path == str(p)
        assert cv.edit_refusal_reason() is None
        assert await cv.enter_edit_mode() is True
        await pilot.pause()
        assert cv._editor.text == ""
        assert cv._editor.file_text == ""
        assert not p.exists()
        await _type(pilot, "ihi")
        await pilot.press("escape")
        await _type(pilot, ":wq\r")
        await pilot.pause()
        assert p.read_text(encoding="utf-8") == "hi"
        assert app.closed == 1


async def test_confirm_unsaved_clean_calls_then_now(tmp_path):
    app = _CVApp()
    async with app.run_test() as pilot:
        cv = app.cv
        calls: list[int] = []
        cv.confirm_unsaved(lambda: calls.append(1))  # no editor
        await _edit(pilot, cv, _write(tmp_path))
        cv.confirm_unsaved(lambda: calls.append(2))  # clean editor
        assert calls == [1, 2]
        assert not isinstance(app.screen, _UnsavedChangesModal)


async def test_confirm_unsaved_save_failure_does_not_continue(tmp_path, monkeypatch):
    app = _CVApp()
    async with app.run_test() as pilot:
        cv = app.cv
        path = _write(tmp_path)
        await _edit(pilot, cv, path)
        await _type(pilot, "x")

        def boom(path: str, text: str) -> None:
            raise OSError("read-only file system")

        monkeypatch.setattr(code_view_mod, "save_text", boom)
        calls: list[int] = []
        cv.confirm_unsaved(lambda: calls.append(1))
        await pilot.pause()
        assert isinstance(app.screen, _UnsavedChangesModal)
        await pilot.press("s")
        await pilot.pause()
        assert calls == []
        assert cv._editor.is_dirty
        assert app.closed == 0
        assert path.read_text(encoding="utf-8") == TEXT
        assert app.focused is cv._editor
        assert isinstance(app.focused, CodeEditor)


async def test_replace_file_switches_without_editclosed(tmp_path):
    app = _CVApp()
    async with app.run_test() as pilot:
        cv = app.cv
        await _edit(pilot, cv, _write(tmp_path))
        await _type(pilot, "x")
        other = tmp_path / "other.py"
        other.write_text("z = 3\n", encoding="utf-8")
        cv.replace_file(str(other))
        await pilot.pause()
        await pilot.pause()
        assert app.closed == 0
        assert cv.source_path == str(other)
        assert cv.mode == Mode.EDIT
        assert cv._editor is not None and cv._editor.is_mounted
        assert cv._editor.file_text == "z = 3\n"
        assert not cv._editor.is_dirty
        assert app.focused is cv._editor
        assert len(cv.query(CodeEditor)) == 1


async def test_replace_file_missing_path_opens_empty_buffer(tmp_path):
    app = _CVApp()
    async with app.run_test() as pilot:
        cv = app.cv
        await _edit(pilot, cv, _write(tmp_path))
        new = tmp_path / "new.py"
        cv.replace_file(str(new))
        await pilot.pause()
        await pilot.pause()
        assert app.closed == 0
        assert cv.source_path == str(new)
        assert cv.is_editing
        assert cv._editor.file_text == ""
        assert not new.exists()

"""`ViApp` (`tdb --vi FILE`): full-screen vim editor, no debugger."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from textual.widgets import Label

from tdb.keybindings import Mode
from tdb.vi_app import ViApp
from tdb.widgets.code_editor import _UnsavedChangesModal
from tdb.widgets.code_view import CodeView
from tdb.widgets.menu_bar import MenuBar, _MenuDropdown
from tdb.widgets.modals import _AboutModal, _OpenFileModal

TEXT = "one\ntwo\nthree\n"

_KEY_NAMES = {
    "\r": "enter",
    ":": "colon",
    "!": "exclamation_mark",
    " ": "space",
}


@pytest.fixture(autouse=True)
def _config_dir(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("TDB_CONFIG_DIR", str(cfg))
    return cfg


def _write(tmp_path, name="f.txt", text=TEXT) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


async def _type(pilot, keys: str) -> None:
    for ch in keys:
        await pilot.press(_KEY_NAMES.get(ch, ch))
    await pilot.pause()


def _cv(app) -> CodeView:
    return app.query_one("#code-view", CodeView)


def _running(app) -> bool:
    return app.return_code is None and app.is_running


async def test_layout_full_screen(tmp_path):
    app = ViApp(str(_write(tmp_path)))
    async with app.run_test() as pilot:
        await pilot.pause()
        for name in (
            "ConsoleView",
            "VariableView",
            "StackView",
            "BreakpointView",
            "EvaluateConsole",
        ):
            assert not list(app.query(name)), name
        top_ids = [
            w.id for w in app.screen.children if not isinstance(w, _MenuDropdown)
        ]
        assert top_ids == ["menu-bar", "code-view"]
        menu = app.query_one("#menu-bar", MenuBar)
        cv = _cv(app)
        assert cv.size.height == app.screen.size.height - menu.size.height
        assert cv.size.width == app.screen.size.width


async def test_menu_labels(tmp_path):
    app = ViApp(str(_write(tmp_path)))
    async with app.run_test() as pilot:
        await pilot.pause()
        menu = app.query_one("#menu-bar", MenuBar)
        labels = [
            str(lbl.render()).strip()
            for lbl in menu.query(Label)
            if lbl.has_class("menu-label") or lbl.has_class("action-label")
        ]
        assert labels == ["File", "Help"]


async def test_starts_in_vim_normal_edit(tmp_path, _config_dir):
    (_config_dir / "config.json").write_text(json.dumps({"keybindings": "emacs"}))
    app = ViApp(str(_write(tmp_path)))
    async with app.run_test() as pilot:
        await pilot.pause()
        cv = _cv(app)
        assert cv.keybindings.scheme == "vim"
        assert cv.mode == Mode.EDIT
        assert cv.standalone
        # Normal mode: "x" deletes a char instead of inserting one.
        await _type(pilot, "x")
        assert cv._editor.file_text == "ne\ntwo\nthree\n"


async def test_theme_from_config(tmp_path, _config_dir):
    (_config_dir / "config.json").write_text(json.dumps({"theme": "nord"}))
    app = ViApp(str(_write(tmp_path)))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.theme == "nord"


async def test_q_exits(tmp_path, _config_dir):
    app = ViApp(str(_write(tmp_path)))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, ":q\r")
        await pilot.pause()
    assert app.return_code == 0
    assert not (_config_dir / "breakpoints.json").exists()
    assert not (_config_dir / "config.json").exists()


async def test_q_dirty_dialog_then_cancel(tmp_path):
    f = _write(tmp_path)
    app = ViApp(str(f))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "x:q\r")
        assert isinstance(app.screen, _UnsavedChangesModal)
        await pilot.press("escape")
        await pilot.pause()
        assert _running(app)
        assert not isinstance(app.screen, _UnsavedChangesModal)
        assert _cv(app)._editor.is_dirty
    assert f.read_text() == TEXT


async def test_q_dirty_dialog_save_exits(tmp_path):
    f = _write(tmp_path)
    app = ViApp(str(f))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "x:q\r")
        assert isinstance(app.screen, _UnsavedChangesModal)
        await pilot.press("s")
        await pilot.pause()
    assert app.return_code == 0
    assert f.read_text() == "ne\ntwo\nthree\n"


async def test_q_bang_exits_without_saving(tmp_path):
    f = _write(tmp_path)
    app = ViApp(str(f))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "x:q!\r")
        await pilot.pause()
    assert app.return_code == 0
    assert f.read_text() == TEXT


@pytest.mark.parametrize("cmd", [":wq\r", ":x\r"])
async def test_wq_saves_and_exits(tmp_path, cmd):
    f = _write(tmp_path)
    app = ViApp(str(f))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "x" + cmd)
        await pilot.pause()
    assert app.return_code == 0
    assert f.read_text() == "ne\ntwo\nthree\n"


async def test_w_saves_and_stays(tmp_path):
    f = _write(tmp_path)
    app = ViApp(str(f))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "x:w\r")
        await pilot.pause()
        assert _running(app)
        assert _cv(app).is_editing
    assert f.read_text() == "ne\ntwo\nthree\n"


async def test_ctrl_q_clean_exits(tmp_path):
    app = ViApp(str(_write(tmp_path)))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+q")
        await pilot.pause()
    assert app.return_code == 0


async def test_ctrl_q_dirty_dialog(tmp_path):
    f = _write(tmp_path)
    app = ViApp(str(f))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "x")
        await pilot.press("ctrl+q")
        await pilot.pause()
        assert isinstance(app.screen, _UnsavedChangesModal)
        # A second Ctrl+Q while the prompt is up neither quits nor stacks
        # another prompt.
        await pilot.press("ctrl+q")
        await pilot.pause()
        assert _running(app)
        assert sum(isinstance(s, _UnsavedChangesModal) for s in app.screen_stack) == 1
        await pilot.press("d")
        await pilot.pause()
    assert app.return_code == 0
    assert f.read_text() == TEXT


async def test_quit_action_dirty_dialog(tmp_path):
    """Textual's own quit action (command palette "Quit", any stray
    binding) must not bypass the unsaved-changes prompt."""
    app = ViApp(str(_write(tmp_path)))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "x")
        await app.action_quit()
        await pilot.pause()
        assert _running(app)
        assert isinstance(app.screen, _UnsavedChangesModal)


async def test_escape_does_not_exit(tmp_path):
    app = ViApp(str(_write(tmp_path)))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape", "escape", "escape")
        await pilot.pause()
        assert _running(app)
        assert _cv(app).is_editing


async def test_ctrl_p_moves_up_no_palette_ctrl_c_no_quit(tmp_path):
    app = ViApp(str(_write(tmp_path)))
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = _cv(app)._editor
        await _type(pilot, "jj")
        assert editor.cursor_location[0] == 2
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert editor.cursor_location[0] == 1
        assert app.screen is app.screen_stack[0]
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert _running(app)
        assert _cv(app).is_editing


async def test_ctrl_f_ctrl_b_page(tmp_path):
    f = _write(tmp_path, text="".join(f"line {i}\n" for i in range(200)))
    app = ViApp(str(f))
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = _cv(app)._editor
        await pilot.press("ctrl+f", "ctrl+f")
        await pilot.pause()
        paged_row = editor.cursor_location[0]
        paged_top = editor.scroll_offset.y
        assert paged_row > 20 and paged_top > 20
        await pilot.press("ctrl+b")
        await pilot.pause()
        # As in vim, Ctrl+B scrolls back a page; the cursor lands on the
        # window's last line, which may be the row it was on.
        assert editor.scroll_offset.y < paged_top
        assert editor.cursor_location[0] <= paged_row
        assert _running(app)


async def test_new_file_quit_creates_nothing(tmp_path):
    f = tmp_path / "new.txt"
    app = ViApp(str(f))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert _cv(app).is_editing
        await _type(pilot, ":q\r")
        await pilot.pause()
    assert app.return_code == 0
    assert not f.exists()


async def test_new_file_write_creates(tmp_path):
    f = tmp_path / "new.txt"
    app = ViApp(str(f))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "itext")
        await pilot.press("escape")
        await _type(pilot, ":w\r")
        await pilot.pause()
        assert _running(app)
    # A new file gets a final newline, like vim's 'fixeol'.
    assert f.read_bytes() == b"text\n"


async def test_alt_f_and_ctrl_right_open_file_picker(tmp_path):
    app = ViApp(str(_write(tmp_path)))
    async with app.run_test() as pilot:
        await pilot.pause()
        for key in ("alt+f", "ctrl+right"):
            await pilot.press(key)
            await pilot.pause()
            assert isinstance(app.screen, _OpenFileModal), key
            assert app.screen._suffixes == ()
            assert app.screen._title == "Open file to edit"
            app.screen.dismiss(None)
            await pilot.pause()
            assert _cv(app).is_editing


async def test_alt_h_opens_help(tmp_path):
    app = ViApp(str(_write(tmp_path)))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("alt+h")
        await pilot.pause()
        menu = app.query_one("#menu-bar", MenuBar)
        assert menu._open_menu == "Help"
        dropdown = app.screen.query_one("#dropdown-help", _MenuDropdown)
        assert dropdown.has_class("--visible")
        await pilot.press("down", "enter")  # Documentation, About
        await pilot.pause()
        assert isinstance(app.screen, _AboutModal)
        await pilot.press("escape")
        await pilot.pause()
        assert _running(app)


async def test_file_menu_switch_dirty_prompts(tmp_path):
    f = _write(tmp_path)
    other = _write(tmp_path, "other.txt", "alpha\nbeta\n")
    app = ViApp(str(f))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "x")
        app.action_open_file()
        await pilot.pause()
        assert isinstance(app.screen, _OpenFileModal)
        app.screen.dismiss(str(other))
        await pilot.pause()
        assert isinstance(app.screen, _UnsavedChangesModal)
        await pilot.press("d")
        await pilot.pause()
        await pilot.pause()
        cv = _cv(app)
        assert _running(app)
        assert cv.source_path == str(other)
        assert cv.is_editing
        assert cv._editor.file_text == "alpha\nbeta\n"
        # The new buffer is live: :q now exits (clean).
        await _type(pilot, ":q\r")
        await pilot.pause()
    assert app.return_code == 0
    assert f.read_text() == TEXT


async def test_file_menu_switch_clean_no_prompt(tmp_path):
    f = _write(tmp_path)
    other = _write(tmp_path, "other.txt", "alpha\nbeta\n")
    app = ViApp(str(f))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_file()
        await pilot.pause()
        app.screen.dismiss(str(other))
        await pilot.pause()
        await pilot.pause()
        cv = _cv(app)
        assert cv.source_path == str(other)
        assert cv.is_editing


async def test_file_menu_rejects_binary(tmp_path):
    f = _write(tmp_path)
    binary = tmp_path / "prog"
    binary.write_bytes(b"\x00ELF")
    app = ViApp(str(f))
    notes: list[str] = []
    async with app.run_test() as pilot:
        await pilot.pause()
        orig_notify = app.notify
        app.notify = lambda msg, *a, **k: (notes.append(msg), orig_notify(msg, *a, **k))
        await _type(pilot, "x")
        app.action_open_file()
        await pilot.pause()
        app.screen.dismiss(str(binary))
        await pilot.pause()
        cv = _cv(app)
        assert not isinstance(app.screen, _UnsavedChangesModal)
        assert cv.source_path == str(f)
        assert cv._editor.is_dirty
        assert any("binary" in n for n in notes)


async def test_file_menu_rejects_directory(tmp_path):
    f = _write(tmp_path)
    app = ViApp(str(f))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_file()
        await pilot.pause()
        app.screen.dismiss(str(tmp_path))
        await pilot.pause()
        assert _cv(app).source_path == str(f)
        assert _cv(app).is_editing


async def test_existing_file_without_final_newline_saves_without(tmp_path):
    f = tmp_path / "noeol.txt"
    f.write_bytes(b"one\ntwo")
    app = ViApp(str(f))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "x:wq\r")
        await pilot.pause()
    assert app.return_code == 0
    assert f.read_bytes() == b"ne\ntwo"

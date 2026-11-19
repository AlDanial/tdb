"""Live-preview color-theme picker: `T` in the Code View and
Configure -> Color Theme."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import OptionList

from tdb.app import TdbApp
from tdb.keybindings import KeybindingConfig, Mode
from tdb.persist import TdbConfig
from tdb.session.controller import DebugController
from tdb.widgets.code_view import CodeView
from tdb.widgets.theme_picker import ThemePickerModal

START = "textual-dark"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    async def no_op(self, *args, **kwargs):
        pass

    monkeypatch.setattr(DebugController, "start", no_op)
    monkeypatch.setattr(DebugController, "stop", no_op)


@pytest.fixture
def saved(monkeypatch):
    calls: list[str | None] = []
    monkeypatch.setattr("tdb.app.save_config", lambda cfg: calls.append(cfg.theme))
    return calls


def _write(tmp_path: Path) -> str:
    p = tmp_path / "prog.py"
    p.write_text("x = 1\ny = 2\n", encoding="utf-8")
    return str(p)


async def _open(app: TdbApp, pilot, tmp_path: Path) -> CodeView:
    cv = app.query_one("#code-view", CodeView)
    cv.load_file(_write(tmp_path))
    cv.focus()
    await pilot.pause()
    await pilot.press("T")
    await pilot.pause()
    return cv


def _pickers(app: TdbApp) -> list:
    return [s for s in app.screen_stack if isinstance(s, ThemePickerModal)]


async def test_t_opens_picker_and_down_previews(tmp_path, saved):
    app = TdbApp(program="", config=TdbConfig(theme=START))
    async with app.run_test() as pilot:
        await pilot.pause()
        saved.clear()
        await _open(app, pilot, tmp_path)
        assert isinstance(app.screen, ThemePickerModal)
        ol = app.screen.query_one(OptionList)
        assert ol.get_option_at_index(ol.highlighted).id == START
        await pilot.press("down")
        await pilot.pause()
        assert app.theme != START
        assert saved == []


async def test_escape_reverts_and_does_not_save(tmp_path, saved):
    app = TdbApp(program="", config=TdbConfig(theme=START))
    async with app.run_test() as pilot:
        await pilot.pause()
        saved.clear()
        await _open(app, pilot, tmp_path)
        await pilot.press("down", "down", "escape")
        await pilot.pause()
        assert not _pickers(app)
        assert app.theme == START
        assert saved == []
        assert app._config.theme == START
        assert not app._theme_previewing


async def test_q_reverts_like_escape(tmp_path, saved):
    app = TdbApp(program="", config=TdbConfig(theme=START))
    async with app.run_test() as pilot:
        await pilot.pause()
        saved.clear()
        await _open(app, pilot, tmp_path)
        await pilot.press("down", "q")
        await pilot.pause()
        assert not _pickers(app)
        assert app.theme == START
        assert saved == []


async def test_enter_keeps_and_saves(tmp_path, saved):
    app = TdbApp(program="", config=TdbConfig(theme=START))
    async with app.run_test() as pilot:
        await pilot.pause()
        saved.clear()
        await _open(app, pilot, tmp_path)
        await pilot.press("down")
        await pilot.pause()
        new = app.theme
        assert new != START
        await pilot.press("enter")
        await pilot.pause()
        assert not _pickers(app)
        assert app.theme == new
        assert saved and saved[-1] == new
        assert app._config.theme == new
        assert not app._theme_previewing


async def test_menu_color_theme_opens_same_picker(saved):
    app = TdbApp(program="", config=TdbConfig(theme=START))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_color_theme()
        await pilot.pause()
        assert isinstance(app.screen, ThemePickerModal)


@pytest.mark.parametrize("scheme", ["vim", "emacs", "default"])
async def test_t_in_navigation_and_debug_modes(tmp_path, saved, scheme):
    for mode in (Mode.NAVIGATION, Mode.DEBUG):
        app = TdbApp(program="", config=TdbConfig(theme=START, keybindings=scheme))
        async with app.run_test() as pilot:
            await pilot.pause()
            cv = app.query_one("#code-view", CodeView)
            cv.keybindings = KeybindingConfig.from_scheme(scheme)
            cv.load_file(_write(tmp_path))
            cv.mode = mode
            cv.focus()
            await pilot.pause()
            await pilot.press("T")
            await pilot.pause()
            assert isinstance(app.screen, ThemePickerModal), (scheme, mode)


async def test_lowercase_t_is_not_picker(tmp_path, saved):
    app = TdbApp(program="", config=TdbConfig(theme=START))
    async with app.run_test() as pilot:
        await pilot.pause()
        cv = app.query_one("#code-view", CodeView)
        cv.load_file(_write(tmp_path))
        cv.focus()
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        assert not _pickers(app)


async def test_t_while_picker_open_does_not_stack(tmp_path, saved):
    app = TdbApp(program="", config=TdbConfig(theme=START))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _open(app, pilot, tmp_path)
        await pilot.press("down")
        await pilot.pause()
        app.action_color_theme()
        await pilot.pause()
        assert len(_pickers(app)) == 1
        await pilot.press("escape")
        await pilot.pause()
        assert app.theme == START


async def test_non_preview_theme_change_still_persists(saved):
    app = TdbApp(program="", config=TdbConfig(theme=START))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.theme = "nord"
        await pilot.pause()
        assert saved and saved[-1] == "nord"


async def test_quit_while_previewing_keeps_saved_theme(tmp_path, saved):
    app = TdbApp(program="", config=TdbConfig(theme=START))
    async with app.run_test() as pilot:
        await pilot.pause()
        saved.clear()
        await _open(app, pilot, tmp_path)
        await pilot.press("down", "down")
        await pilot.pause()
        await pilot.press("ctrl+q")
        await pilot.pause()
    assert all(t == START for t in saved)
    assert app._config.theme == START


async def test_picker_lists_available_themes_sorted(saved):
    app = TdbApp(program="", config=TdbConfig(theme=START))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_color_theme()
        await pilot.pause()
        ol = app.screen.query_one(OptionList)
        ids = [ol.get_option_at_index(i).id for i in range(ol.option_count)]
        assert ids == sorted(app.available_themes)


async def test_picker_does_not_dim_screen(saved):
    app = TdbApp(program="", config=TdbConfig(theme=START))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_color_theme()
        await pilot.pause()
        assert app.screen.styles.background.a == 0


async def test_enter_saves_theme_on_screen_after_palette_change(tmp_path, saved):
    # Ctrl+P's palette can change the theme on top of the open picker;
    # Enter must then save what the user is looking at.
    app = TdbApp(program="", config=TdbConfig(theme=START))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _open(app, pilot, tmp_path)
        app.theme = "nord"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.theme == "nord"
        assert app._config.theme == "nord"
        assert saved and saved[-1] == "nord"

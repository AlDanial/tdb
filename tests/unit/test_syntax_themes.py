"""Code View syntax colors and line highlights follow the Textual theme."""

from __future__ import annotations

from pathlib import Path

import pytest
from pygments.styles import get_all_styles
from rich.style import Style
from textual.theme import BUILTIN_THEMES, Theme

from tdb.app import TdbApp
from tdb.persist import TdbConfig
from tdb.session.controller import DebugController
from tdb.widgets.code_view import CodeView
from tdb.widgets.syntax_themes import line_backgrounds, pygments_style_for


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    async def no_op(self, *args, **kwargs):
        pass

    monkeypatch.setattr(DebugController, "start", no_op)
    monkeypatch.setattr(DebugController, "stop", no_op)
    monkeypatch.setattr("tdb.app.save_config", lambda cfg: None)


def _rgb(s: str) -> tuple[int, int, int]:
    assert s.startswith("rgb(") and s.endswith(")"), s
    r, g, b = (int(v) for v in s[4:-1].split(","))
    return r, g, b


def test_exact_matches():
    assert pygments_style_for(BUILTIN_THEMES["solarized-light"]) == "solarized-light"
    assert pygments_style_for(BUILTIN_THEMES["nord"]) == "nord"
    assert pygments_style_for(BUILTIN_THEMES["dracula"]) == "dracula"
    assert pygments_style_for(BUILTIN_THEMES["gruvbox"]) == "gruvbox-dark"
    assert pygments_style_for(BUILTIN_THEMES["textual-dark"]) == "monokai"


def test_every_builtin_maps_to_installed_style():
    styles = set(get_all_styles())
    for t in BUILTIN_THEMES.values():
        assert pygments_style_for(t) in styles, t.name


def test_unknown_theme_falls_back_by_darkness():
    assert (
        pygments_style_for(Theme("x-dark", primary="#888888", dark=True)) == "monokai"
    )
    assert (
        pygments_style_for(Theme("x-light", primary="#888888", dark=False)) == "default"
    )


def test_light_theme_line_bgs_are_light():
    for s in line_backgrounds(BUILTIN_THEMES["textual-light"]):
        assert sum(_rgb(s)) / 3 > 150, s


def test_dark_theme_line_bgs_are_dark():
    for s in line_backgrounds(BUILTIN_THEMES["textual-dark"]):
        assert sum(_rgb(s)) / 3 < 120, s


def test_current_and_cursor_bgs_differ():
    for t in BUILTIN_THEMES.values():
        cur, cursor = line_backgrounds(t)
        assert cur != cursor, t.name


def _write(tmp_path: Path) -> str:
    p = tmp_path / "prog.py"
    p.write_text("def f(x):\n    return x + 1\n", encoding="utf-8")
    return str(p)


def _colors(lines) -> list:
    return [
        span.style.color
        for line in lines
        for span in line._spans
        if isinstance(span.style, Style) and span.style.color is not None
    ]


async def test_codeview_rehighlights_on_theme_change(tmp_path):
    app = TdbApp(program="", config=TdbConfig(theme="textual-dark"))
    async with app.run_test() as pilot:
        await pilot.pause()
        cv = app.query_one("#code-view", CodeView)
        cv.load_file(_write(tmp_path))
        await pilot.pause()
        assert cv._syntax_style == "monokai"
        before = _colors(cv._highlighted)
        bg_before = cv._cursor_bg
        app.theme = "solarized-light"
        await pilot.pause()
        assert cv._syntax_style == "solarized-light"
        assert _colors(cv._highlighted) != before
        assert cv._cursor_bg != bg_before


async def test_startup_theme_is_used_for_first_highlight(tmp_path):
    app = TdbApp(program="", config=TdbConfig(theme="nord"))
    async with app.run_test() as pilot:
        await pilot.pause()
        cv = app.query_one("#code-view", CodeView)
        cv.load_file(_write(tmp_path))
        await pilot.pause()
        assert cv._syntax_style == "nord"


async def test_placeholder_not_rehighlighted_on_theme_change(tmp_path):
    app = TdbApp(program="", config=TdbConfig(theme="textual-dark"))
    async with app.run_test() as pilot:
        await pilot.pause()
        cv = app.query_one("#code-view", CodeView)
        cv.load_placeholder("binary file: not shown", str(tmp_path / "a.out"))
        await pilot.pause()
        app.theme = "solarized-light"
        await pilot.pause()
        assert cv._highlighted
        for line in cv._highlighted:
            assert line.style.italic and line.style.dim

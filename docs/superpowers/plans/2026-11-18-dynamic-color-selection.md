# Dynamic Color-Theme Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Press `T` in the Code View to open a right-docked theme list that live-previews each highlighted theme. Enter keeps the theme, Esc reverts it. Code syntax colors and the line highlights follow the theme.

**Architecture:** A new `tdb/widgets/syntax_themes.py` maps a Textual theme to a Pygments style and to line-highlight colors. CodeView subscribes to `app.theme_changed_signal` and re-highlights. A new `ThemePickerModal` (a transparent `ModalScreen[str | None]` with an `OptionList`) sets `app.theme` on each highlight. TdbApp suppresses `watch_theme` persistence while a preview is open and saves only on Enter.

**Tech Stack:** Python, Textual 8.2.8 (`Theme`, `theme_changed_signal`, `OptionList`, `ModalScreen`), Rich `Syntax` with Pygments styles, pytest with Textual pilot.

**Spec:** `docs/superpowers/specs/2026-11-18-dynamic-color-selection-design.md`

## Global Constraints

- Only Textual's registered themes (`app.available_themes`) are listed, in alphabetical order.
- Config is written only on Enter. While previewing, `TdbConfig.theme` stays the original value.
- Every dismissal other than Enter restores the theme that was active when the list opened.
- `T` opens the list in Debug and Navigation modes for the vim, emacs and default schemes. Lowercase `t` stays run-to-cursor.
- Configure → Color Theme opens the same list instead of Textual's palette.
- `tdb --vi` (`vi_app.py`) is unchanged.
- Run tests with `uv run pytest`. Use the memcap-pytest wrapper for full-suite runs (see the OOM incident memory).

## Review Focus

1. **Opening the list while a theme preview is already active (`T` pressed twice, or the menu used while the list is open).** Expected: only one list is open, and the "original" theme is still the pre-preview one. Test: `test_t_while_picker_open_does_not_stack` (Task 2).
2. **Theme changes from Textual's Ctrl+P command palette** (TdbApp keeps the palette enabled). Expected: these are still saved as before. Test: `test_non_preview_theme_change_still_persists` (Task 2).
3. **An unmapped or user-registered theme.** Expected: the Pygments fallback is chosen by the `dark` flag, with no crash. Test: `test_unknown_theme_falls_back_by_darkness` (Task 1).
4. **The app quitting with the list open (Ctrl+Q).** Expected: the config on disk keeps the original theme. Test: `test_quit_while_previewing_keeps_saved_theme` (Task 2).
5. **A theme change while a placeholder (non-code) source is shown.** Expected: the dim italic placeholder stays plain and isn't run through the lexer. Test: `test_placeholder_not_rehighlighted_on_theme_change` (Task 1).

---

### Task 1: Syntax colors and line highlights follow the theme

**Files:**
- Create: `src/tdb/widgets/syntax_themes.py`
- Modify: `src/tdb/widgets/code_view.py` (`_highlight_source` at ~1261, the line-number and line-bg styles at ~1318-1334, and `on_mount` / the load path where `_highlighted` is built at ~1212)
- Test: `tests/unit/test_syntax_themes.py`

**Interfaces:**
- Produces:
  - `pygments_style_for(theme: textual.theme.Theme) -> str`
  - `line_backgrounds(theme: textual.theme.Theme) -> tuple[str, str]`, returning `(current_line_bg, cursor_line_bg)` as `"rgb(r,g,b)"` strings
  - `CodeView.refresh_theme() -> None`, which recomputes the Pygments style and the line backgrounds, re-runs `_highlight_source` (skipped for placeholder sources), and re-renders

- [ ] **Step 1: Write the failing tests**

```python
from textual.theme import BUILTIN_THEMES, Theme
from tdb.widgets.syntax_themes import pygments_style_for, line_backgrounds

def test_exact_matches():
    assert pygments_style_for(BUILTIN_THEMES["solarized-light"]) == "solarized-light"
    assert pygments_style_for(BUILTIN_THEMES["nord"]) == "nord"
    assert pygments_style_for(BUILTIN_THEMES["dracula"]) == "dracula"
    assert pygments_style_for(BUILTIN_THEMES["gruvbox"]) == "gruvbox-dark"
    assert pygments_style_for(BUILTIN_THEMES["textual-dark"]) == "monokai"

def test_every_builtin_maps_to_installed_style():
    from pygments.styles import get_all_styles
    styles = set(get_all_styles())
    for t in BUILTIN_THEMES.values():
        assert pygments_style_for(t) in styles

def test_unknown_theme_falls_back_by_darkness():
    assert pygments_style_for(Theme("x-dark", primary="#888888", dark=True)) == "monokai"
    assert pygments_style_for(Theme("x-light", primary="#888888", dark=False)) == "default"

def test_light_theme_line_bgs_are_light():
    cur, cursor = line_backgrounds(BUILTIN_THEMES["textual-light"])
    # parse "rgb(r,g,b)"; luminance well above the old dark values
    for s in (cur, cursor):
        r, g, b = map(int, s[4:-1].split(","))
        assert (r + g + b) / 3 > 150

async def test_codeview_rehighlights_on_theme_change(tmp_path):
    # TdbApp(program="", config=TdbConfig()) as in test_app_edit_mode.py,
    # with DebugController.start/stop patched to no-ops
    # load a .py file; record cv._syntax_style; set app.theme = "solarized-light";
    # await pilot.pause(); assert cv._syntax_style == "solarized-light"
    # and a highlighted keyword span color differs from before

async def test_placeholder_not_rehighlighted_on_theme_change(...):
    # load a placeholder source (CodeView's placeholder path, is_placeholder=True);
    # switch theme; every span in cv._highlighted is still italic+dim
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `uv run pytest tests/unit/test_syntax_themes.py -v`
Expected: FAIL with ImportError for `tdb.widgets.syntax_themes`.

- [ ] **Step 3: Implement `syntax_themes.py`**

Write a `_PYGMENTS_FOR_THEME` dict with the entries `textual-dark→monokai`, `textual-light→default`, `nord→nord`, `gruvbox→gruvbox-dark`, `dracula→dracula`, `tokyo-night→night-owl`, `monokai→monokai`, `solarized-light→solarized-light`, `solarized-dark→solarized-dark`, `atom-one-dark→one-dark` and `catppuccin-latte→friendly`. Other themes fall back on `theme.dark`: `monokai` for dark, `default` for light.

For `line_backgrounds`, blend onto `Color.parse(theme.background or ("#1e1e1e" if theme.dark else "#ffffff"))`:
- current line: `theme.warning or "#ffa62b"` at a factor of 0.35
- cursor line: `theme.primary` at 0.20

Return `.rgb` formatted as `rgb(r,g,b)`.

- [ ] **Step 4: Wire up CodeView**

In `_highlight_source`, use `self._syntax_style` instead of `"monokai"`. Replace the four hardcoded `rgb(120,100,30)` / `rgb(60,60,80)` uses with `self._current_bg` / `self._cursor_bg`. Line-number text stays `bright_white` on dark themes and becomes `black` on light ones.

Initialize `_syntax_style`, `_current_bg` and `_cursor_bg` from `self.app.current_theme` in `on_mount`. In the same place, subscribe with `self.app.theme_changed_signal.subscribe(self, lambda _t: self.refresh_theme())`.

`refresh_theme` re-highlights only if `self._source_text` is loaded and `not self._source_is_placeholder`, then calls `self._render_code()`.

- [ ] **Step 5: Run the tests and confirm they pass**

Run: `uv run pytest tests/unit/test_syntax_themes.py tests/unit/test_app_edit_mode.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

`git commit -m "feat: code view syntax and line highlights follow the color theme"`

---

### Task 2: Live-preview theme picker (`T` and Configure → Color Theme)

**Files:**
- Create: `src/tdb/widgets/theme_picker.py`
- Modify: `src/tdb/keybindings/__init__.py` (add `"T": "theme_picker"` to `_DEBUG`, `_VIM_NAV`, `_EMACS_NAV` and `_DEFAULT_NAV`, and `"theme_picker": "Color theme (live preview)"` to `ACTION_LABELS`)
- Modify: `src/tdb/widgets/code_view.py` (`_dispatch_action` branch, new `ThemePickerRequested` message)
- Modify: `src/tdb/app.py` (`action_color_theme` at ~1771, `watch_theme` at ~1776, a new handler `on_code_view_theme_picker_requested`)
- Test: `tests/unit/test_theme_picker.py`

**Interfaces:**
- Consumes: Task 1's live re-highlighting. No direct calls are needed, because the signal drives it.
- Produces:
  - `ThemePickerModal(original: str)`, a `ModalScreen[str | None]`. Highlighting sets `self.app.theme`. Enter dismisses with the highlighted name. Escape or `q` restores `original` and then dismisses with `None`.
  - `CodeView.ThemePickerRequested(Message)` with no fields
  - `TdbApp._theme_previewing: bool`

- [ ] **Step 1: Write the failing tests**

Use the TdbApp fixture pattern from `tests/unit/test_app_edit_mode.py`: patch `DebugController.start/stop` to no-ops, and monkeypatch `tdb.app.save_config` with a recorder list.

```python
async def test_t_opens_picker_and_down_previews():
    # cv.focus(); press "T"; isinstance(app.screen, ThemePickerModal)
    # highlighted option == starting theme; press "down"; app.theme != start; saved == []

async def test_escape_reverts_and_does_not_save():
    # T, down, down, escape -> app.theme == start, saved == [],
    # app._config.theme unchanged, not app._theme_previewing

async def test_enter_keeps_and_saves():
    # T, down, enter -> app.theme == new; saved[-1].theme == new

async def test_menu_color_theme_opens_same_picker():
    # app.action_color_theme(); isinstance(app.screen, ThemePickerModal)

async def test_t_in_navigation_mode_all_schemes():
    # for scheme in ("vim", "emacs", "default"): Navigation mode, T opens the picker

async def test_lowercase_t_is_not_picker():
    # Debug mode press "t" -> app.screen is not ThemePickerModal

async def test_t_while_picker_open_does_not_stack():
    # T, down, then app.action_color_theme(); count of ThemePickerModal in
    # app.screen_stack == 1; escape -> theme == original start

async def test_non_preview_theme_change_still_persists():
    # app.theme = "nord" with no picker open -> saved[-1].theme == "nord"

async def test_quit_while_previewing_keeps_saved_theme():
    # T, down, then app.exit(); every recorded save has .theme == start
    # (or no saves at all); app._config.theme == start

async def test_picker_lists_available_themes_sorted():
    # option ids == sorted(app.available_themes)

async def test_picker_does_not_dim_screen():
    # ThemePickerModal styles.background alpha == 0
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `uv run pytest tests/unit/test_theme_picker.py -v`
Expected: FAIL with ImportError for `tdb.widgets.theme_picker`.

- [ ] **Step 3: Implement `ThemePickerModal` in `theme_picker.py`**

CSS: `ThemePickerModal { align: right top; background: transparent; }`, with an inner container of `width: 28; height: 100%; border: solid $primary; background: $surface;`.

Compose a title Static reading `Color theme`, an `OptionList` with one `Option(name, id=name)` for each `sorted(self.app.available_themes)`, and a dim hint line reading `↑↓ preview  Enter keep  Esc revert`.

In `on_mount`, set `highlighted` to the index of `original` and focus the list. `on_option_list_option_highlighted` sets `self.app.theme = event.option.id`. `on_option_list_option_selected` calls `dismiss(event.option.id)`. The Escape and `q` bindings restore the original theme and then call `dismiss(None)`.

- [ ] **Step 4: Wire the picker into the key table, CodeView and the app**

1. In the key table, add the `T` entries and the label from the Files list.
2. In CodeView, add a `_dispatch_action` branch: `"theme_picker"` posts `ThemePickerRequested()`.
3. In TdbApp, `action_color_theme` returns immediately if any screen in `self.screen_stack` is a `ThemePickerModal`. Otherwise it sets `self._theme_previewing = True` and pushes `ThemePickerModal(self.theme)` with a callback.
4. The callback clears `_theme_previewing`. If the result is not `None`, it sets `self._config.theme = result` and calls `save_config(self._config)`.
5. `on_code_view_theme_picker_requested` calls `action_color_theme()`.
6. `watch_theme` returns before saving when `self._theme_previewing` is set. Initialize the flag to `False` in `__init__`, before the startup theme restore at ~379.

- [ ] **Step 5: Run the tests and confirm they pass**

Run: `uv run pytest tests/unit/test_theme_picker.py tests/unit/test_keybindings_modal.py tests/unit/test_syntax_themes.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

`git commit -m "feat: T opens a live-preview color theme picker"`

---

### Task 3: Docs and full suite

**Files:**
- Modify: `docs/using/keybindings.md` (the line-34 area plus the Debug and Navigation key tables)

- [ ] **Step 1: Update the docs**

Document `T` as "Color theme picker (↑↓ preview, Enter keep, Esc revert)" in the Debug and Navigation tables. State that the syntax colors follow the theme.

- [ ] **Step 2: Check the built-in docs**

Run: `uv run pytest tests/unit/test_documentation.py -v`
Expected: PASS (`--doc` stitches these pages together).

- [ ] **Step 3: Run the full suite**

Run: `uv run pytest` through the memcap-pytest wrapper.
Expected: all tests pass, apart from known pre-existing flakes. Verify any flake against a `main` worktree before calling it pre-existing.

- [ ] **Step 4: Commit**

`git commit -m "docs: T theme picker and theme-following syntax colors"`

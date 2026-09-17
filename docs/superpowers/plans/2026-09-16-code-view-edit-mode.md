# Code View Edit Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a third Code View mode, Edit, that lets the user change and save the file being viewed, with Notepad-style, emacs, and vim-lite key layers matching the existing keybinding schemes.

**Architecture:** `CodeView` keeps its line-API renderer and, in Edit mode, hides it and mounts a `CodeEditor` (a `TextArea` subclass) that owns the text buffer, undo history, and the per-scheme key layer. `CodeEditor` talks upward only through messages (`LeaveRequested`, `SaveRequested`, `SubmodeChanged`, search requests); `CodeView` owns mode cycling, save, the unsaved-changes modal, and posts `FileSaved` so `TdbApp` can remap breakpoints and notify. Pure helpers (atomic write, line remap, `$EDITOR` resolution) live in a new `tdb.source_edit` module with no Textual dependency.

**Tech Stack:** Python 3.11+, Textual 8.x (`TextArea`, `ModalScreen`, pilot tests via `App.run_test`), `difflib`, `pytest` + `pytest-asyncio` (auto mode).

**Spec:** `docs/superpowers/specs/2026-09-16-code-view-edit-mode-design.md`

## Global Constraints

- Textual floor stays `textual>=8.0.0`; the editor must work without tree-sitter. Highlighting inside the editor only when `textual._tree_sitter.TREE_SITTER` is true, and only for lexers `python`, `bash`, `go`, `rust`.
- New optional extra is named exactly `edit` and contains exactly `textual[syntax]>=8.0.0`.
- The config value for the Notepad scheme stays the string `"default"`. Only labels change, to `Notepad-style`.
- Esc cycle is Debug → Navigate → Edit → Debug. On vim, Esc in insert mode goes to normal mode and does not leave Edit mode.
- Every exit from Edit mode goes through `CodeView.leave_edit_mode()`. Every quit and restart path goes through `TdbApp._confirm_discard_edits()`.
- Save is atomic (`os.replace` of a temp file in the same directory) and `OSError` never propagates past `CodeView.save_file()`.
- Edit mode is refused for in-memory (remote) sources, replay sessions, post-mortem snapshots, and when no file is loaded.
- All work happens on branch `code-view-edit-mode` (already exists with the spec committed). Run tests with `uv run pytest`. Commit after every task with the attribution trailer from the session reminder.
- Windows: no POSIX-only calls in `source_edit.py` other than `shlex.split` on the non-Windows branch.

## Reading the code blocks

Code blocks that show only methods (no enclosing `class` line) are printed at column 0. They belong inside the class named in the surrounding prose (`CodeView`, `VimLayer`, `TdbApp`, ...); indent them one level when pasting. Blocks that start with `class ...` or module-level `def ...` are complete as shown.

## Key Textual facts the implementer must know

- Key events go to the focused widget's `_on_key` first, then bubble to ancestors' `_on_key`, and only *after* all handlers run (and only if nobody called `event.stop()`) does the App walk `BINDINGS` from the focused widget outward. So a subclass `_on_key` that stops an event pre-empts `TextArea.BINDINGS` and the App's own bindings.
- `TextArea._on_key` with `tab_behavior="indent"` turns Esc into `screen.focus_next()`. `CodeEditor._on_key` must handle Esc before calling `super()._on_key`.
- The App binds `ctrl+s` (focus Stack), `ctrl+q` (quit), `q` (confirm quit), `escape`. In Edit mode `CodeEditor` stops `ctrl+s` and `escape`; `q` is a printable key that `TextArea` inserts (and stops) in insert mode and that the vim layer swallows in normal mode.
- `CodeView._on_key` currently stops any key found in `KeybindingConfig.shared` (`up`, `down`, `home`, `end`, `q`, ...). In Edit mode it must return immediately without stopping, or arrow keys never reach `TextArea`'s bindings.
- `App.push_screen(modal, wait_for_dismiss=True)` may only be awaited inside a worker. `_restart_session` is already `@work`; `_detach_and_exit` / `_terminate_and_exit` are run via `run_worker`. Ctrl+Q's `action_quit_debugger` is not a worker and must delegate to one.
- `pilot.press("$")` and `pilot.press("dollar_sign")` are equivalent; uppercase letters are their own key names (`"G"`).

## File structure

| File | Responsibility |
|------|----------------|
| `src/tdb/keybindings/__init__.py` (modify) | `Mode.EDIT`, scheme labels, edit-mode help tables for the Keybindings dialog |
| `src/tdb/source_edit.py` (create) | Pure helpers: `atomic_write_text`, `remap_line_numbers`, `remap_breakpoints`, `resolve_external_editor` |
| `src/tdb/widgets/code_editor.py` (create) | `CodeEditor(TextArea)`, `EmacsLayer`, `VimLayer`, `_UnsavedChangesModal`, `editor_language_for` |
| `src/tdb/widgets/code_view.py` (modify) | Enter/leave Edit mode, Esc cycle, save, dirty state, deferred source loads, `FileSaved` |
| `src/tdb/session/controller.py` (modify) | `replace_breakpoints()` |
| `src/tdb/app.py` (modify) | Title, `FileSaved` handler, unsaved-edits guard on quit/restart, Edit menu, `$EDITOR` |
| `src/tdb/widgets/modals.py` (modify) | Keybindings dialog: `Notepad-style` label, Edit Mode section |
| `src/tdb/cli.py` (modify) | `--keybindings` help text |
| `pyproject.toml`, `README.md` (modify) | `edit` extra, docs |
| `tests/unit/test_keybindings.py` (modify), `tests/unit/test_source_edit.py`, `tests/unit/test_code_editor.py`, `tests/unit/test_code_editor_emacs.py`, `tests/unit/test_code_editor_vim.py`, `tests/unit/test_code_view_edit_mode.py`, `tests/unit/test_app_edit_mode.py` (create) | Tests |

---

### Task 1: `Mode.EDIT`, scheme labels, and edit-mode help tables

**Files:**
- Modify: `src/tdb/keybindings/__init__.py`
- Test: `tests/unit/test_keybindings.py`

**Interfaces:**
- Produces: `Mode.EDIT`; `SCHEME_LABELS: dict[str, str]`; `scheme_label(scheme: str) -> str`; `edit_help(scheme: str) -> list[tuple[str, str]]`; `KeybindingConfig.lookup(Mode.EDIT, key)` always returns `None`; `KeybindingConfig.format_bindings(Mode.EDIT)` returns `edit_help(self.scheme)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_keybindings.py`:

```python
def test_edit_mode_exists_and_lookup_is_inert():
    cfg = KeybindingConfig.from_scheme("vim")
    assert Mode.EDIT.value == "Edit"
    # Edit mode never routes keys through the nav/debug/shared tables:
    # the editor widget owns every key while it has focus.
    assert cfg.lookup(Mode.EDIT, "up") is None
    assert cfg.lookup(Mode.EDIT, "q") is None
    assert cfg.lookup(Mode.EDIT, "j") is None


def test_scheme_labels():
    from tdb.keybindings import scheme_label

    assert scheme_label("vim") == "Vim"
    assert scheme_label("emacs") == "Emacs"
    assert scheme_label("default") == "Notepad-style"
    assert scheme_label("bogus") == "bogus"


def test_format_bindings_edit_mode_per_scheme():
    notepad = KeybindingConfig.from_scheme("default").format_bindings(Mode.EDIT)
    emacs = KeybindingConfig.from_scheme("emacs").format_bindings(Mode.EDIT)
    vim = KeybindingConfig.from_scheme("vim").format_bindings(Mode.EDIT)
    keys = lambda rows: [k for k, _ in rows]  # noqa: E731
    assert "Ctrl+S" in keys(notepad) and "Ctrl+X Ctrl+S" not in keys(notepad)
    assert "Ctrl+X Ctrl+S" in keys(emacs)
    assert ":w" in keys(vim) and "i a I A o O" in keys(vim)
    for rows in (notepad, emacs, vim):
        assert all(isinstance(k, str) and isinstance(d, str) for k, d in rows)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_keybindings.py -v -p no:cacheprovider --no-cov`
Expected: FAIL with `AttributeError: EDIT` and `ImportError: cannot import name 'scheme_label'`.

- [ ] **Step 3: Implement**

In `src/tdb/keybindings/__init__.py`:

Update the module docstring's mode list:

```python
"""Keybinding configuration for tdb.

Three modes:
  - NAVIGATION: vim-style movement with optional count prefix (e.g. 5j, 12G)
  - DEBUG: single-key debug commands (n, s, o, c, b, p, t)
  - EDIT: the file is open in an editor widget; keys are owned by the
    editor (see tdb.widgets.code_editor), not by these tables.

ESC cycles Debug -> Navigation -> Edit -> Debug (when CodeView has focus).
"""
```

Extend `Mode`:

```python
class Mode(Enum):
    NAVIGATION = "Navigation"
    DEBUG = "Debug"
    EDIT = "Edit"
```

Add after `_SHARED`:

```python
# User-facing names for the three schemes. The config value stays the
# key ("default" is what breakpoints.json / config.json persist); only
# the label says what "default" means.
SCHEME_LABELS = {"vim": "Vim", "emacs": "Emacs", "default": "Notepad-style"}


def scheme_label(scheme: str) -> str:
    return SCHEME_LABELS.get(scheme, scheme)


# Edit-mode reference tables, (key display, description). These are
# documentation for the Keybindings dialog; the live bindings are
# implemented in tdb.widgets.code_editor and must be kept in step.
_NOTEPAD_EDIT_HELP: list[tuple[str, str]] = [
    ("Arrows", "Move cursor"),
    ("Home / End", "Start / end of line"),
    ("PgUp / PgDn", "Page up / down"),
    ("Shift+Arrows", "Select text"),
    ("Delete / Backspace", "Delete right / left"),
    ("Ctrl+Z / Ctrl+Y", "Undo / redo"),
    ("Ctrl+X / Ctrl+C / Ctrl+V", "Cut / copy / paste"),
    ("Ctrl+S", "Save file"),
    ("Esc", "Leave Edit mode"),
]

_EMACS_EDIT_HELP: list[tuple[str, str]] = [
    ("Ctrl+N / Ctrl+P", "Down / up"),
    ("Ctrl+F / Ctrl+B", "Right / left"),
    ("Alt+F / Alt+B", "Word right / left"),
    ("Ctrl+A / Ctrl+E", "Start / end of line"),
    ("Alt+< / Alt+>", "Start / end of file"),
    ("Ctrl+D", "Delete right"),
    ("Ctrl+K", "Kill to end of line"),
    ("Ctrl+Y", "Yank last kill"),
    ("Ctrl+_", "Undo"),
    ("Ctrl+S / Ctrl+R", "Search forward / backward"),
    ("Ctrl+X Ctrl+S", "Save file"),
    ("Ctrl+X Ctrl+C", "Leave Edit mode"),
    ("Ctrl+G", "Cancel pending Ctrl+X"),
    ("Esc", "Leave Edit mode"),
]

_VIM_EDIT_HELP: list[tuple[str, str]] = [
    ("h j k l", "Move (count prefix allowed)"),
    ("w b e", "Word motions"),
    ("0 ^ $", "Line start / first non-blank / line end"),
    ("gg / G / NG", "File start / end / line N"),
    ("Ctrl+F / Ctrl+B", "Page down / up"),
    ("x X", "Delete char under / before cursor"),
    ("dd dw D", "Delete line / word / to end of line"),
    ("yy", "Yank line"),
    ("p P", "Put after / before"),
    ("u / Ctrl+R", "Undo / redo"),
    ("J", "Join lines"),
    ("i a I A o O", "Enter insert mode"),
    ("/ ? n N", "Search forward / backward, next / previous"),
    (":w", "Save file"),
    (":q :wq :q!", "Leave Edit mode (save / discard)"),
    (":N", "Go to line N"),
    ("Esc", "Insert -> normal; normal -> leave Edit mode"),
]


def edit_help(scheme: str) -> list[tuple[str, str]]:
    if scheme == "emacs":
        return list(_EMACS_EDIT_HELP)
    if scheme == "default":
        return list(_NOTEPAD_EDIT_HELP)
    return list(_VIM_EDIT_HELP)
```

Change `lookup` so `Mode.EDIT` is inert:

```python
    def lookup(self, mode: Mode, key: str) -> str | None:
        """Return the action name for a key in the given mode, or None.

        EDIT always returns None: while the editor widget has focus it
        owns every key, and the shared table (arrows, q, ...) must not
        intercept them.
        """
        if mode == Mode.EDIT:
            return None
        if mode == Mode.NAVIGATION:
            action = self.navigation.get(key)
            if action:
                return action
        elif mode == Mode.DEBUG:
            action = self.debug.get(key)
            if action:
                return action
        return self.shared.get(key)
```

At the top of `format_bindings`, before `ACTION_LABELS`:

```python
        if mode == Mode.EDIT:
            return edit_help(self.scheme)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_keybindings.py -v -p no:cacheprovider --no-cov`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/keybindings/__init__.py tests/unit/test_keybindings.py
git commit -m "Add Mode.EDIT, scheme labels, and edit-mode help tables"
```

---

### Task 2: Pure helpers in `tdb.source_edit`

**Files:**
- Create: `src/tdb/source_edit.py`
- Test: `tests/unit/test_source_edit.py`

**Interfaces:**
- Produces:
  - `atomic_write_text(path: str, text: str) -> None` (raises `OSError`; temp file removed on failure)
  - `remap_line_numbers(old_lines: Sequence[str], new_lines: Sequence[str], lines: Iterable[int]) -> dict[int, int | None]` (1-based in and out; `None` = line deleted)
  - `remap_breakpoints(old_lines, new_lines, bps: list[SourceBreakpoint]) -> list[SourceBreakpoint]`
  - `resolve_external_editor(env: Mapping[str, str] | None = None, platform: str | None = None) -> list[str]`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_source_edit.py`:

```python
"""Pure helpers behind Code View Edit mode: atomic save, breakpoint
line remap across an edit, and $EDITOR resolution."""

from __future__ import annotations

import os

import pytest

from tdb.dap.types import SourceBreakpoint
from tdb.source_edit import (
    atomic_write_text,
    remap_breakpoints,
    remap_line_numbers,
    resolve_external_editor,
)


# ---- atomic_write_text ----


def test_atomic_write_creates_file_and_preserves_text(tmp_path):
    target = tmp_path / "prog.py"
    atomic_write_text(str(target), "x = 1\ny = 2\n")
    assert target.read_text(encoding="utf-8") == "x = 1\ny = 2\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["prog.py"]


def test_atomic_write_replaces_existing_file(tmp_path):
    target = tmp_path / "prog.py"
    target.write_text("old\n", encoding="utf-8")
    atomic_write_text(str(target), "new\n")
    assert target.read_text(encoding="utf-8") == "new\n"


def test_atomic_write_keeps_text_without_trailing_newline(tmp_path):
    target = tmp_path / "prog.py"
    atomic_write_text(str(target), "no newline at end")
    assert target.read_bytes() == b"no newline at end"


def test_atomic_write_raises_oserror_and_leaves_no_temp(tmp_path, monkeypatch):
    target = tmp_path / "prog.py"
    target.write_text("keep me\n", encoding="utf-8")

    def boom(src, dst):
        raise PermissionError("read-only")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_text(str(target), "new\n")
    assert target.read_text(encoding="utf-8") == "keep me\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["prog.py"]


# ---- remap_line_numbers ----


def test_remap_insert_above_shifts_down():
    old = ["a", "b", "c"]
    new = ["new", "a", "b", "c"]
    assert remap_line_numbers(old, new, [1, 3]) == {1: 2, 3: 4}


def test_remap_insert_below_leaves_lines_alone():
    old = ["a", "b", "c"]
    new = ["a", "b", "c", "new"]
    assert remap_line_numbers(old, new, [1, 3]) == {1: 1, 3: 3}


def test_remap_deleted_line_maps_to_none():
    old = ["a", "b", "c"]
    new = ["a", "c"]
    assert remap_line_numbers(old, new, [2, 3]) == {2: None, 3: 2}


def test_remap_equal_size_replace_block_maps_by_offset():
    old = ["a", "b1", "b2", "c"]
    new = ["a", "B1", "B2", "c"]
    assert remap_line_numbers(old, new, [2, 3]) == {2: 2, 3: 3}


def test_remap_unequal_replace_block_maps_to_block_start():
    old = ["a", "b1", "b2", "b3", "c"]
    new = ["a", "B", "c"]
    assert remap_line_numbers(old, new, [2, 3, 4]) == {2: 2, 3: 2, 4: 2}


def test_remap_out_of_range_line_is_none():
    assert remap_line_numbers(["a"], ["a"], [0, 5]) == {0: None, 5: None}


# ---- remap_breakpoints ----


def test_remap_breakpoints_preserves_flags_and_drops_deleted():
    old = ["a", "b", "c", "d"]
    new = ["x", "a", "c", "d"]  # inserted above, deleted 'b'
    bps = [
        SourceBreakpoint(line=1, condition="i > 1", enabled=False),
        SourceBreakpoint(line=2),
        SourceBreakpoint(line=4, hit_condition="3"),
    ]
    out = remap_breakpoints(old, new, bps)
    assert [(bp.line, bp.condition, bp.hit_condition, bp.enabled) for bp in out] == [
        (2, "i > 1", None, False),
        (4, None, "3", True),
    ]


def test_remap_breakpoints_collapses_duplicates_keeping_first():
    old = ["a", "b1", "b2", "c"]
    new = ["a", "B", "c"]
    bps = [SourceBreakpoint(line=2, condition="first"), SourceBreakpoint(line=3)]
    out = remap_breakpoints(old, new, bps)
    assert [(bp.line, bp.condition) for bp in out] == [(2, "first")]


# ---- resolve_external_editor ----


def test_resolve_editor_prefers_visual_then_editor_posix():
    assert resolve_external_editor({"VISUAL": "code -w", "EDITOR": "vim"}, "linux") == [
        "code",
        "-w",
    ]
    assert resolve_external_editor({"EDITOR": "vim"}, "linux") == ["vim"]
    assert resolve_external_editor({}, "linux") == ["vi"]


def test_resolve_editor_windows_passes_value_verbatim():
    assert resolve_external_editor({"EDITOR": "C:\\Tools\\ed.exe -n"}, "win32") == [
        "C:\\Tools\\ed.exe -n"
    ]
    assert resolve_external_editor({}, "win32") == ["notepad"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_source_edit.py -v -p no:cacheprovider --no-cov`
Expected: FAIL with `ModuleNotFoundError: No module named 'tdb.source_edit'`.

- [ ] **Step 3: Implement**

Create `src/tdb/source_edit.py`:

```python
"""Pure helpers behind Code View Edit mode.

No Textual imports here: everything is unit-testable without a TUI.

- atomic_write_text: write-to-temp + os.replace so a crash mid-save
  never leaves a half-written source file (works on Windows because
  the temp file lives in the target's own directory).
- remap_line_numbers / remap_breakpoints: after a save, breakpoints
  set on the old text need new line numbers. difflib gives us the
  equal/insert/delete/replace blocks; we map through them.
- resolve_external_editor: $VISUAL / $EDITOR lookup for the
  "Open in $EDITOR" menu item.
"""

from __future__ import annotations

import difflib
import os
import shlex
import sys
from collections.abc import Iterable, Mapping, Sequence

from tdb.dap.types import SourceBreakpoint


def atomic_write_text(path: str, text: str) -> None:
    """Write `text` (UTF-8) to `path` atomically. Raises OSError."""
    directory = os.path.dirname(path) or "."
    name = os.path.basename(path)
    tmp = os.path.join(directory, f".{name}.tdb-tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def remap_line_numbers(
    old_lines: Sequence[str],
    new_lines: Sequence[str],
    lines: Iterable[int],
) -> dict[int, int | None]:
    """Map 1-based line numbers in `old_lines` to their position in
    `new_lines`. A line that was deleted maps to None.

    Rules (from the spec): lines inside an `equal` block map by
    offset; inside a `replace` block they map by offset when the old
    and new blocks have the same size, otherwise to the first line of
    the new block; inside a `delete` block they map to None.
    """
    wanted = set(lines)
    result: dict[int, int | None] = {}
    matcher = difflib.SequenceMatcher(None, list(old_lines), list(new_lines))
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        for old_idx in range(i1, i2):
            line = old_idx + 1
            if line not in wanted:
                continue
            if tag == "equal" or (tag == "replace" and (i2 - i1) == (j2 - j1)):
                result[line] = j1 + (old_idx - i1) + 1
            elif tag == "replace":
                result[line] = j1 + 1
            else:  # delete
                result[line] = None
    for line in wanted:
        result.setdefault(line, None)
    return result


def remap_breakpoints(
    old_lines: Sequence[str],
    new_lines: Sequence[str],
    bps: list[SourceBreakpoint],
) -> list[SourceBreakpoint]:
    """Return breakpoints with lines remapped across the edit.

    Deleted lines drop their breakpoint. When two breakpoints land on
    the same new line, the first (in input order) wins so its
    condition / enabled flag survive.
    """
    mapping = remap_line_numbers(old_lines, new_lines, [bp.line for bp in bps])
    seen: set[int] = set()
    out: list[SourceBreakpoint] = []
    for bp in bps:
        new_line = mapping.get(bp.line)
        if new_line is None or new_line in seen:
            continue
        seen.add(new_line)
        out.append(
            SourceBreakpoint(
                line=new_line,
                condition=bp.condition,
                hit_condition=bp.hit_condition,
                log_message=bp.log_message,
                enabled=bp.enabled,
                persist=bp.persist,
            )
        )
    return out


def resolve_external_editor(
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> list[str]:
    """Command (argv prefix) for the user's editor: $VISUAL, then
    $EDITOR, then a platform default. POSIX values are shlex-split so
    `EDITOR="code -w"` works; Windows values are passed verbatim."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    windows = platform.startswith("win")
    for var in ("VISUAL", "EDITOR"):
        value = env.get(var, "").strip()
        if value:
            return [value] if windows else shlex.split(value)
    return ["notepad"] if windows else ["vi"]
```

Check `SourceBreakpoint` in `src/tdb/dap/types.py:56` for any fields beyond `line, condition, hit_condition, log_message, enabled, persist` (there is a `verified`-style flag after `persist`). Do **not** copy that one: a remapped breakpoint has not been verified yet, so leave it at its default.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_source_edit.py -v -p no:cacheprovider --no-cov`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/source_edit.py tests/unit/test_source_edit.py
git commit -m "Add source_edit helpers: atomic save, breakpoint remap, editor lookup"
```

---

### Task 3: `CodeEditor` widget with Notepad-style behavior and the unsaved-changes modal

**Files:**
- Create: `src/tdb/widgets/code_editor.py`
- Test: `tests/unit/test_code_editor.py`

**Interfaces:**
- Produces:
  - `editor_language_for(lexer: str | None) -> str | None`
  - `class CodeEditor(TextArea)`: `__init__(text: str, *, scheme: str, lexer: str | None = None, **kwargs)`; attributes `scheme: str`; properties `is_dirty: bool`, `submode: str | None`, `command_text: str`; methods `mark_clean() -> None`, `find(term: str, backward: bool, *, from_cursor: bool = True) -> bool`, `_set_layer(layer)` hook used by Tasks 5 and 6; messages `CodeEditor.LeaveRequested(discard: bool = False)`, `CodeEditor.SaveRequested()`, `CodeEditor.SubmodeChanged()`, `CodeEditor.SearchRequested(backward: bool)`, `CodeEditor.SearchStepRequested(forward: bool)`.
  - `class _UnsavedChangesModal(ModalScreen[str])`: `__init__(filename: str)`; dismisses with `"save"`, `"discard"`, or `"cancel"`; keys `s`, `d`, `escape`.
- Consumes: nothing from earlier tasks.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_code_editor.py`:

```python
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
    async with app.run_test() as pilot:
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
    async with app.run_test() as pilot:
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_code_editor.py -v -p no:cacheprovider --no-cov`
Expected: FAIL with `ModuleNotFoundError: No module named 'tdb.widgets.code_editor'`.

- [ ] **Step 3: Implement**

Create `src/tdb/widgets/code_editor.py`:

```python
"""Editor widget behind Code View Edit mode.

`CodeEditor` is Textual's TextArea plus:

- tdb's key routing. Textual checks BINDINGS only *after* every
  `_on_key` handler up the DOM has run and none stopped the event, so
  handling a key here and calling `event.stop()` pre-empts both
  TextArea's own bindings and the App's (ctrl+s = focus Stack, etc.).
- a per-scheme key layer (EmacsLayer / VimLayer, Tasks 5-6). With
  scheme "default" there is no layer: TextArea's stock behavior *is*
  the Notepad-style editor.
- messages instead of reach-ups: the editor never touches CodeView or
  the App. It asks to leave, asks to save, reports sub-mode changes.
"""

from __future__ import annotations

from typing import Protocol

from textual.binding import Binding
from textual.containers import Vertical
from textual.events import Key
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Static, TextArea

# Lexer names (LanguageProfile.presentation.lexer) that Textual's
# bundled tree-sitter grammars can highlight.
_TEXTUAL_LANGUAGES = {"python": "python", "bash": "bash", "go": "go", "rust": "rust"}


def editor_language_for(lexer: str | None) -> str | None:
    """Tree-sitter language for TextArea, or None for plain text.

    None when the optional `textual[syntax]` extra (tdb's `[edit]`
    extra) is not installed, or when the lexer has no bundled grammar.
    """
    import textual._tree_sitter as ts

    if not ts.TREE_SITTER or lexer is None:
        return None
    return _TEXTUAL_LANGUAGES.get(lexer)


class KeyLayer(Protocol):
    """A scheme-specific key interpreter installed on a CodeEditor."""

    def handle_key(self, event: Key) -> bool:
        """Return True when the key was consumed."""
        ...

    @property
    def submode(self) -> str | None: ...

    @property
    def command_text(self) -> str: ...


class CodeEditor(TextArea):
    DEFAULT_CSS = """
    CodeEditor {
        width: 100%;
        height: 100%;
        border: none;
        padding: 0;
    }
    """

    # Footer hints only. `_on_key` stops these keys before the binding
    # system sees them, so the no-op action methods never run (same
    # trick as CodeView's footer_hint_* bindings).
    BINDINGS = [
        Binding("escape", "footer_hint_leave", "leave edit"),
        Binding("ctrl+s", "footer_hint_save", "save"),
    ]

    class LeaveRequested(Message):
        def __init__(self, discard: bool = False) -> None:
            self.discard = discard
            super().__init__()

    class SaveRequested(Message):
        pass

    class SubmodeChanged(Message):
        pass

    class SearchRequested(Message):
        def __init__(self, backward: bool) -> None:
            self.backward = backward
            super().__init__()

    class SearchStepRequested(Message):
        def __init__(self, forward: bool) -> None:
            self.forward = forward
            super().__init__()

    def __init__(
        self,
        text: str,
        *,
        scheme: str,
        lexer: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(
            text,
            language=editor_language_for(lexer),
            soft_wrap=False,
            tab_behavior="indent",
            show_line_numbers=True,
            **kwargs,
        )
        self.scheme = scheme
        self._clean_text = text
        self._layer: KeyLayer | None = None

    # ---- state ----

    @property
    def is_dirty(self) -> bool:
        return self.text != self._clean_text

    def mark_clean(self) -> None:
        self._clean_text = self.text

    @property
    def submode(self) -> str | None:
        return self._layer.submode if self._layer is not None else None

    @property
    def command_text(self) -> str:
        return self._layer.command_text if self._layer is not None else ""

    def _set_layer(self, layer: KeyLayer | None) -> None:
        self._layer = layer

    # ---- footer hint no-ops ----

    def action_footer_hint_leave(self) -> None: ...
    def action_footer_hint_save(self) -> None: ...

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "footer_hint_save":
            return True if self.scheme != "emacs" else False
        return True

    # ---- key routing ----

    async def _on_key(self, event: Key) -> None:
        if self._layer is not None and self._layer.handle_key(event):
            event.stop()
            event.prevent_default()
            return
        key = event.key
        if key == "escape":
            # Must run before TextArea._on_key: with tab_behavior="indent"
            # it turns Esc into screen.focus_next().
            event.stop()
            event.prevent_default()
            self.post_message(self.LeaveRequested())
            return
        if key == "ctrl+s" and self.scheme != "emacs":
            event.stop()
            event.prevent_default()
            self.post_message(self.SaveRequested())
            return
        if key == "insert":
            # TextArea has no overwrite mode; swallow so the key does
            # nothing surprising (spec: documented no-op).
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)

    # ---- search (used by all schemes via CodeView's search modal) ----

    def find(self, term: str, backward: bool, *, from_cursor: bool = True) -> bool:
        """Move the caret to the next/previous line containing `term`
        (case-insensitive, wrapping). Returns False when not found."""
        if not term:
            return False
        needle = term.lower()
        total = self.document.line_count
        if total == 0:
            return False
        row = (
            self.cursor_location[0]
            if from_cursor
            else (total - 1 if not backward else 0)
        )
        step = -1 if backward else 1
        for i in range(1, total + 1):
            idx = (row + step * i) % total
            if needle in self.document.get_line(idx).lower():
                self.move_cursor((idx, 0))
                return True
        return False


class _UnsavedChangesModal(ModalScreen[str]):
    """Save / Discard / Cancel prompt used by every path that would
    abandon unsaved edits (leave Edit mode, quit, restart, open)."""

    DEFAULT_CSS = """
    _UnsavedChangesModal {
        align: center middle;
    }
    _UnsavedChangesModal #dialog {
        width: 52;
        height: auto;
        border: solid $warning;
        background: $surface;
        padding: 1 2;
    }
    """

    BINDINGS = [
        Binding("s", "save", "Save", show=False),
        Binding("d", "discard", "Discard", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, filename: str) -> None:
        super().__init__()
        self._filename = filename

    def compose(self):
        with Vertical(id="dialog"):
            yield Static(
                f"[bold]{self._filename} has unsaved changes[/bold]", markup=True
            )
            yield Static("[bold]s[/bold]: save and continue", markup=True)
            yield Static("[bold]d[/bold]: discard changes and continue", markup=True)
            yield Static("[dim]ESC: cancel, keep editing[/dim]", markup=True)

    def action_save(self) -> None:
        self.dismiss("save")

    def action_discard(self) -> None:
        self.dismiss("discard")

    def action_cancel(self) -> None:
        self.dismiss("cancel")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_code_editor.py -v -p no:cacheprovider --no-cov`
Expected: all PASS. If `test_tab_inserts_indentation` fails because `TextArea.indent_width` default differs, assert on `ed.text.startswith(" " * ed.indent_width + "x")` instead.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/widgets/code_editor.py tests/unit/test_code_editor.py
git commit -m "Add CodeEditor widget (Notepad-style) and unsaved-changes modal"
```

---

### Task 4: `CodeView` Edit mode: enter, leave, Esc cycle, save, dirty title, deferred loads

**Files:**
- Modify: `src/tdb/widgets/code_view.py` (imports; `__init__`; `_on_key`; new section after `_on_key`; `_install_source`; `load_file`; `load_content`; `on_focus`)
- Test: `tests/unit/test_code_view_edit_mode.py`

**Interfaces:**
- Consumes: `Mode.EDIT` (Task 1); `atomic_write_text` (Task 2); `CodeEditor`, `_UnsavedChangesModal` (Task 3).
- Produces on `CodeView`:
  - attribute `edit_enabled: bool = True` (the App clears it for replay / post-mortem)
  - properties `is_editing: bool`, `is_dirty: bool`, `editor_submode: str | None`
  - `lines() -> list[str]` (copy of the current source lines)
  - `mode_label() -> str`
  - `edit_refusal_reason() -> str | None` (`None` means Edit mode is allowed)
  - `async enter_edit_mode() -> bool`
  - `leave_edit_mode(discard: bool = False) -> None`
  - `discard_edits() -> None`
  - `revert_to_disk() -> bool`
  - `save_file() -> bool`
  - message `CodeView.FileSaved(path: str, old_lines: list[str], new_lines: list[str])`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_code_view_edit_mode.py`:

```python
"""CodeView Edit mode: the Esc cycle, entering/leaving the editor,
save, dirty state, refusal cases, and deferred source loads."""

from __future__ import annotations

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
    async with app.run_test() as pilot:
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
        assert open(path, encoding="utf-8").read() == "x = 10\ny = 2\n"
        assert app.saved == [(path, ["x = 1", "y = 2"], ["x = 10", "y = 2"])]


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
        assert open(path, encoding="utf-8").read() == "x = 1\ny = 2\n"


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
        assert open(path, encoding="utf-8").read() == "zx = 1\ny = 2\n"


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
        open(other, "w").write("o = 1\n")
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_code_view_edit_mode.py -v -p no:cacheprovider --no-cov`
Expected: FAIL (`AttributeError` on `is_editing`, `mode_label`, etc.).

- [ ] **Step 3: Implement in `src/tdb/widgets/code_view.py`**

Imports — add:

```python
from textual.events import Click, Focus, Key  # Click used by _CodeContent
from tdb.source_edit import atomic_write_text
from tdb.widgets.code_editor import CodeEditor, _UnsavedChangesModal
```

Add a message next to `ModeChanged`:

```python
    class FileSaved(Message):
        """Posted after a successful save. Carries old and new lines so
        the App can remap breakpoints across the edit."""

        def __init__(self, path: str, old_lines: list[str], new_lines: list[str]) -> None:
            self.path = path
            self.old_lines = old_lines
            self.new_lines = new_lines
            super().__init__()
```

In `__init__`, after `self._suppress_next_click = False`:

```python
        # ---- Edit mode ----
        # Cleared by the App for replay / post-mortem sessions.
        self.edit_enabled: bool = True
        self._editor: CodeEditor | None = None
        # True when the displayed text came from a readable local file
        # (load_file success). load_content (remote source) clears it.
        self._source_is_local: bool = False
        self._had_trailing_newline: bool = True
        # A load_file/load_content that arrived while editing another
        # file. Applied when the editor closes.
        self._deferred_source: tuple[str, str] | None = None
```

Replace the start of `_on_key` (everything from `key = event.key` through the ESC block) with:

```python
        key = event.key

        if self.mode == Mode.EDIT:
            # The editor owns every key while editing. If focus somehow
            # landed on the container itself, Esc still leaves; nothing
            # else is intercepted (arrows must reach TextArea's bindings).
            if key == "escape":
                event.stop()
                event.prevent_default()
                self.leave_edit_mode()
            return

        # ESC cycles mode: Debug -> Navigation -> Edit -> Debug
        if key == "escape":
            self._count_buf = ""
            if self.mode == Mode.DEBUG:
                self.mode = Mode.NAVIGATION
            elif not await self.enter_edit_mode():
                # Edit refused (no file, remote source, replay, ...):
                # keep the cycle moving.
                self.mode = Mode.DEBUG
            self._announce_mode()
            event.stop()
            event.prevent_default()
            return
```

Add a new section after `_dispatch_action` (before the footer-hint plumbing):

```python
# ---- Edit mode ----


@property
def is_editing(self) -> bool:
    return self._editor is not None


@property
def is_dirty(self) -> bool:
    return self._editor is not None and self._editor.is_dirty


@property
def editor_submode(self) -> str | None:
    return self._editor.submode if self._editor is not None else None


def lines(self) -> list[str]:
    return list(self._lines)


def mode_label(self) -> str:
    """Text for the pane title: 'Debug', 'Navigation', 'Edit',
    'Edit*', 'Edit:INSERT*', 'Edit :wq' ..."""
    if self.mode != Mode.EDIT or self._editor is None:
        return self.mode.value
    label = "Edit"
    sub = self._editor.submode
    if sub == "insert":
        label += ":INSERT"
    elif sub == "command":
        label += f" :{self._editor.command_text}"
    if self._editor.is_dirty:
        label += "*"
    return label


def edit_refusal_reason(self) -> str | None:
    """None when Edit mode may be entered, else a user-facing reason."""
    if not self.edit_enabled:
        return "Editing is not available in this session."
    if self.source_path is None:
        return "No file is loaded."
    if not self._source_is_local:
        return "This source is not on this machine, so it cannot be edited."
    return None


def _announce_mode(self) -> None:
    self.post_message(self.ModeChanged(self.mode))
    # Footer caches per-focused-widget bindings; without this nudge
    # the c/n/s/p/t/b hints stay visible after leaving DEBUG.
    self.app.refresh_bindings()


def _editor_text(self) -> str:
    text = "\n".join(self._lines)
    return text + "\n" if self._had_trailing_newline and self._lines else text


async def enter_edit_mode(self) -> bool:
    """Mount the editor over the code pane. Returns False (with a
    notification) when editing is not allowed here."""
    if self._editor is not None:
        return True
    reason = self.edit_refusal_reason()
    if reason is not None:
        self.app.notify(reason, title="Edit", severity="warning")
        return False
    editor = CodeEditor(
        self._editor_text(),
        scheme=self.keybindings.scheme,
        lexer=self.lexer_name,
    )
    self._install_key_layer(editor)
    self._editor = editor
    if self._content is not None:
        self._content.display = False
    self.mode = Mode.EDIT
    await self.mount(editor)
    editor.move_cursor((max(0, self.cursor_line - 1), 0))
    editor.scroll_cursor_visible(center=True)
    editor.focus()
    self._announce_mode()
    return True


def _install_key_layer(self, editor: CodeEditor) -> None:
    """Attach the scheme's key layer. Filled in by Tasks 5 and 6;
    the Notepad scheme has no layer."""
    return None


def leave_edit_mode(self, discard: bool = False) -> None:
    """The one exit from Edit mode. Prompts when there are unsaved
    changes (unless `discard`), then tears the editor down."""
    if self._editor is None:
        return
    if discard or not self._editor.is_dirty:
        self._teardown_editor(reload_from_disk=discard)
        return

    def on_dismiss(result: str | None) -> None:
        if result == "save":
            if self.save_file():
                self._teardown_editor(reload_from_disk=False)
        elif result == "discard":
            self._teardown_editor(reload_from_disk=True)
        # cancel: stay in Edit mode
        if self._editor is not None:
            self._editor.focus()

    name = Path(self.source_path).name if self.source_path else "buffer"
    self.app.push_screen(_UnsavedChangesModal(name), callback=on_dismiss)


def discard_edits(self) -> None:
    """Drop unsaved edits and leave Edit mode (no prompt)."""
    self.leave_edit_mode(discard=True)


def revert_to_disk(self) -> bool:
    """Replace the editor buffer with the on-disk text, staying in
    Edit mode. Undoable with the editor's undo. False if not editing
    or the file cannot be read."""
    if self._editor is None or self.source_path is None:
        return False
    try:
        text = Path(self.source_path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        self.app.notify(
            f"Cannot read {self.source_path}: {exc}", title="Edit", severity="error"
        )
        return False
    self._editor.replace(text, (0, 0), self._editor.document.end)
    self._editor.mark_clean()
    self._editor.move_cursor((0, 0))
    self._announce_mode()
    return True


def save_file(self) -> bool:
    """Atomically write the editor buffer to source_path. On OSError
    notify and keep the buffer dirty. Posts FileSaved on success."""
    if self._editor is None or self.source_path is None:
        return False
    text = self._editor.text
    try:
        atomic_write_text(self.source_path, text)
    except OSError as exc:
        self.app.notify(f"Save failed: {exc}", title="Edit", severity="error")
        return False
    old_lines = list(self._lines)
    self._editor.mark_clean()
    # Keep the viewer's model in step so a later teardown without
    # further edits is a no-op, and FileSaved's new_lines are right.
    self._had_trailing_newline = text.endswith("\n")
    self._lines = text.splitlines()
    self.post_message(self.FileSaved(self.source_path, old_lines, list(self._lines)))
    self._announce_mode()
    return True


def _teardown_editor(self, *, reload_from_disk: bool) -> None:
    editor = self._editor
    if editor is None:
        return
    text = editor.text
    row = editor.cursor_location[0]
    self._editor = None
    editor.remove()
    if self._content is not None:
        self._content.display = True
    self.mode = Mode.DEBUG
    deferred = self._deferred_source
    self._deferred_source = None
    if deferred is not None:
        self._install_source(*deferred)
    elif reload_from_disk and self.source_path is not None:
        self.load_file(self.source_path)
    elif self.source_path is not None and text != self._editor_text():
        # Saved text (save_file already updated _lines) or an edit
        # the user chose to keep: re-highlight and recompute step units.
        self._install_source(text, self.source_path)
    if deferred is None:
        self.cursor_line = max(1, min(row + 1, len(self._lines) or 1))
    self._announce_mode()
    self.focus()


# Editor -> view messages


def on_code_editor_leave_requested(self, message: CodeEditor.LeaveRequested) -> None:
    message.stop()
    self.leave_edit_mode(discard=message.discard)


def on_code_editor_save_requested(self, message: CodeEditor.SaveRequested) -> None:
    message.stop()
    self.save_file()


def on_code_editor_submode_changed(self, message: CodeEditor.SubmodeChanged) -> None:
    message.stop()
    self._announce_mode()


def on_text_area_changed(self, message) -> None:
    # Dirty flag may have flipped; refresh the title.
    message.stop()
    self._announce_mode()


def on_code_editor_search_requested(self, message: CodeEditor.SearchRequested) -> None:
    message.stop()
    self._search_backward = message.backward

    def on_dismiss(term: str | None) -> None:
        if term is not None and self._editor is not None:
            self._search_term = term
            if not self._editor.find(term, message.backward):
                self.app.notify(f"Not found: {term}", title="Search")
        if self._editor is not None:
            self._editor.focus()

    self.app.push_screen(_SearchModal(), callback=on_dismiss)


def on_code_editor_search_step_requested(
    self, message: CodeEditor.SearchStepRequested
) -> None:
    message.stop()
    if self._editor is None or not self._search_term:
        return
    backward = self._search_backward if message.forward else not self._search_backward
    if not self._editor.find(self._search_term, backward):
        self.app.notify(f"Not found: {self._search_term}", title="Search")


def on_focus(self, event: Focus) -> None:
    # ESC from another pane focuses the CodeView; while editing the
    # editor is the thing that should take keys.
    if self._editor is not None:
        self._editor.focus()
```

Update `load_file` and `load_content` to record locality and defer while editing:

```python
def load_file(self, path: str) -> None:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        is_local = True
    except OSError:
        text = f"<Could not read {path}>"
        is_local = False
    self._install_source(text, path, is_local=is_local)


def load_content(self, content: str, path: str) -> None:
    """..."""  # keep the existing docstring
    self._install_source(content, path, is_local=False)


def _install_source(self, text: str, path: str, *, is_local: bool = True) -> None:
    """Shared body of load_file / load_content."""
    if self._editor is not None and path != self.source_path:
        # A stop in another file arrived mid-edit. Don't yank the
        # editor away; show that file once the editor closes.
        self._deferred_source = (text, path)
        self._deferred_is_local = is_local
        return
    from tdb.source_analysis import compute_step_units

    self.source_path = path
    self._source_is_local = is_local
    self._had_trailing_newline = text.endswith("\n")
    self._lines = text.splitlines()
    ...  # rest unchanged
```

In `_teardown_editor`, when applying `deferred`, pass locality: replace `self._install_source(*deferred)` with `self._install_source(deferred[0], deferred[1], is_local=self._deferred_is_local)`, and initialize `self._deferred_is_local: bool = True` in `__init__`.

Also make `enter_edit_mode` re-read the editor text from the current lines only; nothing else in the renderer changes.

- [ ] **Step 4: Run the new and existing CodeView tests**

Run: `uv run pytest tests/unit/test_code_view_edit_mode.py tests/unit/test_code_view_windowed_render.py tests/unit/test_code_view_lexer.py tests/unit/test_missing_source.py -v -p no:cacheprovider --no-cov`
Expected: all PASS. If `test_esc_cycles...` fails on the final `app.focused is cv`, the `editor.remove()` has not completed; add `await pilot.pause()` twice in the test rather than changing the widget.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/widgets/code_view.py tests/unit/test_code_view_edit_mode.py
git commit -m "CodeView: Edit mode with Esc cycle, save, dirty title, deferred loads"
```

---

### Task 5: `EmacsLayer`

**Files:**
- Modify: `src/tdb/widgets/code_editor.py` (add `EmacsLayer`), `src/tdb/widgets/code_view.py` (`_install_key_layer`)
- Test: `tests/unit/test_code_editor_emacs.py`

**Interfaces:**
- Consumes: `CodeEditor`, `KeyLayer` (Task 3).
- Produces: `class EmacsLayer` with `__init__(editor: CodeEditor)`, `handle_key(event: Key) -> bool`, `submode -> None`, `command_text -> ""`, `pending_ctrl_x: bool`, `kill_buffer: str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_code_editor_emacs.py`:

```python
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


async def test_escape_leaves():
    app = _EdApp()
    async with app.run_test() as pilot:
        await pilot.press("escape")
        assert app.leave == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_code_editor_emacs.py -v -p no:cacheprovider --no-cov`
Expected: FAIL with `ImportError: cannot import name 'EmacsLayer'`.

- [ ] **Step 3: Implement `EmacsLayer`**

Append to `src/tdb/widgets/code_editor.py` (after `CodeEditor`, before `_UnsavedChangesModal`):

```python
class EmacsLayer:
    """Emacs-flavored overrides on top of TextArea.

    Only two bits of state: a pending Ctrl+X chord and a one-slot kill
    buffer (Ctrl+K fills it, Ctrl+Y inserts it). Keys not listed here
    fall through to TextArea's defaults, which are already emacs-ish
    (Ctrl+A/E, Ctrl+W, Ctrl+U ...).
    """

    def __init__(self, editor: CodeEditor) -> None:
        self.ed = editor
        self.pending_ctrl_x = False
        self.kill_buffer = ""

    @property
    def submode(self) -> str | None:
        return None

    @property
    def command_text(self) -> str:
        return ""

    def handle_key(self, event: Key) -> bool:
        key = event.key
        ed = self.ed

        if self.pending_ctrl_x:
            self.pending_ctrl_x = False
            if key == "ctrl+s":
                ed.post_message(CodeEditor.SaveRequested())
            elif key == "ctrl+c":
                ed.post_message(CodeEditor.LeaveRequested())
            # ctrl+g and any unknown second key: chord cancelled, key eaten
            return True

        if key == "ctrl+x":
            self.pending_ctrl_x = True
            return True
        if key == "ctrl+g":
            return True
        if key == "ctrl+n":
            ed.action_cursor_down()
        elif key == "ctrl+p":
            ed.action_cursor_up()
        elif key == "ctrl+f":
            ed.action_cursor_right()
        elif key == "ctrl+b":
            ed.action_cursor_left()
        elif key in ("alt+f", "ctrl+right"):
            ed.action_cursor_word_right()
        elif key in ("alt+b", "ctrl+left"):
            ed.action_cursor_word_left()
        elif key == "ctrl+a":
            ed.action_cursor_line_start()
        elif key == "ctrl+e":
            ed.action_cursor_line_end()
        elif key == "alt+less_than_sign":
            ed.move_cursor((0, 0))
        elif key == "alt+greater_than_sign":
            ed.move_cursor(ed.document.end)
        elif key == "ctrl+d":
            ed.action_delete_right()
        elif key == "ctrl+k":
            self._kill_line()
        elif key == "ctrl+y":
            if self.kill_buffer:
                ed.insert(self.kill_buffer)
        elif key in ("ctrl+underscore", "ctrl+slash"):
            ed.undo()
        elif key == "ctrl+s":
            ed.post_message(CodeEditor.SearchRequested(backward=False))
        elif key == "ctrl+r":
            ed.post_message(CodeEditor.SearchRequested(backward=True))
        else:
            return False
        return True

    def _kill_line(self) -> None:
        ed = self.ed
        row, col = ed.cursor_location
        line = ed.document.get_line(row)
        if col < len(line):
            start, end = (row, col), (row, len(line))
        elif row + 1 < ed.document.line_count:
            start, end = (row, col), (row + 1, 0)
        else:
            return
        self.kill_buffer = ed.get_text_range(start, end)
        ed.delete(start, end)
        ed.move_cursor(start)
```

In `src/tdb/widgets/code_view.py`, fill in `_install_key_layer`:

```python
    def _install_key_layer(self, editor: CodeEditor) -> None:
        from tdb.widgets.code_editor import EmacsLayer

        if self.keybindings.scheme == "emacs":
            editor._set_layer(EmacsLayer(editor))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_code_editor_emacs.py tests/unit/test_code_editor.py -v -p no:cacheprovider --no-cov`
Expected: all PASS. If `ctrl+underscore` is delivered under a different name by the pilot, print `event.key` in a scratch test to learn the name and add it to the tuple; do not drop the binding.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/widgets/code_editor.py src/tdb/widgets/code_view.py tests/unit/test_code_editor_emacs.py
git commit -m "Add emacs key layer for Edit mode"
```

---

### Task 6: `VimLayer` — sub-modes, motions, insert entry, command line

**Files:**
- Modify: `src/tdb/widgets/code_editor.py` (add `VimLayer`), `src/tdb/widgets/code_view.py` (`_install_key_layer`)
- Test: `tests/unit/test_code_editor_vim.py`

**Interfaces:**
- Consumes: `CodeEditor` (Task 3).
- Produces: `class VimLayer` with `__init__(editor)`, `handle_key(event) -> bool`, `submode` in `{"normal", "insert", "command"}`, `command_text: str`, `count: str`, `pending: str`, `yank_buffer: str`, `yank_linewise: bool`. Task 7 adds the editing operators to the same class.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_code_editor_vim.py`:

```python
"""Vim-lite key layer for CodeEditor: sub-modes, motions, insert
entry, and the ':' command line. Editing operators are in Task 7's
section at the bottom of this file."""

from __future__ import annotations

from textual.app import App

from tdb.widgets.code_editor import CodeEditor, VimLayer

TEXT = "one two three\n  four five\nsix\nseven eight\n"


class _EdApp(App):
    def __init__(self, text: str = TEXT):
        super().__init__()
        self._text = text
        self.leave: list[bool] = []
        self.saves = 0
        self.searches: list[bool] = []
        self.steps: list[bool] = []
        self.submodes: list[str] = []

    def compose(self):
        yield CodeEditor(self._text, scheme="vim", id="ed")

    def on_mount(self):
        ed = self.query_one("#ed", CodeEditor)
        ed._set_layer(VimLayer(ed))
        ed.focus()

    def on_code_editor_leave_requested(self, msg):
        self.leave.append(msg.discard)

    def on_code_editor_save_requested(self, msg):
        self.saves += 1

    def on_code_editor_search_requested(self, msg):
        self.searches.append(msg.backward)

    def on_code_editor_search_step_requested(self, msg):
        self.steps.append(msg.forward)

    def on_code_editor_submode_changed(self, msg):
        self.submodes.append(self.query_one("#ed", CodeEditor).submode)


async def test_starts_in_normal_mode_and_swallows_letters():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        assert ed.submode == "normal"
        await pilot.press("q", "z", "enter")
        assert ed.text == TEXT


async def test_hjkl_with_counts_and_arrows():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("3", "l")
        assert ed.cursor_location == (0, 3)
        await pilot.press("2", "j")
        assert ed.cursor_location[0] == 2
        await pilot.press("k", "h")
        assert ed.cursor_location == (1, 2)
        await pilot.press("down", "right")
        assert ed.cursor_location == (2, 1)


async def test_line_motions():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("j", "$")
        assert ed.cursor_location == (1, 11)
        await pilot.press("0")
        assert ed.cursor_location == (1, 0)
        await pilot.press("^")
        assert ed.cursor_location == (1, 2)


async def test_word_motions():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("w")
        assert ed.cursor_location == (0, 4)
        await pilot.press("w")
        assert ed.cursor_location == (0, 8)
        await pilot.press("b")
        assert ed.cursor_location == (0, 4)
        await pilot.press("e")
        assert ed.cursor_location == (0, 6)


async def test_gg_G_and_count_G():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("G")
        assert ed.cursor_location[0] == 4  # trailing empty line
        await pilot.press("g", "g")
        assert ed.cursor_location == (0, 0)
        await pilot.press("3", "G")
        assert ed.cursor_location == (2, 0)


async def test_insert_entry_points_and_escape_back_to_normal():
    app = _EdApp(text="abc\n")
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("i", "X", "escape")
        assert ed.text == "Xabc\n" and ed.submode == "normal"
        await pilot.press("0", "a", "Y", "escape")
        assert ed.text == "XYabc\n"
        await pilot.press("A", "Z", "escape")
        assert ed.text == "XYabcZ\n"
        await pilot.press("I", "W", "escape")
        assert ed.text == "WXYabcZ\n"
        await pilot.press("o", "new", "escape")
        assert ed.text == "WXYabcZ\nnew\n"
        await pilot.press("O", "up", "escape")
        assert ed.text == "WXYabcZ\nup\nnew\n"
        assert app.leave == []  # Esc from insert never left Edit mode
        assert "insert" in app.submodes and app.submodes[-1] == "normal"


async def test_escape_in_normal_mode_requests_leave():
    app = _EdApp()
    async with app.run_test() as pilot:
        await pilot.press("escape")
        assert app.leave == [False]


async def test_command_line_w_q_wq_qbang_and_line_number():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press(":")
        assert ed.submode == "command"
        await pilot.press("w", "q")
        assert ed.command_text == "wq"
        await pilot.press("backspace")
        assert ed.command_text == "w"
        await pilot.press("enter")
        assert app.saves == 1 and app.leave == [] and ed.submode == "normal"
        await pilot.press(":", "q", "enter")
        assert app.leave == [False]
        await pilot.press(":", "q", "!", "enter")
        assert app.leave == [False, True]
        await pilot.press(":", "w", "q", "enter")
        assert app.saves == 2 and app.leave == [False, True, False]
        await pilot.press(":", "3", "enter")
        assert ed.cursor_location == (2, 0)
        await pilot.press(":", "x", "y", "escape")
        assert ed.submode == "normal" and ed.command_text == ""


async def test_unknown_command_notifies():
    app = _EdApp()
    async with app.run_test() as pilot:
        notes: list[str] = []
        app.notify = lambda msg, **kw: notes.append(msg)
        await pilot.press(":", "b", "o", "g", "u", "s", "enter")
        assert notes and "Unknown command" in notes[0]


async def test_search_keys_post_messages():
    app = _EdApp()
    async with app.run_test() as pilot:
        await pilot.press("/", "?", "n", "N")
        assert app.searches == [False, True]
        assert app.steps == [True, False]


async def test_page_keys():
    app = _EdApp(text="\n".join(f"l{i}" for i in range(200)) + "\n")
    async with app.run_test(size=(80, 24)) as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("ctrl+f")
        assert ed.cursor_location[0] > 5
        await pilot.press("ctrl+b")
        assert ed.cursor_location[0] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_code_editor_vim.py -v -p no:cacheprovider --no-cov`
Expected: FAIL with `ImportError: cannot import name 'VimLayer'`.

- [ ] **Step 3: Implement `VimLayer` (motions, insert, command line)**

Append to `src/tdb/widgets/code_editor.py` after `EmacsLayer`:

```python
class VimLayer:
    """Vim-lite: a normal/insert/command state machine over TextArea.

    Insert mode passes every key through to TextArea (so typing is
    native). Normal mode swallows everything it does not understand,
    so a stray letter never edits the buffer. Deliberately NOT vim:
    no visual mode, registers, text objects, `.` repeat, or macros.
    """

    # Key names Textual delivers for the punctuation we use.
    _KEYS = {
        "dollar_sign": "$",
        "circumflex_accent": "^",
        "colon": ":",
        "slash": "/",
        "question_mark": "?",
        "exclamation_mark": "!",
    }

    def __init__(self, editor: CodeEditor) -> None:
        self.ed = editor
        self._submode = "normal"
        self._command = ""
        self.count = ""
        self.pending = ""  # "", "d", "y", "g"
        self.yank_buffer = ""
        self.yank_linewise = False

    # ---- state exposed to CodeEditor / CodeView ----

    @property
    def submode(self) -> str:
        return self._submode

    @property
    def command_text(self) -> str:
        return self._command

    def _set_submode(self, submode: str) -> None:
        self._submode = submode
        self.ed.post_message(CodeEditor.SubmodeChanged())

    def _take_count(self) -> tuple[int, bool]:
        had = bool(self.count)
        n = int(self.count) if had else 1
        self.count = ""
        return max(1, n), had

    # ---- dispatch ----

    def handle_key(self, event: Key) -> bool:
        key = self._KEYS.get(event.key, event.key)
        if self._submode == "insert":
            if key == "escape":
                self._set_submode("normal")
                return True
            return False  # native TextArea typing
        if self._submode == "command":
            return self._handle_command_key(key, event.character)
        return self._handle_normal_key(key)

    # ---- command line ----

    def _handle_command_key(self, key: str, char: str | None) -> bool:
        if key == "escape":
            self._command = ""
            self._set_submode("normal")
        elif key == "enter":
            cmd = self._command.strip()
            self._command = ""
            self._set_submode("normal")
            self._run_command(cmd)
        elif key == "backspace":
            self._command = self._command[:-1]
            self.ed.post_message(CodeEditor.SubmodeChanged())
        elif char is not None and char.isprintable():
            self._command += char
            self.ed.post_message(CodeEditor.SubmodeChanged())
        return True

    def _run_command(self, cmd: str) -> None:
        ed = self.ed
        if cmd == "w":
            ed.post_message(CodeEditor.SaveRequested())
        elif cmd == "q":
            ed.post_message(CodeEditor.LeaveRequested())
        elif cmd == "q!":
            ed.post_message(CodeEditor.LeaveRequested(discard=True))
        elif cmd in ("wq", "x"):
            ed.post_message(CodeEditor.SaveRequested())
            ed.post_message(CodeEditor.LeaveRequested())
        elif cmd.isdigit():
            line = max(1, min(int(cmd), ed.document.line_count))
            ed.move_cursor((line - 1, 0))
        elif cmd:
            ed.app.notify(f"Unknown command: :{cmd}", title="Edit", severity="warning")

    # ---- normal mode ----

    def _handle_normal_key(self, key: str) -> bool:
        ed = self.ed

        # Count prefix ('0' alone is a motion)
        if key.isdigit() and (self.count or key != "0"):
            self.count += key
            return True

        if self.pending:
            op, self.pending = self.pending, ""
            return self._handle_operator(op, key)

        count, had_count = self._take_count()
        row, col = ed.cursor_location
        line = ed.document.get_line(row)

        if key == "escape":
            return False  # CodeEditor turns this into LeaveRequested
        if key in ("h", "left"):
            for _ in range(count):
                ed.action_cursor_left()
        elif key in ("l", "right"):
            for _ in range(count):
                ed.action_cursor_right()
        elif key in ("j", "down"):
            for _ in range(count):
                ed.action_cursor_down()
        elif key in ("k", "up"):
            for _ in range(count):
                ed.action_cursor_up()
        elif key == "w":
            for _ in range(count):
                ed.action_cursor_word_right()
        elif key == "b":
            for _ in range(count):
                ed.action_cursor_word_left()
        elif key == "e":
            for _ in range(count):
                self._word_end()
        elif key == "0":
            ed.move_cursor((row, 0))
        elif key == "^":
            ed.move_cursor((row, len(line) - len(line.lstrip())))
        elif key == "$":
            ed.move_cursor((row, len(line)))
        elif key == "G":
            if had_count:
                ed.move_cursor((min(count, ed.document.line_count) - 1, 0))
            else:
                ed.move_cursor((ed.document.line_count - 1, 0))
        elif key == "g":
            self.pending = "g"
        elif key in ("ctrl+f", "pagedown"):
            ed.action_cursor_page_down()
        elif key in ("ctrl+b", "pageup"):
            ed.action_cursor_page_up()
        elif key == "i":
            self._set_submode("insert")
        elif key == "a":
            if col < len(line):
                ed.move_cursor((row, col + 1))
            self._set_submode("insert")
        elif key == "I":
            ed.move_cursor((row, len(line) - len(line.lstrip())))
            self._set_submode("insert")
        elif key == "A":
            ed.move_cursor((row, len(line)))
            self._set_submode("insert")
        elif key == "o":
            ed.insert("\n", (row, len(line)))
            self._set_submode("insert")
        elif key == "O":
            ed.insert("\n", (row, 0))
            ed.move_cursor((row, 0))
            self._set_submode("insert")
        elif key == ":":
            self._command = ""
            self._set_submode("command")
        elif key == "/":
            ed.post_message(CodeEditor.SearchRequested(backward=False))
        elif key == "?":
            ed.post_message(CodeEditor.SearchRequested(backward=True))
        elif key == "n":
            ed.post_message(CodeEditor.SearchStepRequested(forward=True))
        elif key == "N":
            ed.post_message(CodeEditor.SearchStepRequested(forward=False))
        elif key in ("d", "y"):
            self.pending = key
            self.count = str(count) if had_count else ""
        else:
            self._edit_key(key, count)  # Task 7; swallows unknown keys
        return True

    def _handle_operator(self, op: str, key: str) -> bool:
        """Second key of gg / dd / dw / yy. Task 7 fills in d/y."""
        if op == "g":
            if key == "g":
                self.ed.move_cursor((0, 0))
            self.count = ""
            return True
        count, _ = self._take_count()
        return self._operator(op, key, count)

    def _operator(self, op: str, key: str, count: int) -> bool:
        return True  # Task 7

    def _edit_key(self, key: str, count: int) -> None:
        return None  # Task 7

    def _word_end(self) -> None:
        """Approximate `e`: jump to the last char of the next word."""
        ed = self.ed
        ed.action_cursor_word_right()
        row, col = ed.cursor_location
        line = ed.document.get_line(row)
        # word_right lands after the word (on the space); step back onto
        # its last character when we're not at a line boundary.
        if col > 0 and (col >= len(line) or line[col] == " "):
            ed.move_cursor((row, col - 1))
```

Update `_install_key_layer` in `code_view.py`:

```python
    def _install_key_layer(self, editor: CodeEditor) -> None:
        from tdb.widgets.code_editor import EmacsLayer, VimLayer

        if self.keybindings.scheme == "emacs":
            editor._set_layer(EmacsLayer(editor))
        elif self.keybindings.scheme == "vim":
            editor._set_layer(VimLayer(editor))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_code_editor_vim.py -v -p no:cacheprovider --no-cov`
Expected: all PASS. The `e` and `w` assertions depend on TextArea's word boundaries; if `test_word_motions` fails by one column, print the locations, adjust `_word_end` (not the test) so `e` lands on the last character of the word.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/widgets/code_editor.py src/tdb/widgets/code_view.py tests/unit/test_code_editor_vim.py
git commit -m "Add vim-lite key layer: sub-modes, motions, insert entry, command line"
```

---

### Task 7: `VimLayer` editing operators

**Files:**
- Modify: `src/tdb/widgets/code_editor.py` (`VimLayer._operator`, `VimLayer._edit_key`, helpers)
- Test: `tests/unit/test_code_editor_vim.py` (append)

**Interfaces:**
- Consumes: `VimLayer` from Task 6.
- Produces: `x X dd dw D yy p P u Ctrl+R J` with counts; `yank_buffer` / `yank_linewise` semantics.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_code_editor_vim.py`:

```python
# ---- Task 7: editing operators ----


async def test_x_and_X_with_count():
    app = _EdApp(text="abcdef\n")
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("l", "x")
        assert ed.text == "acdef\n"
        await pilot.press("2", "x")
        assert ed.text == "aef\n"
        await pilot.press("l", "X")
        assert ed.text == "ef\n"


async def test_dd_yy_p_P_linewise_with_counts():
    app = _EdApp(text="a\nb\nc\nd\n")
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("j", "2", "d", "d")
        assert ed.text == "a\nd\n"
        assert ed.cursor_location[0] == 1
        await pilot.press("p")
        assert ed.text == "a\nd\nb\nc\n"
        await pilot.press("g", "g", "y", "y", "G", "P")
        assert ed.text == "a\nd\nb\nc\na\n"


async def test_dd_on_last_line_removes_preceding_newline():
    app = _EdApp(text="a\nb")
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("j", "d", "d")
        assert ed.text == "a"


async def test_dw_D_and_J():
    app = _EdApp(text="one two three\nfour\n")
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("d", "w")
        assert ed.text == "two three\nfour\n"
        await pilot.press("w", "D")
        assert ed.text == "two \nfour\n"
        await pilot.press("J")
        assert ed.text == "two four\n"


async def test_undo_redo():
    app = _EdApp(text="abc\n")
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("x")
        assert ed.text == "bc\n"
        await pilot.press("u")
        assert ed.text == "abc\n"
        await pilot.press("ctrl+r")
        assert ed.text == "bc\n"


async def test_charwise_put_after_dw():
    app = _EdApp(text="one two\n")
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("d", "w", "$", "p")
        assert ed.text == "twoone \n"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_code_editor_vim.py -v -p no:cacheprovider --no-cov -k "x_and_X or dd_yy or last_line or dw_D or undo_redo or charwise"`
Expected: FAIL (text unchanged assertions).

- [ ] **Step 3: Implement the operators**

Replace the two stubs in `VimLayer` and add helpers:

```python
def _operator(self, op: str, key: str, count: int) -> bool:
    ed = self.ed
    row, _ = ed.cursor_location
    if key == op:  # dd / yy
        start, end, text = self._line_range(row, count)
        self.yank_buffer, self.yank_linewise = text, True
        if op == "d":
            ed.delete(start, end)
            ed.move_cursor((min(row, ed.document.line_count - 1), 0))
    elif op == "d" and key == "w":
        start = ed.cursor_location
        for _ in range(count):
            ed.action_cursor_word_right()
        end = ed.cursor_location
        self.yank_buffer, self.yank_linewise = ed.get_text_range(start, end), False
        ed.delete(start, end)
        ed.move_cursor(start)
    # any other second key: operator cancelled
    return True


def _line_range(
    self, row: int, count: int
) -> tuple[tuple[int, int], tuple[int, int], str]:
    """Location span covering `count` whole lines from `row`, and
    the text (always newline-terminated) they contain. Deleting the
    last line takes the preceding newline instead of a trailing one."""
    ed = self.ed
    last = ed.document.line_count - 1
    end_row = min(row + count, last + 1)
    if end_row <= last:
        start, end = (row, 0), (end_row, 0)
        text = ed.get_text_range(start, end)
    else:
        end = ed.document.end
        text = ed.get_text_range((row, 0), end)
        if not text.endswith("\n"):
            text += "\n"
        start = (row - 1, len(ed.document.get_line(row - 1))) if row > 0 else (0, 0)
    return start, end, text


def _edit_key(self, key: str, count: int) -> None:
    ed = self.ed
    row, col = ed.cursor_location
    line = ed.document.get_line(row)
    if key == "x":
        end_col = min(len(line), col + count)
        if end_col > col:
            self.yank_buffer, self.yank_linewise = line[col:end_col], False
            ed.delete((row, col), (row, end_col))
            ed.move_cursor((row, col))
    elif key == "X":
        start_col = max(0, col - count)
        if start_col < col:
            self.yank_buffer, self.yank_linewise = line[start_col:col], False
            ed.delete((row, start_col), (row, col))
            ed.move_cursor((row, start_col))
    elif key == "D":
        self.yank_buffer, self.yank_linewise = line[col:], False
        ed.delete((row, col), (row, len(line)))
        ed.move_cursor((row, col))
    elif key == "J":
        for _ in range(count):
            self._join_line()
    elif key == "p":
        self._put(after=True)
    elif key == "P":
        self._put(after=False)
    elif key == "u":
        for _ in range(count):
            ed.undo()
    elif key == "ctrl+r":
        for _ in range(count):
            ed.redo()
    # anything else: swallowed


def _join_line(self) -> None:
    ed = self.ed
    row, _ = ed.cursor_location
    if row + 1 >= ed.document.line_count:
        return
    line = ed.document.get_line(row)
    nxt = ed.document.get_line(row + 1)
    lead = len(nxt) - len(nxt.lstrip())
    ed.replace(" " if nxt.strip() else "", (row, len(line)), (row + 1, lead))
    ed.move_cursor((row, len(line)))


def _put(self, *, after: bool) -> None:
    ed = self.ed
    if not self.yank_buffer:
        return
    row, col = ed.cursor_location
    if self.yank_linewise:
        text = self.yank_buffer
        if after:
            if row + 1 < ed.document.line_count:
                ed.insert(text, (row + 1, 0))
                ed.move_cursor((row + 1, 0))
            else:
                line = ed.document.get_line(row)
                ed.insert("\n" + text.rstrip("\n"), (row, len(line)))
                ed.move_cursor((row + 1, 0))
        else:
            ed.insert(text, (row, 0))
            ed.move_cursor((row, 0))
    else:
        line = ed.document.get_line(row)
        at = (row, min(len(line), col + 1)) if after else (row, col)
        ed.insert(self.yank_buffer, at)
```

- [ ] **Step 4: Run the whole vim test module**

Run: `uv run pytest tests/unit/test_code_editor_vim.py -v -p no:cacheprovider --no-cov`
Expected: all PASS. If `test_dd_on_last_line_removes_preceding_newline` fails because `TextArea` normalizes a document without a trailing newline differently, print `ed.text` and adjust `_line_range`'s last-line branch; the intended result is exactly `"a"`.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/widgets/code_editor.py tests/unit/test_code_editor_vim.py
git commit -m "Add vim-lite editing operators: x X dd dw D yy p P u ctrl+r J"
```

---

### Task 8: App integration — title, `FileSaved` remap, `controller.replace_breakpoints`, refusal flags

**Files:**
- Modify: `src/tdb/session/controller.py` (after `toggle_breakpoint`), `src/tdb/app.py` (`_update_code_title`, new `on_code_view_file_saved`, `on_mount`, `_enter_post_mortem`)
- Test: `tests/unit/test_app_edit_mode.py`

**Interfaces:**
- Consumes: `CodeView.FileSaved`, `CodeView.mode_label()`, `CodeView.edit_enabled` (Task 4); `remap_breakpoints` (Task 2).
- Produces: `DebugController.replace_breakpoints(source_path: str, bps: list[SourceBreakpoint]) -> None` (async); `TdbApp.on_code_view_file_saved`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_app_edit_mode.py`:

```python
"""TdbApp side of Edit mode: pane title, breakpoint remap after save,
refusal flags for replay / post-mortem, and the unsaved-edits guard on
quit and restart (Task 9 appends to this file)."""

from __future__ import annotations

from pathlib import Path

from tdb.app import TdbApp
from tdb.dap.types import SourceBreakpoint
from tdb.persist import TdbConfig
from tdb.widgets.code_editor import CodeEditor
from tdb.widgets.code_view import CodeView


def _write(tmp_path: Path, text: str = "x = 1\ny = 2\nz = 3\n") -> str:
    p = tmp_path / "prog.py"
    p.write_text(text, encoding="utf-8")
    return str(p)


async def _enter_edit(app: TdbApp, pilot, path: str) -> CodeView:
    cv = app.query_one("#code-view", CodeView)
    cv.load_file(path)
    cv.focus()
    await pilot.press("escape", "escape")
    await pilot.pause()
    assert cv.is_editing
    return cv


async def test_title_reflects_edit_state(tmp_path):
    app = TdbApp(program="", config=TdbConfig(keybindings="vim"))
    async with app.run_test() as pilot:
        await pilot.pause()
        cv = await _enter_edit(app, pilot, _write(tmp_path))
        assert "Edit" in cv.border_title and "*" not in cv.border_title
        await pilot.press("i", "q")
        await pilot.pause()
        assert "Edit:INSERT*" in cv.border_title
        await pilot.press("escape")
        await pilot.pause()
        assert "Edit*" in cv.border_title and "INSERT" not in cv.border_title


async def test_save_remaps_breakpoints_and_notifies(tmp_path):
    app = TdbApp(program="", config=TdbConfig(keybindings="default"))
    async with app.run_test() as pilot:
        await pilot.pause()
        path = _write(tmp_path)
        app.controller.state.breakpoints[path] = [
            SourceBreakpoint(line=2, condition="y"),
            SourceBreakpoint(line=3),
        ]
        sent: list[tuple[str, list[int]]] = []

        async def fake_replace(source_path, bps):
            app.controller.state.breakpoints[source_path] = list(bps)
            sent.append((source_path, [bp.line for bp in bps]))

        app.controller.replace_breakpoints = fake_replace
        notes: list[str] = []
        app.notify = lambda msg, **kw: notes.append(msg)

        cv = await _enter_edit(app, pilot, path)
        cv.current_line = 2
        ed = app.query_one(CodeEditor)
        ed.move_cursor((0, 0))
        await pilot.press("enter")  # insert a blank line above everything
        await pilot.press("ctrl+s")
        await pilot.pause()

        assert sent == [(path, [3, 4])]
        assert app.controller.state.breakpoints[path][0].condition == "y"
        assert cv.current_line is None
        assert any("Saved prog.py" in n for n in notes)


class _FakeReplayDriver:
    """on_mount schedules `replay_driver.run(app)` as a task; a no-op
    coroutine is enough to exercise the edit_enabled gate."""

    async def run(self, app):
        return None


async def test_replay_disables_editing():
    app = TdbApp(program="", config=TdbConfig(), replay_driver=_FakeReplayDriver())
    async with app.run_test() as pilot:
        await pilot.pause()
        cv = app.query_one("#code-view", CodeView)
        assert cv.edit_enabled is False
        assert cv.edit_refusal_reason() is not None
```

`on_mount` starts the driver with `asyncio.create_task(self._replay_driver.run(self), ...)` (see `src/tdb/app.py` around line 409), so the fake only needs an awaitable `run`. Post-mortem sets the same flag via `_post_mortem_snapshot is not None`; it is covered by the `on_mount` condition rather than by building a full snapshot in a test.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_app_edit_mode.py -v -p no:cacheprovider --no-cov`
Expected: FAIL (`border_title` lacks `INSERT`, `sent == []`, `edit_enabled` still True).

- [ ] **Step 3: Implement**

`src/tdb/session/controller.py`, after `toggle_breakpoint`:

```python
    async def replace_breakpoints(
        self, source_path: str, bps: list[SourceBreakpoint]
    ) -> None:
        """Replace one file's breakpoint list wholesale (used after an
        edit shifts line numbers) and push it to the adapter when a
        session is live."""
        self.state.breakpoints[source_path] = list(bps)
        if self.state.is_ready and not self.state.is_terminated:
            await self._send_breakpoints(source_path, self._enabled_bps(bps))
```

`src/tdb/app.py`:

Replace `_update_code_title`:

```python
    def _update_code_title(self, code_view: CodeView) -> None:
        label = code_view.mode_label()
        if code_view.mode == Mode.NAVIGATION:
            styled = f"[red]{label}[/]"
        elif code_view.mode == Mode.EDIT:
            styled = f"[yellow]{label}[/]"
        else:
            styled = label
        code_view.border_title = f"[bold orange]C[/]ode \\[{styled}]"
```

Add `from tdb.keybindings import KeybindingConfig, Mode` to the existing import, and `from tdb.source_edit import remap_breakpoints`.

Add after `on_code_view_mode_changed`:

```python
async def on_code_view_file_saved(self, message: CodeView.FileSaved) -> None:
    """After a save: shift this file's breakpoints across the edit,
    re-push them, drop the stale current-line marker, and tell the
    user how to run the new code. No hot reload."""
    state = self.controller.state
    code_view = self.query_one("#code-view", CodeView)
    old = state.breakpoints.get(message.path, [])
    if old:
        new = remap_breakpoints(message.old_lines, message.new_lines, old)
        try:
            await self.controller.replace_breakpoints(message.path, new)
        except Exception:
            log.exception("Error re-pushing breakpoints after save")
            state.breakpoints[message.path] = new
        self.post_message(self.BreakpointsChanged())
    code_view.current_line = None
    name = Path(message.path).name
    if self.controller.supports_restart and not state.is_post_mortem:
        self.notify(
            f"Saved {name}. Press R to restart with the new code.", title="Edit"
        )
    else:
        self.notify(f"Saved {name}.", title="Edit")
```

In `on_mount`, right after `code_view.keybindings = ...`:

```python
        # Replay drives the UI from a recording; post-mortem shows a
        # frozen snapshot. Neither has a file the user should edit.
        if self._replay_driver is not None or self._post_mortem_snapshot is not None:
            code_view.edit_enabled = False
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_app_edit_mode.py tests/unit/test_open_file_gate.py tests/unit/test_app_adopted_session.py -v -p no:cacheprovider --no-cov`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/session/controller.py src/tdb/app.py tests/unit/test_app_edit_mode.py
git commit -m "App: edit-mode title, breakpoint remap on save, disable editing in replay/post-mortem"
```

---

### Task 9: Unsaved-edits guard on quit, restart, and File > Open

**Files:**
- Modify: `src/tdb/app.py` (`action_quit_debugger`, `_detach_and_exit`, `_terminate_and_exit`, `_restart_session`)
- Test: `tests/unit/test_app_edit_mode.py` (append)

**Interfaces:**
- Consumes: `CodeView.is_dirty`, `save_file()`, `discard_edits()`, `_UnsavedChangesModal` (Tasks 3-4).
- Produces: `async TdbApp._confirm_discard_edits() -> bool` (worker-only), `async TdbApp._quit_debugger_flow()`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_app_edit_mode.py`:

```python
# ---- Task 9: unsaved-edits guard ----

from tdb.widgets.code_editor import _UnsavedChangesModal  # noqa: E402


async def _dirty_app(tmp_path, pilot_size=None):
    app = TdbApp(program="", config=TdbConfig(keybindings="default"))
    return app


async def test_ctrl_q_with_dirty_buffer_prompts_and_cancel_aborts(tmp_path):
    app = TdbApp(program="", config=TdbConfig(keybindings="default"))
    async with app.run_test() as pilot:
        await pilot.pause()
        cv = await _enter_edit(app, pilot, _write(tmp_path))
        await pilot.press("z")
        exits: list[str] = []
        app.exit = lambda *a, **kw: exits.append("exit")
        await pilot.press("ctrl+q")
        await pilot.pause()
        assert isinstance(app.screen, _UnsavedChangesModal)
        await pilot.press("escape")
        await pilot.pause()
        assert exits == [] and cv.is_editing and cv.is_dirty
        assert app._is_quitting is False


async def test_ctrl_q_with_dirty_buffer_save_then_quits(tmp_path):
    app = TdbApp(program="", config=TdbConfig(keybindings="default"))
    async with app.run_test() as pilot:
        await pilot.pause()
        path = _write(tmp_path)
        await _enter_edit(app, pilot, path)
        await pilot.press("z")
        exits: list[str] = []
        app.exit = lambda *a, **kw: exits.append("exit")

        async def fake_stop():
            return None

        app.controller.stop = fake_stop
        await pilot.press("ctrl+q")
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        await pilot.pause()
        assert exits == ["exit"]
        assert open(path, encoding="utf-8").read().startswith("z")


async def test_restart_with_dirty_buffer_prompts_and_discard_proceeds(
    tmp_path, monkeypatch
):
    app = TdbApp(program="", config=TdbConfig(keybindings="default"))
    async with app.run_test() as pilot:
        await pilot.pause()
        path = _write(tmp_path)
        cv = await _enter_edit(app, pilot, path)
        await pilot.press("z")
        monkeypatch.setattr(
            type(app.controller), "supports_restart", property(lambda self: True)
        )
        recorded: list[str] = []
        app.recorder.record = lambda name, args: recorded.append(name)

        async def fake_restart_body(*a, **kw):
            recorded.append("body")

        # Stop the real relaunch: patch what runs after the guard.
        monkeypatch.setattr(app, "_start_session", lambda *a, **kw: None)
        app._restart_session()
        await pilot.pause()
        assert isinstance(app.screen, _UnsavedChangesModal)
        await pilot.press("d")
        await pilot.pause()
        await pilot.pause()
        assert not cv.is_editing
        assert "restart" in recorded
        assert open(path, encoding="utf-8").read() == "x = 1\ny = 2\nz = 3\n"
```

If `_restart_session` needs more of the controller than `_start_session` stubbed out (read `src/tdb/app.py:625-735` before running), patch the additional collaborator it touches after the guard (for example `app.controller.stop`) with an async no-op in the same way; the assertions are about the prompt appearing, discard tearing down the editor, and the restart being recorded after the prompt.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_app_edit_mode.py -v -p no:cacheprovider --no-cov -k "ctrl_q or restart_with_dirty"`
Expected: FAIL (`app.screen` is not the modal; exit happens immediately).

- [ ] **Step 3: Implement the guard**

In `src/tdb/app.py`, add near `_update_code_title`:

```python
    async def _confirm_discard_edits(self) -> bool:
        """Worker-only. True when it is safe to abandon the Code View's
        edit buffer: nothing unsaved, or the user chose Save (and it
        succeeded) or Discard. False on Cancel or a failed save.
        Awaits a modal, so callers must run inside a worker."""
        code_view = self.query_one("#code-view", CodeView)
        if not code_view.is_dirty:
            return True
        name = Path(code_view.source_path).name if code_view.source_path else "buffer"
        result = await self.push_screen(_UnsavedChangesModal(name), wait_for_dismiss=True)
        if result == "save":
            return code_view.save_file()
        if result == "discard":
            code_view.discard_edits()
            return True
        return False
```

Import `_UnsavedChangesModal` from `tdb.widgets.code_editor` at the top of `app.py`.

Restructure quit so the Ctrl+Q path runs in a worker:

```python
async def action_quit_debugger(self) -> None:
    if self._adopted:
        # Adopted sessions route EVERY quit path — Ctrl+Q included —
        # through the detach/terminate choice.
        self.action_confirm_quit()
        return
    if self._is_quitting:
        return
    # The unsaved-edits prompt awaits a modal, which needs a worker.
    self.run_worker(self._quit_debugger_flow(), name="quit")


async def _quit_debugger_flow(self) -> None:
    if not await self._confirm_discard_edits():
        return
    # Idempotent: Ctrl+Q and the q-confirm path both land here, and
    # `controller.stop()` can take a moment when many child DAP
    # sessions need to be torn down — without this guard, a second
    # keypress would spawn another concurrent shutdown.
    if self._is_quitting:
        return
    self._is_quitting = True
    self.recorder.record("quit", [])
    # ... the rest of the former action_quit_debugger body, unchanged:
    # save_breakpoints, await self.controller.stop(), uvicorn flag, self.exit()
```

In `_detach_and_exit` and `_terminate_and_exit`, insert as the first statement:

```python
        if not await self._confirm_discard_edits():
            return
```

In `_restart_session`, immediately after the `supports_restart` guard's `return` and before `if new_program is None: self.recorder.record("restart", [])`:

```python
        # Restart is the moment the user wants the new code on disk.
        if not await self._confirm_discard_edits():
            return
```

File > Open already funnels into `_restart_session(new_program=...)`, so it is covered.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_app_edit_mode.py tests/unit/test_app_adopted_session.py tests/unit/test_error_modal_routing.py tests/unit/test_record_hooks_stepping.py -v -p no:cacheprovider --no-cov`
Expected: all PASS. If a pre-existing test awaited `app.action_quit_debugger()` and expected `app.exit` to have been called synchronously, add `await pilot.pause()` after the await in that test (the quit now runs in a worker); do not change the worker structure.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/app.py tests/unit/test_app_edit_mode.py
git commit -m "Guard quit, restart, and File > Open on unsaved edits"
```

---

### Task 10: Edit menu, Alt+E, and Open in $EDITOR

**Files:**
- Modify: `src/tdb/app.py` (`BINDINGS`, `compose`, `on_option_list_option_selected`, new `action_menu_edit`, `_edit_menu_action`, `_open_in_external_editor`)
- Test: `tests/unit/test_app_edit_mode.py` (append)

**Interfaces:**
- Consumes: `resolve_external_editor` (Task 2); `CodeView.enter_edit_mode / save_file / revert_to_disk / leave_edit_mode / edit_refusal_reason / lines()` (Task 4); `_confirm_discard_edits` (Task 9).
- Produces: menu items `Edit > Save`, `Edit > Revert to Disk`, `Edit > Discard and Exit Edit Mode`, `Edit > Open in $EDITOR`; `TdbApp.action_menu_edit`; `TdbApp._open_in_external_editor()` (worker).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_app_edit_mode.py`:

```python
# ---- Task 10: Edit menu + $EDITOR ----

import subprocess  # noqa: E402
from contextlib import contextmanager  # noqa: E402


async def test_edit_menu_exists_and_alt_e_opens_it():
    app = TdbApp(program="", config=TdbConfig())
    async with app.run_test() as pilot:
        await pilot.pause()
        from tdb.widgets.menu_bar import MenuBar

        bar = app.query_one("#menu-bar", MenuBar)
        assert "Edit" in bar._menus
        assert bar._menus["Edit"] == [
            "Save",
            "Revert to Disk",
            "Discard and Exit Edit Mode",
            "Open in $EDITOR",
        ]
        await pilot.press("alt+e")
        await pilot.pause()
        assert bar._open_menu == "Edit"


async def test_edit_menu_save_and_discard(tmp_path):
    app = TdbApp(program="", config=TdbConfig(keybindings="default"))
    async with app.run_test() as pilot:
        await pilot.pause()
        path = _write(tmp_path)
        notes: list[str] = []
        app.notify = lambda msg, **kw: notes.append(msg)
        app._edit_menu_action("Save")
        assert notes[-1].startswith("Nothing to save")
        cv = await _enter_edit(app, pilot, path)
        await pilot.press("z")
        app._edit_menu_action("Save")
        await pilot.pause()
        assert open(path, encoding="utf-8").read().startswith("z")
        await pilot.press("q")
        app._edit_menu_action("Revert to Disk")
        assert not cv.is_dirty and cv.is_editing
        await pilot.press("q")
        app._edit_menu_action("Discard and Exit Edit Mode")
        await pilot.pause()
        assert not cv.is_editing
        assert open(path, encoding="utf-8").read() == "zx = 1\ny = 2\nz = 3\n"


async def test_open_in_external_editor_reloads_and_remaps(tmp_path, monkeypatch):
    app = TdbApp(program="", config=TdbConfig(keybindings="default"))
    async with app.run_test() as pilot:
        await pilot.pause()
        path = _write(tmp_path)
        cv = app.query_one("#code-view", CodeView)
        cv.load_file(path)
        app.controller.state.breakpoints[path] = [SourceBreakpoint(line=2)]

        async def fake_replace(source_path, bps):
            app.controller.state.breakpoints[source_path] = list(bps)

        app.controller.replace_breakpoints = fake_replace
        monkeypatch.setenv("EDITOR", "fake-editor")

        @contextmanager
        def fake_suspend():
            yield

        monkeypatch.setattr(app, "suspend", fake_suspend)
        calls: list[list[str]] = []

        def fake_run(argv, **kw):
            calls.append(list(argv))
            Path(path).write_text("# new\nx = 1\ny = 2\nz = 3\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        await app._open_in_external_editor()
        await pilot.pause()
        assert calls == [["fake-editor", path]]
        assert cv.lines()[0] == "# new"
        assert [bp.line for bp in app.controller.state.breakpoints[path]] == [3]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_app_edit_mode.py -v -p no:cacheprovider --no-cov -k "menu or external"`
Expected: FAIL (`"Edit" not in bar._menus`, `AttributeError: _edit_menu_action`).

- [ ] **Step 3: Implement**

In `src/tdb/app.py`:

`BINDINGS` — after the `alt+c` line:

```python
(Binding("alt+e", "menu_edit", "Edit menu", show=False),)
```

`compose` — the `MenuBar` menus dict becomes:

```python
(
    {
        "Edit": [
            "Save",
            "Revert to Disk",
            "Discard and Exit Edit Mode",
            "Open in $EDITOR",
        ],
        "Configure": ["Color Theme", "Keybindings", "Step Mode"],
        "Help": ["Documentation", "About"],
    },
)
```

`on_option_list_option_selected` — add before the `Configure` branches:

```python
        if menu == "Edit":
            self._edit_menu_action(item)
        elif menu == "Configure" and item == "Color Theme":
```

(and turn the former leading `if` into `elif`).

Add next to `action_menu_configure`:

```python
def action_menu_edit(self) -> None:
    self.query_one("#menu-bar", MenuBar).open_menu("Edit")


def _edit_menu_action(self, item: str) -> None:
    code_view = self.query_one("#code-view", CodeView)
    if item == "Save":
        if not code_view.is_editing:
            self.notify("Nothing to save: not in Edit mode.", title="Edit")
        elif not code_view.is_dirty:
            self.notify("No unsaved changes.", title="Edit")
        else:
            code_view.save_file()
    elif item == "Revert to Disk":
        if not code_view.is_editing or not code_view.is_dirty:
            self.notify("No unsaved changes.", title="Edit")
        elif code_view.revert_to_disk():
            self.notify("Reverted to the on-disk contents.", title="Edit")
    elif item == "Discard and Exit Edit Mode":
        if not code_view.is_editing:
            self.notify("Not in Edit mode.", title="Edit")
        else:
            code_view.leave_edit_mode(discard=True)
    elif item == "Open in $EDITOR":
        self.run_worker(self._open_in_external_editor(), name="external-editor")


async def _open_in_external_editor(self) -> None:
    """Suspend the TUI, run $VISUAL / $EDITOR on the current file,
    then reload it and remap breakpoints if it changed on disk."""
    import subprocess

    from tdb.source_edit import resolve_external_editor

    code_view = self.query_one("#code-view", CodeView)
    reason = code_view.edit_refusal_reason()
    if reason is not None:
        self.notify(reason, title="Edit", severity="warning")
        return
    if not await self._confirm_discard_edits():
        return
    if code_view.is_editing:
        # Buffer is clean or just saved; the file on disk is current.
        code_view.leave_edit_mode(discard=True)
    path = code_view.source_path
    assert path is not None
    old_lines = code_view.lines()
    try:
        before = os.stat(path).st_mtime_ns
    except OSError:
        before = None
    argv = resolve_external_editor() + [path]
    try:
        with self.suspend():
            subprocess.run(argv)
    except OSError as exc:
        self.notify(f"Could not run {argv[0]}: {exc}", title="Edit", severity="error")
        return
    try:
        after = os.stat(path).st_mtime_ns
    except OSError:
        after = None
    if after == before:
        return
    code_view.load_file(path)
    self.post_message(CodeView.FileSaved(path, old_lines, code_view.lines()))
```

`os` is already imported in `app.py` (used by `os.path.isfile`); verify, and add `import os` if not.

Update the README's menu-bar shortcut table in Task 12; no docs change here.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_app_edit_mode.py -v -p no:cacheprovider --no-cov`
Expected: all PASS. If `FileSaved` posted from the App is not delivered to `on_code_view_file_saved` (the handler name is derived from the message's owning class, `CodeView`, so it should be), post it via `code_view.post_message(...)` instead.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/app.py tests/unit/test_app_edit_mode.py
git commit -m "Add Edit menu (Save, Revert, Discard, Open in \$EDITOR) and Alt+E"
```

---

### Task 11: Keybindings dialog and CLI relabel

**Files:**
- Modify: `src/tdb/widgets/modals.py` (`_KeybindingsModal.compose`, `_render_bindings`), `src/tdb/cli.py:153-157`
- Test: `tests/unit/test_keybindings_modal.py` (create)

**Interfaces:**
- Consumes: `scheme_label`, `Mode.EDIT`, `format_bindings(Mode.EDIT)` (Task 1).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_keybindings_modal.py`:

```python
"""Keybindings dialog: Notepad-style label for the `default` scheme
and the Edit Mode section."""

from __future__ import annotations

from textual.app import App
from textual.widgets import RadioButton

from tdb.keybindings import KeybindingConfig
from tdb.widgets.modals import _KeybindingsModal


class _App(App):
    def __init__(self, scheme: str):
        super().__init__()
        self._scheme = scheme
        self.chosen: list[str] = []

    def on_mount(self):
        self.push_screen(
            _KeybindingsModal(
                KeybindingConfig.from_scheme(self._scheme), self.chosen.append
            )
        )


async def test_default_scheme_is_labelled_notepad_style():
    app = _App("default")
    async with app.run_test() as pilot:
        await pilot.pause()
        labels = [str(rb.label) for rb in app.screen.query(RadioButton)]
        assert labels == ["Vim", "Emacs", "Notepad-style"]
        pressed = [rb for rb in app.screen.query(RadioButton) if rb.value]
        assert pressed and pressed[0].id == "scheme-default"


async def test_edit_mode_section_lists_scheme_keys():
    for scheme, marker in (
        ("vim", ":w"),
        ("emacs", "Ctrl+X Ctrl+S"),
        ("default", "Ctrl+S"),
    ):
        app = _App(scheme)
        async with app.run_test() as pilot:
            await pilot.pause()
            text = app.screen._render_bindings()
            assert "Edit Mode" in text
            assert marker in text


def test_cli_help_mentions_notepad():
    from tdb.cli import build_parser  # adjust to the real parser factory name

    help_text = build_parser().format_help()
    assert "Notepad-style" in help_text
```

Before running, open `src/tdb/cli.py` and find the function that builds the `argparse` parser (search for `add_argument("--keybindings"`). Use its real name in the last test; if the parser is built inline inside `main()`/`parse_args()`, call that with `["--help"]` inside `pytest.raises(SystemExit)` and capture `capsys` instead.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_keybindings_modal.py -v -p no:cacheprovider --no-cov`
Expected: FAIL (`labels[-1] == "Default"`, no "Edit Mode" section, no "Notepad-style" in help).

- [ ] **Step 3: Implement**

`src/tdb/widgets/modals.py`, in `_KeybindingsModal`:

- import `scheme_label` alongside `KeybindingConfig, Mode` from `tdb.keybindings`.
- header `Static`: `"[bold]Keybindings[/bold]  (ESC cycles Debug → Navigate → Edit)"`.
- radio buttons: `RadioButton(scheme_label(scheme), value=..., id=f"scheme-{scheme}")`.
- `_render_bindings`: after the Debug Mode block and before the closing `[dim]` line, add:

```python
        lines.append("")
        lines.append(
            f"[bold underline]Edit Mode ({scheme_label(self._config.scheme)})[/bold underline]"
        )
        for key_display, description in self._config.format_bindings(Mode.EDIT):
            lines.append(fmt(key_display, description))
```

`src/tdb/cli.py:153-157`:

```python
    parser.add_argument(
        "--keybindings",
        choices=["default", "vim", "emacs"],
        default=None,
        help="Keybinding scheme for code navigation and editing: vim, emacs, "
        "or default (Notepad-style editing). Saved to config.",
    )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_keybindings_modal.py tests/unit/test_keybindings.py -v -p no:cacheprovider --no-cov`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/widgets/modals.py src/tdb/cli.py tests/unit/test_keybindings_modal.py
git commit -m "Keybindings dialog: Notepad-style label and Edit Mode section"
```

---

### Task 12: `edit` extra, README, and full test run

**Files:**
- Modify: `pyproject.toml`, `README.md`

- [ ] **Step 1: Add the optional extra**

In `pyproject.toml`, under `[project.optional-dependencies]`, add before `dev`:

```toml
  edit = ["textual[syntax]>=8.0.0"]
```

Verify it resolves: `uv pip install -e ".[edit]" --dry-run` (or `uv sync --extra edit --dry-run`). Do not leave tree-sitter installed in the dev environment afterwards unless it was already there, so `test_editor_language_for_without_tree_sitter` keeps exercising the plain path locally. (The test patches `TREE_SITTER` either way.)

- [ ] **Step 2: Document in README.md**

Around line 964 (the Navigation section), change:

```
By default the Code View is in Debug mode.  Hit `Escape` to switch to Navigate mode
```
to
```
By default the Code View is in Debug mode.  `Escape` cycles Debug → Navigate → Edit → Debug.
```

and `Switch from Navigate back to Debug mode with `Escape`.` to `Press `Escape` again to enter Edit mode (below), and once more to return to Debug mode.`

Add a new subsection right after the Navigation table and before `### Debugging Controls`:

```markdown
**Edit mode:**

Press `Escape` twice from Debug mode to open the current file in an editor
inside the Code View. The pane title shows `[Edit]`, with `*` while there
are unsaved changes. Editing keys follow the keybinding scheme:

| Scheme | Editing style | Save | Leave Edit mode |
|--------|---------------|------|-----------------|
| `vim` | vim-lite: normal / insert modes, counts, `h j k l w b e 0 ^ $ gg G`, `x dd dw D yy p P u Ctrl+R J`, `i a I A o O`, `/ ? n N` | `:w` or `Ctrl+S` | `Esc` (from normal mode), `:q`, `:wq`, `:q!` |
| `emacs` | `Ctrl+N/P/F/B`, `Alt+F/B`, `Ctrl+A/E`, `Ctrl+K` / `Ctrl+Y`, `Ctrl+_` undo, `Ctrl+S` / `Ctrl+R` search | `Ctrl+X Ctrl+S` | `Esc` or `Ctrl+X Ctrl+C` |
| `default` (Notepad-style) | arrows, Home/End, PgUp/PgDn, Delete/Backspace, Shift+arrows to select, `Ctrl+Z`/`Ctrl+Y`, `Ctrl+X`/`Ctrl+C`/`Ctrl+V` | `Ctrl+S` | `Esc` |

Leaving Edit mode, quitting, restarting, or opening another file with
unsaved changes prompts: `s` save, `d` discard, `Esc` keep editing.

Saving does not change the running program. tdb shifts your breakpoints
to their new lines, clears the current-line marker, and reminds you to
press `R` to restart with the new code. Edit mode is unavailable for
sources that are not on this machine (remote attach), during replay, and
in post-mortem mode.

The `Edit` menu (`Alt+E`) offers Save, Revert to Disk, Discard and Exit
Edit Mode, and Open in $EDITOR, which suspends tdb, runs `$VISUAL` /
`$EDITOR` (`notepad` on Windows, `vi` otherwise) on the file, and reloads
it when the editor exits.

Syntax highlighting inside the editor needs tree-sitter, an optional
extra: `uv pip install "textual-debugger[edit]"` (highlights Python, Bash,
Go, and Rust; other languages edit as plain text). Highlighting in the
normal Code View is unaffected.
```

In the menu-bar shortcut table add a row after `Alt+F`:

```
| `Alt+E` | Edit (Save, Revert to Disk, Discard and Exit Edit Mode, Open in $EDITOR) |
```

At line 1565-1567 and 1845, relabel: `tdb --keybindings default my_program.py   # Notepad-style editing` and `| `--keybindings SCHEME` | `vim`, `emacs`, or `default` (Notepad-style editing); saved to config |`.

- [ ] **Step 3: Run the full unit suite, then the whole suite**

Run: `uv run pytest tests/unit -p no:cacheprovider -q`
Expected: all PASS.

Run: `uv run pytest -q`
Expected: PASS, apart from integration tests that skip for missing toolchains (perl, ruby, go, ...). Any failure in a test touching quit, restart, breakpoints, or CodeView is caused by this work and must be fixed before proceeding. If the full suite is too heavy for the machine (see the OOM note in the project memory), run `tests/unit` plus `tests/integration/test_dap_session.py`.

- [ ] **Step 4: Lint**

Run: `uv run ruff check src tests && uv run ruff format --check src tests`
Expected: clean. Fix anything reported.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml README.md
git commit -m "Document Edit mode; add [edit] extra for editor syntax highlighting"
```

---

## Self-review against the spec

- **§1 Mode model and Esc cycle:** Task 1 (`Mode.EDIT`), Task 4 (`_on_key` cycle, refusal keeps the cycle moving, `leave_edit_mode` as the single exit, `mode_label` with `*`, `:INSERT`, and the command line), Task 6 (vim Esc insert→normal, `:q`/`:wq`/`:q!`), Task 5 (`Ctrl+X Ctrl+C`), Task 10 (menu exits). Deviation, deliberate: the typed `:` command shows in the pane title (`Code [Edit :wq]`) rather than the footer, because the footer only renders bindings.
- **§2 Widget integration:** Task 3 (`CodeEditor` config: line numbers, no soft wrap, tab indents, tree-sitter gating), Task 4 (mount/unmount, cursor round-trip, `_install_source` reuse, `is_editing`/`is_dirty`/`editor_submode`, deferred loads for a stop in another file, `on_focus` redirect). No breakpoint gutter while editing: `_CodeContent` is hidden.
- **§3 Keybinding schemes:** Task 3 (Notepad = TextArea defaults, Insert no-op, Ctrl+S), Task 5 (emacs table incl. Ctrl+Right/Left aliases, Ctrl+S as search), Tasks 6-7 (vim table, unbound keys swallowed, single yank buffer with line-wise flag, unknown `:` command notification), Task 1 + Task 11 (Edit Mode section, `Notepad-style` radio label).
- **§4 Save and dirty state:** Task 2 (`atomic_write_text`), Task 4 (`save_file` OSError path, `_UnsavedChangesModal` on leave), Task 9 (guard on every quit path, restart, File > Open via `_restart_session`).
- **§5 Debug session interaction:** Task 4 (editing allowed regardless of debuggee state; stops in another file deferred), Task 8 (`FileSaved` → `remap_breakpoints` → `replace_breakpoints` → `BreakpointsChanged`, clear current line, notify with/without "Press R"), Task 8 (`edit_enabled=False` for replay and post-mortem), Task 4 (in-memory source refused).
- **§6 Menu and configuration:** Task 10 (Edit menu, Alt+E, `$EDITOR` with VISUAL/EDITOR/platform fallback, shlex on POSIX, reload + remap on mtime change), Task 11 (labels, CLI help), Task 12 (README). Deviation, deliberate: Revert to Disk does not show the Save/Discard/Cancel modal (offering "Save" before reverting is contradictory); it replaces the buffer in place and is undoable with the editor's undo.
- **§7 Syntax highlighting extra:** Task 12 (`edit` extra), Task 3 (`editor_language_for`).
- **§8 Cross-platform:** Task 2 (`os.replace`, Windows editor branch), Task 5 (Ctrl+Right/Left aliases).
- **§9 Testing:** every task carries its tests; Task 12 runs the full suite and lint.

**Placeholder scan:** no TBD/TODO. One spot tells the implementer to confirm a name before use (the `argparse` factory in Task 11) with an explicit fallback.

**Type consistency:** `CodeEditor.LeaveRequested(discard)`, `SaveRequested`, `SubmodeChanged`, `SearchRequested(backward)`, `SearchStepRequested(forward)` are used with those names in Tasks 3-7 and handled in Task 4. `CodeView.FileSaved(path, old_lines, new_lines)` is produced in Tasks 4 and 10 and consumed in Task 8. `controller.replace_breakpoints(source_path, bps)` is defined in Task 8 and stubbed with the same signature in Tasks 8 and 10 tests. `_confirm_discard_edits()` is defined in Task 9 and used in Task 10.

# Vim Edit Mode on prompt_toolkit — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Code View's hand-written vim-lite key layer with a headless prompt_toolkit vi engine, plus the tdb-owned gap fixes, marks, Python-`re` search and an ex line with `:s`/`:g`/`:v`.

**Architecture:** A new package `src/tdb/vi/` owns all vi logic. `engine.py` and `bindings.py` are the only files importing prompt_toolkit; `marks`, `search`, `ex`, `substitute`, `global_cmd`, `textedit` are pure Python. `ViLayer` in `src/tdb/widgets/code_editor.py` translates Textual keys to `ViKey`, feeds `ViEngine`, and mirrors text/cursor/selection into the existing `CodeEditor` (TextArea) with one diff-sized edit per key.

**Tech Stack:** Python 3.11+, Textual ≥ 8, prompt_toolkit 3.0.x, pytest + pytest-asyncio (auto mode), nvim (developer machine only, to generate golden fixtures).

**Spec:** `docs/superpowers/specs/2026-10-30-vim-edit-mode-prompt-toolkit-design.md` — read it first; this plan argues from it.

## Global Constraints

- Dependency: `prompt_toolkit>=3.0.52,<3.1` in core `[project] dependencies` of `pyproject.toml`.
- Only `src/tdb/vi/engine.py` and `src/tdb/vi/bindings.py` may import `prompt_toolkit`. `src/tdb/widgets/*` must not.
- The engine always uses `DummyInput()` / `DummyOutput()`; never create a real prompt_toolkit input/output (Windows console safety).
- `app.timeoutlen = None` on the engine Application.
- tdb pane keys never reach the engine: `ctrl+c ctrl+o ctrl+e ctrl+v ctrl+s ctrl+b ctrl+q ctrl+p`, all `f1`–`f24`, and `alt+*` outside insert/replace mode.
- In insert/replace mode only: `alt+<x>` → `Esc` then `<x>`; `ctrl+right` → `Esc` then `f`; `ctrl+left` → `Esc` then `b` (Textual's rewrites of a fast `Esc f` / `Esc b`).
- Patterns everywhere are Python `re`; case-sensitive by default.
- Block visual is unreachable: `ctrl+v` fed directly to the engine is a no-op.
- `"+` / `"*` registers produce the notification `Clipboard registers are not supported` and change nothing.
- Every `ViEngine.feed` is exception-safe: restore pre-key text/cursor, reset the key processor to normal mode, emit a warning `Notify`.
- One `:s`, `:g` or `:v` = one undo step.
- Emacs and default (Notepad-style) schemes: no behavior change; their tests pass unmodified.
- Tests run with `uv run pytest` (full suite via `memcap-pytest`, see repo notes). Commit messages end with the session's Co-Authored-By / Claude-Session lines.

## Review Focus

1. **Trailing newline / phantom last row.** vim's `a\nb\n` has 2 lines; prompt_toolkit's Document has 3 rows. `G`, `dd` on the last line, `p` after the last line, `:$` must match vim, and saving must keep exactly the original final-newline state. → Task 1 test `test_trailing_newline_is_not_a_line`, plus golden cases `eol-*` in Task 2.
2. **Tabs and non-ASCII columns.** `24|`, `f`, `$`, cursor mirroring on lines containing tabs, `é`, CJK and emoji: engine and TextArea must agree on (row, codepoint-column). → Task 3 test `test_cursor_mirroring_with_tabs_and_wide_chars`.
3. **Empty buffer.** `dd`, `x`, `p`, `J`, `.`, `:g/x/d`, `:%s/a/b/`, `/x` on `""` must not raise or corrupt. → golden cases `empty-*` in Task 2; Task 12 test `test_global_on_empty_buffer`.
4. **Text changed outside the engine.** Edit ▸ Revert to Disk replaces the TextArea text directly; the engine must resync, and `u` must undo the revert. → Task 3 test `test_revert_to_disk_resyncs_engine`.
5. **Regex edge input.** `*` on a word next to regex metacharacters, `:s` replacement containing `\\`, `&` (literal in Python `re`), `\n`, and an invalid pattern `(` must behave predictably (literal `&`, newline split, error notification, buffer untouched). → Task 9 test `test_star_escapes_word`, Task 11 tests `test_substitute_backslash_and_ampersand`, `test_substitute_invalid_pattern`.

---

## File Map

| File | Responsibility |
|---|---|
| `src/tdb/vi/__init__.py` | Re-exports: `ViEngine, ViKey, ViSession, MarkTable, Notify, SaveRequest, LeaveRequest, Viewport` |
| `src/tdb/vi/textedit.py` | `TextEdit` + `diff_text()` (pure) — shared by mirroring and mark adjustment |
| `src/tdb/vi/engine.py` | `ViEngine`: headless ptk Application, feed, state, events, modes for `:`/`/`/confirm, undo grouping |
| `src/tdb/vi/bindings.py` | `load_tdb_vi_bindings(engine) -> KeyBindings`: every gap fix/addition from the spec table |
| `src/tdb/vi/marks.py` | `MarkTable` (pure) |
| `src/tdb/vi/search.py` | Python-`re` search (pure) |
| `src/tdb/vi/ex.py` | ex-line parsing, ranges, dispatch of non-`s`/`g` commands (pure, via `ExHost`) |
| `src/tdb/vi/substitute.py` | `:s` parsing + application (pure) |
| `src/tdb/vi/global_cmd.py` | `:g` / `:v` (pure, via `ExHost`) |
| `src/tdb/widgets/code_editor.py` | `ViLayer` replaces `VimLayer`; `CodeEditor` gains viewport adapter, paste/mouse hooks, `sync_layer_from_widget()` |
| `src/tdb/widgets/code_view.py` | owns `ViSession` + per-file `MarkTable`s; installs `ViLayer`; `mode_label()` new sub-modes; revert resync |
| `src/tdb/keybindings/__init__.py` | `_VIM_EDIT_HELP` rewritten |
| `docs/using/vim-edit-mode.md` (new), `docs/using/keybindings.md`, `mkdocs.yml` | user docs |
| `scripts/vi_golden.py` (new) | regenerates golden fixture with nvim (dev only) |
| `tests/fixtures/vi/cases.json`, `tests/fixtures/vi/golden.json` (new) | compat cases + nvim results |
| `tests/unit/test_vi_*.py` (new), `tests/unit/test_code_editor_vim.py` (rewritten) | tests |
| `.github/workflows/test.yml` | `windows-vi` job |

---

### Task 1: Engine core (`ViEngine`, `ViKey`, `TextEdit`)

**Files:**
- Modify: `pyproject.toml` (dependency)
- Create: `src/tdb/vi/__init__.py`, `src/tdb/vi/textedit.py`, `src/tdb/vi/engine.py`, `src/tdb/vi/bindings.py`
- Test: `tests/unit/test_vi_engine.py`, `tests/unit/test_vi_textedit.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class ViKey: name: str` — a single printable character (`"d"`, `"$"`, `" "`) or one of `escape enter backspace tab delete up down left right home end pageup pagedown` or `ctrl+<letter>`. `ViKey.parse(notation: str) -> list[ViKey]` accepts vim notation (`"ciw<Esc>"`, `<CR> <BS> <Tab> <C-r> <lt>`), used by tests and `:normal`.
  - `@dataclass(frozen=True) class TextEdit: start: int; old_end: int; new_text: str` (character offsets into the old text) and `diff_text(old: str, new: str) -> TextEdit | None` (common prefix/suffix; `None` when equal). `TextEdit.line_delta(old: str) -> tuple[int, int, int]` returns `(first_row, old_last_row, new_last_row)`.
  - Events: `@dataclass class Notify: text: str; severity: Literal["information","warning","error"] = "information"`, `@dataclass class SaveRequest: pass`, `@dataclass class LeaveRequest: discard: bool = False`.
  - `class Viewport(Protocol): def visible_rows(self) -> tuple[int, int]` (first, last inclusive) `; def scroll_to_top_row(self, row: int) -> None`.
  - `@dataclass class ViSession` — process-wide state shared by every engine: `vi_state` (a ptk `ViState`, typed as `object` in the public signature), `clipboard` (ptk `InMemoryClipboard`), `last_search: tuple[str, bool] | None` (pattern, backward), `last_substitute: SubstituteSpec | None` (Task 11), `ex_history: list[str]`, `search_history: list[str]`, `last_change: list[ViKey] | None` (Task 6). Construct with `ViSession()`.
  - `class ViEngine:`
    - `__init__(self, text: str, *, cursor: tuple[int, int] = (0, 0), session: ViSession | None = None, marks: MarkTable | None = None, viewport: Viewport | None = None, indent_unit: str = "    ", _extra_bindings=None)` — `marks` (the `MarkTable` of Task 8) is accepted and stored but unused until Task 8; annotate it as a `TYPE_CHECKING` forward reference. `ViSession.last_substitute` is likewise typed `"SubstituteSpec | None"` ahead of Task 11.
    - `feed(self, key: ViKey) -> None`; `feed_keys(self, notation: str) -> None` (parse + feed each)
    - read-only properties: `text: str` (full text including the original final newline state), `cursor: tuple[int, int]`, `mode: str` (one of `normal insert replace visual visual-line command search confirm`), `selection: tuple[tuple[int,int], tuple[int,int]] | None` (inclusive start/end, ordered), `has_pending: bool`, `status: str` (pending keys like `"a2d`, `recording @a`, the `:`/`/` line text, or the confirm prompt; `""` otherwise)
    - `drain_events(self) -> list[Notify | SaveRequest | LeaveRequest]`
    - `set_cursor(self, row: int, col: int) -> None` (leaves visual mode), `select(self, start, end) -> None` (enters `visual`), `insert_text(self, text: str) -> None` (one undo step, mode unchanged), `reset_text(self, text: str) -> None` (undoable replacement of the whole buffer)

- [ ] **Step 1: Write failing tests** in `tests/unit/test_vi_engine.py` (async tests so a loop is running):

```python
async def test_cw_changes_word():
    e = ViEngine("foo bar\n")
    e.feed_keys("cwX<Esc>")
    assert (e.text, e.cursor, e.mode) == ("X bar\n", (0, 0), "normal")


async def test_trailing_newline_is_not_a_line():
    e = ViEngine("a\nb\n")
    e.feed_keys("Gdd")
    assert e.text == "a\n" and e.cursor == (0, 0)
    e = ViEngine("a\nb")
    e.feed_keys("Gyyp")
    assert e.text == "a\nb\nb"  # no final newline added


async def test_pending_operator_and_status():
    e = ViEngine("abc\n")
    e.feed_keys("2d")
    assert e.has_pending and e.status == "2d" and e.text == "abc\n"
    e.feed_keys("<Esc>")
    assert not e.has_pending and e.status == ""


async def test_redo_ctrl_r():
    e = ViEngine("abc\n")
    e.feed_keys("xu<C-r>")
    assert e.text == "bc\n"


async def test_ctrl_v_is_noop():
    e = ViEngine("abc\nabc\n"); e.feed_keys("<C-v>")
    assert e.mode == "normal" and not e.has_pending
    e.feed_keys("jx"); assert e.text == "abc\nbc\n"

async def test_exception_restores_buffer():
    kb = boom_bindings("Z")  # helper: binding for "Z" that edits then raises
    e = ViEngine("abc\n", _extra_bindings=kb)
    e.feed_keys("Z")
    assert e.text == "abc\n" and e.mode == "normal"
    assert [type(x) for x in e.drain_events()] == [Notify]


async def test_insert_text_is_one_undo_step():
    e = ViEngine("ab\n")
    e.insert_text("XY\nZ")
    e.feed_keys("u")
    assert e.text == "ab\n"


async def test_reset_text_is_undoable():
    e = ViEngine("ab\n")
    e.reset_text("cd\n")
    e.feed_keys("u")
    assert e.text == "ab\n"
```

`tests/unit/test_vi_textedit.py`: `diff_text("abc","abc") is None`; `diff_text("abcd","aXd") == TextEdit(1, 3, "X")`; `diff_text("a\nb\nc","a\nc").line_delta("a\nb\nc") == (1, 2, 1)`.

- [ ] **Step 2: Run** `uv run pytest tests/unit/test_vi_engine.py tests/unit/test_vi_textedit.py -v` — FAIL (`ModuleNotFoundError: tdb.vi`).
- [ ] **Step 3: Implement.** Add the dependency. `ViEngine` builds the Application as the spec's "headless engine" section describes (Buffer with no completer, layout `Window(BufferControl(buffer))`, `editing_mode=EditingMode.VI`, `key_bindings=load_tdb_vi_bindings(self)` merged with `_extra_bindings`, `app.vi_state = session.vi_state`, `app.clipboard = session.clipboard`, start in `InputMode.NAVIGATION`). Store text **without** its final `"\n"` plus an `_eol: bool`; `text` re-appends it. `bindings.py` in this task contains only Ctrl+R redo (copy pyvim's `key_bindings.py` redo with its BSD notice) and the `c-v` no-op override (bind `c-v` in navigation and selection modes to a no-op). `mode` maps ptk `InputMode` + `selection_state.type`; `has_pending` is true when the key processor holds unprocessed keys, `vi_state.operator_func` is set, an arg/count is pending, a register prefix is pending, or mode ≠ `normal`. Exception safety per Global Constraints (`key_processor.reset()`, input mode NAVIGATION, document restored).
- [ ] **Step 4: Run** the same command — PASS.
- [ ] **Step 5: Commit** `feat(vi): headless prompt_toolkit vi engine core`.

---

### Task 2: Golden compatibility harness

**Files:**
- Create: `scripts/vi_golden.py`, `tests/fixtures/vi/cases.json`, `tests/fixtures/vi/golden.json`, `tests/unit/test_vi_compat.py`

**Interfaces:**
- Consumes: `ViEngine`, `ViKey.parse` (Task 1).
- Produces: case format later tasks append to: `{"id": str, "text": str, "cursor": [row, col], "keys": str (vim notation), "difference": str | absent}`. Golden format: `{"<id>": {"text": str, "cursor": [row, col]}}`.

- [ ] **Step 1: Write `scripts/vi_golden.py`** — `python scripts/vi_golden.py [--only ID ...]` reads `cases.json`, and for each case runs `nvim --headless -u NONE -i NONE -n` on a temp file with `:mapclear | mapclear! | set sw=4 sts=4 et ai nojoinspaces nrformats=bin,hex nostartofline`, `call cursor(row+1, col+1)`, `call feedkeys(<keys translated to nvim termcodes>, 'xt')`, then writes `{"text": join(getline(1,'$'), "\n") + (eol ? "\n" : ""), "cursor": [line('.')-1, col('.')-1]}` via `writefile(json_encode(...))`; honors `&eol`/`fixeol` so the final-newline state is reported faithfully. Writes `golden.json` sorted by id. Exit non-zero if nvim is missing.
- [ ] **Step 2: Seed `cases.json`** with the spec's "Already verified" list (one case each: `cw`, `{`, `}`, `f`, `F`, `t`, `T`, `;`, `,`, `24|`, `dt=`, `ct)`, `%`, `w W b B e ge`, `ci"`, `yiw`, `di(`, `r`, `R`, `s`, `S`, `cc`, `C`, `D`, `g~w`, `gUiw`, `~`, `>>`, `Vj>`, `dd`, `2dd`, `d2j`, `yyp`, `yyP`, `u`, `qaxq@a`, `o O i a I A`) plus Review-Focus cases `eol-G-dd`, `eol-p-last`, `noeol-yyp`, `empty-dd`, `empty-x`, `empty-p`, `empty-J`, `empty-dot`. Use the Python-ish sample text from the spec audit (`def f(a, b):\n    x = "hi there"\n    return (a + b) * 2\n\ny = 10\nz = 20\n`) for most cases.
- [ ] **Step 3: Run** `python scripts/vi_golden.py` and commit the resulting `golden.json`.
- [ ] **Step 4: Write `tests/unit/test_vi_compat.py`**: parametrize over `cases.json`; skip (with reason) cases that have a `"difference"` key; otherwise assert `(engine.text, engine.cursor) == golden[id]`. A second test asserts every case id has a golden entry (stale-fixture guard).
- [ ] **Step 5: Run** `uv run pytest tests/unit/test_vi_compat.py -v`. Expected: all seeded "already verified" cases PASS. If any fail, that is a real gap — fix it in `bindings.py` within this task, or move it (with the failing id) to the task in this plan that owns that area, marking it `"difference": "TODO Task N"` temporarily; Task 13 asserts no `TODO` differences remain.
- [ ] **Step 6: Commit** `test(vi): nvim golden compatibility harness`.

From here on, **every behavior task adds its cases to `cases.json`, regenerates `golden.json` for those ids (`--only`), and the compat test is part of its verification.**

---

### Task 3: `ViLayer` replaces `VimLayer` (routing, Esc, mirroring, label, resync)

**Files:**
- Modify: `src/tdb/widgets/code_editor.py` (delete `VimLayer`, add `ViLayer`, add `CodeEditor.sync_layer_from_widget()`)
- Modify: `src/tdb/widgets/code_view.py:704-713` (`_install_key_layer`), `:642-655` (`mode_label`), `:740-757` (`revert_to_disk`), `__init__` (session + marks dict)
- Test: `tests/unit/test_code_editor_vim.py` (rewrite), `tests/unit/test_app_edit_mode.py` (add)

**Interfaces:**
- Consumes: `ViEngine`, `ViKey`, `ViSession`, events, `diff_text` (Task 1); `MarkTable` is introduced in Task 8 — until then pass `marks=None`.
- Produces:
  - `class ViLayer` (satisfies `KeyLayer`): `__init__(self, editor: CodeEditor, *, session: ViSession, marks: MarkTable | None = None)`; `handle_key(event: Key) -> bool`; `submode -> str` (= `engine.mode`); `command_text -> str` (= `engine.status`); `handle_paste(text: str) -> bool`; `on_mouse_selection(start: tuple[int,int], end: tuple[int,int]) -> None`; `resync_from_widget() -> None`; `engine` attribute.
  - `CodeEditor.sync_layer_from_widget() -> None` — calls `self._layer.resync_from_widget()` when the layer has it.
  - `CodeView._vi_session: ViSession` and `CodeView._vi_marks: dict[str, MarkTable]` (populated in Task 8).
  - `mode_label()` strings: `Edit`, `Edit:INSERT`, `Edit:REPLACE`, `Edit:VISUAL`, `Edit:V-LINE`, `Edit :<text>` (command), `Edit /<text>` or `Edit ?<text>` (search; the engine's `status` already includes the `/`/`?` — do not double it), `Edit <status>` for confirm or a non-empty normal-mode status; trailing `*` when dirty.

- [ ] **Step 1: Rewrite `tests/unit/test_code_editor_vim.py`.** Keep the `_EdApp` harness but install `ViLayer(ed, session=ViSession())`. Keep, adapted, the old tests whose behavior is still vim-correct: `test_starts_in_normal_mode_and_swallows_letters` (keys `"q","z","enter"` — note `q z` now starts recording into `z`: change the keys to `"Z","enter"` and assert text unchanged), `test_hjkl_with_counts_and_arrows`, `test_line_motions`, `test_word_motions`, `test_gg_G_and_count_G`, `test_insert_entry_points_and_escape_back_to_normal`, `test_escape_in_normal_mode_requests_leave`, `test_command_line_w_q_wq_qbang_and_line_number` (Task 10 owns ex; in this task keep the five commands working by routing them through a minimal stub in the engine's `command` mode that Task 10 replaces), `test_unknown_command_notifies`, `test_x_and_X_with_count`, `test_dd_yy_p_P_linewise_with_counts`, `test_dd_on_last_line_removes_preceding_newline`, `test_dw_D_and_J`, `test_undo_redo`, `test_charwise_put_after_dw`, `test_linewise_put_after_on_phantom_last_row`, `test_ctrl_p_not_claimed_from_app_on_vim_scheme`, `test_ctrl_s_in_normal_mode_saves_instead_of_being_swallowed`, `test_ctrl_q_in_normal_mode_bubbles_to_app`, `test_ctrl_q_in_command_mode_bubbles_to_app`, `test_charwise_put_places_cursor_on_pasted_text`. Drop `test_search_keys_post_messages` (search is inline now — Task 9) and `test_page_keys` (Task 4 replaces it); list both in the commit message. Add:

```python
async def test_pane_keys_bubble_in_every_mode():   # ctrl+o/e/v/b/c in normal, insert, visual, command
async def test_escape_cancels_pending_then_leaves():  # "d", Esc -> no leave; Esc -> leave
async def test_escape_in_insert_returns_to_normal_without_leave():
async def test_alt_letter_in_insert_is_escape_then_letter():  # "i","x","alt+j" -> mode normal, cursor moved down
async def test_alt_letter_in_normal_bubbles():  # alt+e reaches app (menu) unchanged
async def test_ctrl_right_in_insert_is_escape_f():
async def test_mirroring_keeps_textarea_in_sync():  # after "ciwZ<Esc>" ed.text == layer.engine.text and ed.cursor_location == engine.cursor
async def test_visual_selection_is_mirrored():  # "vl" -> ed.selection covers 2 chars; "V" -> whole line
async def test_cursor_mirroring_with_tabs_and_wide_chars():  # text "\té漢😀x\n", keys "f" "x" -> ed.cursor_location == (0, 4)
async def test_engine_exception_shows_warning_and_keeps_text():
async def test_typing_latency_on_large_file():  # 5000-line buffer; 300 keys via ViLayer.handle_key (fake Key events, no Pilot) incl. 'ciw' edits: total < 1.0 s
```

In `tests/unit/test_app_edit_mode.py` add `test_mode_label_vi_submodes` (drive through CodeView: insert → `Edit:INSERT`, `v` → `Edit:VISUAL`, `V` → `Edit:V-LINE`, `"2d"` → `Edit 2d`, `:wq` typed → `Edit :wq`, dirty `*` suffix) and `test_revert_to_disk_resyncs_engine` (edit, Revert to Disk, `editor._layer.engine.text` equals disk text, then `u` restores the edited text).

- [ ] **Step 2: Run** `uv run pytest tests/unit/test_code_editor_vim.py tests/unit/test_app_edit_mode.py -v` — new tests FAIL.
- [ ] **Step 3: Implement `ViLayer`.** Key translation per Global Constraints (use `event.character` when printable, else the name table; unmapped keys return True = swallowed). After each feed: `diff_text(old, new)` → single `editor.replace(edit.new_text, start_loc, end_loc)` (convert offsets with `editor.document`), set `cursor_location`, set `selection` (line visual = `(row,0)`..`(row_end, len(line))`), drain events (`SaveRequest` → `CodeEditor.SaveRequested`, `LeaveRequest` → `LeaveRequested(discard)`, `Notify` → `editor.app.notify(text, title="Edit", severity=...)`), post `SubmodeChanged` when `(mode, status)` changed. Esc: when `engine.mode == "normal" and not engine.has_pending`, return False so `CodeEditor._on_key` posts `LeaveRequested` (existing path). Engine input text is `"\n".join(editor.document.lines)` (+ final newline state) — never raw `editor.text` with `\r`. `CodeView`: create `self._vi_session = ViSession()` and `self._vi_marks = {}` in `__init__`; install `ViLayer`; `revert_to_disk` calls `self._editor.sync_layer_from_widget()` after its `replace`; `resync_from_widget` uses `engine.reset_text`.
- [ ] **Step 4: Run** the Step 2 command plus `uv run pytest tests/unit/test_code_editor.py tests/unit/test_code_editor_emacs.py tests/unit/test_keybindings.py -v` — PASS.
- [ ] **Step 5: Commit** `feat(edit): vim scheme drives the prompt_toolkit engine` (message lists the two dropped tests and why).

---

### Task 4: Viewport commands, mouse, paste, insert Tab

**Files:**
- Modify: `src/tdb/vi/bindings.py`, `src/tdb/widgets/code_editor.py`
- Test: `tests/unit/test_vi_engine.py`, `tests/unit/test_code_editor_vim.py`

**Interfaces:**
- Consumes: `Viewport` (Task 1), `ViLayer.handle_paste/on_mouse_selection` (Task 3).
- Produces: `CodeEditor` implements `Viewport` (`visible_rows` from `scroll_offset.y` and `scrollable_content_region.height`; `scroll_to_top_row` via `scroll_to(y=row, animate=False)`); `CodeEditor._on_paste` and mouse-up hook delegate to the layer when it has `handle_paste` / `on_mouse_selection`.

- [ ] **Step 1: Failing tests.** Engine tests with a fake viewport (`visible_rows` returns `(top, top+19)`, records `scroll_to_top_row` calls), 100-line buffer: `Ctrl+D` moves cursor and top by 10; `Ctrl+U` back; `Ctrl+F` moves top by 18 (vim: page minus 2); `Ctrl+Y` scrolls top up 1 without moving the cursor unless it would leave the view; `H`/`M`/`L` land on top/middle/bottom first non-blank, `3H` = top+2, `dL` deletes cursor..bottom linewise; `zz`/`zt`/`zb` set top so cursor row is centered/top/bottom; `z<CR>`, `z.`, `z-` same plus first non-blank. Insert Tab: `ViEngine("x\n", indent_unit="    ")` + `"i<Tab><Esc>"` → `"    x\n"`; with `indent_unit="\t"` → `"\tx\n"`. Integration tests: `test_click_moves_cursor_and_leaves_visual`, `test_drag_starts_visual`, `test_paste_is_one_undo_step_any_mode` (paste in normal mode inserts at cursor, mode stays normal, `u` removes it all), `test_pageup_pagedown_keys`.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement** the viewport bindings in `bindings.py` as text objects via `create_text_object_decorator` so they work with operators (H/M/L linewise) and as plain scroll commands for Ctrl-D/U/F/Y and z-family. `CodeEditor` passes `indent_unit` from its indent settings (`"\t"` when `indent_type == "tabs"`, else `" " * indent_width`) and itself as `viewport`.
- [ ] **Step 4: Run** — PASS (plus compat test).
- [ ] **Step 5: Commit** `feat(vi): viewport commands, mouse, paste, insert Tab`.

---

### Task 5: Text-object and join fixes

**Files:** Modify `src/tdb/vi/bindings.py`; add cases to `tests/fixtures/vi/cases.json`, regenerate golden; test `tests/unit/test_vi_compat.py`.

**Interfaces:** Consumes `create_text_object_decorator` (ptk) inside `bindings.py`; produces no new API.

- [ ] **Step 1: Add golden cases** (ids prefixed `tobj-` / `join-`): `ci(`, `di{`, `ci[`, `ci<`, `ci"`, `ci'`, ``ci` `` with **no enclosing pair** (expect text unchanged — vim beeps); `ci(` cursor *on* `(` and on `)`; `diw`/`daw`/`ciw` at column 0 of an indented line, inside a whitespace run, at end of line; `dap`/`dip`/`yap` on first, middle, last paragraph and on a blank line; `3J`, `J` onto a blank line, `J` when next line starts with `)`, `gJ`, `3gJ`. Regenerate with `--only`.
- [ ] **Step 2: Run** compat test — new cases FAIL.
- [ ] **Step 3: Implement** overriding bindings: bracket/quote text objects return "no object" so the pending operator is cancelled without touching the buffer (not `TextObject(0)`); vim-accurate `iw`/`aw` whitespace-run handling; `ip`/`ap`; `J`/`gJ` count semantics (count N joins N lines, minimum 2).
- [ ] **Step 4: Run** compat test — PASS.
- [ ] **Step 5: Commit** `fix(vi): text objects and J match vim`.

---

### Task 6: Registers, `.` repeat, macros

**Files:** Modify `src/tdb/vi/bindings.py`, `src/tdb/vi/engine.py`; cases + golden; tests `tests/unit/test_vi_compat.py`, `tests/unit/test_vi_engine.py`.

**Interfaces:**
- Consumes: `ViSession.last_change` (Task 1).
- Produces: `ViEngine.is_replaying: bool` (true while `.` or a macro replays; Task 8's jump-mark logic and Task 12's `:normal` rely on it).

- [ ] **Step 1: Golden cases** (`reg-`, `dot-`, `mac-`): `"ayyj"ap`, `"add"ap`, `"acc` + text, `"Ayy` append after `"ayy`, `"adw"aP`, `"ad2j"ap`, `"_dd` then `p` pastes the previous unnamed text, `yyjdd"0p`, `3"ayy"ap`; `.` after `dw`, `x`, `3x` then `.` and `2.`, `ciwfoo<Esc>w.`, `A;<Esc>j.`, `>>j.`, `rXl.`, `Jj.`, `p` then `.`, `oline<Esc>.`; `qaxjq@a@@`, `qaxq3@a`, `@a` inside a recording. Engine test: `test_clipboard_registers_rejected` — `"+yy` and `"*p` emit `Notify("Clipboard registers are not supported")` and change nothing. Engine test: `test_registers_shared_across_engines` — yank in one engine with a shared `ViSession`, `p` in a new engine with that session.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.** Register prefix (`"x`) with `a`–`z`, `A`–`Z` (append, linewise-aware), `"`, `0` (set on every yank), `_` (black hole), working with every operator and `yy dd cc p P x X D C s S`. `.`: record the key sequence from the start of a change in normal mode (no pending keys) until the engine is back in normal mode with nothing pending; keep it as `session.last_change` only when the text changed and the first key is not `u`, `Ctrl+R`, `.`, or `@`; a count typed before `.` replaces the original count. `@@` repeats the last executed macro; `N@a` repeats N times.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(vi): registers, dot repeat, @@ and macro counts`.

---

### Task 7: Visual additions and `Ctrl+A` / `Ctrl+X`

**Files:** Modify `src/tdb/vi/bindings.py`; cases + golden.

**Interfaces:** Produces `ViEngine.last_visual: tuple[tuple[int,int], tuple[int,int], bool] | None` (start, end, linewise) — consumed by Task 8 (`'<`/`'>`) and Task 10 (`:'<,'>`).

- [ ] **Step 1: Golden cases** (`vis-`, `num-`): `vlyjgvd`, `gv` after `V` selection, `vjU`, `vju`, `vj~`, `vjJ`, `vjo` then `h`, `Vj>`, `Vj<`, `vjrX`; `Ctrl+A`/`Ctrl+X` on `y = 10` (cursor before the number), `5<C-a>`, on `-3`, on `0x0f`, on `007` (decimal with `nrformats=bin,hex` → `008`), on a line with no number (no change), `.` after `3<C-a>`.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement** missing visual commands and number increment (search forward from cursor on the line for the first number; keep hex case and width).
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(vi): gv, visual case ops, Ctrl+A/Ctrl+X`.

---

### Task 8: Marks

**Files:**
- Create: `src/tdb/vi/marks.py`
- Modify: `src/tdb/vi/bindings.py`, `src/tdb/vi/engine.py`, `src/tdb/widgets/code_view.py` (`_install_key_layer` passes `self._vi_marks.setdefault(key, MarkTable())`, key = `str(Path(source_path).resolve())`)
- Test: `tests/unit/test_vi_marks.py`, cases + golden, `tests/unit/test_app_edit_mode.py`

**Interfaces:**
- Consumes: `TextEdit.line_delta` (Task 1), `ViEngine.last_visual`, `is_replaying` (Tasks 6–7).
- Produces: `class MarkTable`: `set(name: str, pos: tuple[int,int]) -> None`, `get(name: str) -> tuple[int,int] | None`, `adjust(first_row: int, old_last_row: int, new_last_row: int) -> None`, `names() -> list[str]`. Names: `a`–`z`, `'` (previous-jump; `` ` `` is an alias for the same slot), `<`, `>`.

- [ ] **Step 1: Failing tests.** `test_vi_marks.py`: inserting 2 lines above a mark shifts it down 2; deleting lines above shifts up; deleting the mark's own line removes it; edit within the mark's line keeps row and clamps column; `<`/`>` adjust too. Golden cases (`mark-`): `majjd'a`, ``majjlld`a``, `majjy'aP`, `mak'a`, ``ma3jx`a``, `majj''`, ``` majj`` ```, `ggOnew<Esc>j'a` (mark moved), `ma` then `dd` on that line then `'a` (no-op; vim errors E20), `G` then `''` returns. Engine test: `'z` with no mark emits `Notify("Mark not set: z", "warning")`. App test `test_marks_survive_leave_and_file_switch`: set `ma` in file A, leave Edit mode, open file B, back to A, Edit mode, `'a` lands on the mark; file B has no mark `a`.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.** `m{a-z}`; `'x` (linewise, first non-blank) and `` `x `` (charwise exclusive) via `create_text_object_decorator` so they work as motions and operator targets; the engine records `'` before every jump command (`G gg NG % ( ) { } H M L n N * # / ? 'x `x` and `:N`) unless `is_replaying`; the engine calls `marks.adjust(*edit.line_delta(old))` after every text change (including `reset_text`); `'<`/`'>` updated when visual mode ends.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(vi): marks a-z, '' and '< '> per file for the tdb run`.

---

### Task 9: Python-`re` search (`/ ? n N * #`)

**Files:**
- Create: `src/tdb/vi/search.py`
- Modify: `src/tdb/vi/engine.py` (search line mode), `src/tdb/vi/bindings.py`
- Test: `tests/unit/test_vi_search.py`, `tests/unit/test_vi_engine.py`, cases + golden (only patterns whose vim and Python syntax agree)

**Interfaces:**
- Produces: `class ViPatternError(ValueError)`; `compile_pattern(pattern: str) -> re.Pattern` (raises `ViPatternError` with the `re` message); `find(text: str, pattern: re.Pattern, offset: int, backward: bool) -> tuple[int, int, bool] | None` returning `(start, end, wrapped)`, matching vim semantics (forward search starts after `offset`, backward before it, wraps once); `word_pattern(word: str) -> str` = `r"\b" + re.escape(word) + r"\b"`.

- [ ] **Step 1: Failing tests.** `test_vi_search.py`: forward/backward, wrap flag, no match → `None`, zero-width match handling, invalid `(` → `ViPatternError`; `test_star_escapes_word` (`word_pattern("a.b")` matches only literal `a.b`). Engine: `test_incremental_search_moves_cursor` (`/re` → cursor on first `re` while mode is `search`, `status == "/re"`; `<Esc>` restores the original cursor; `<CR>` keeps it); `test_search_history_up_down`; `test_search_not_found_notifies` (`Pattern not found: zzz`); `test_search_wrap_notifies` (`search hit BOTTOM, continuing at TOP`); `test_invalid_regex_notifies_error`; `test_search_is_operator_target` (`d/x<CR>` deletes up to `x`, exclusive); `test_n_after_question_mark_goes_backward`; `test_star_and_hash`; `test_last_search_shared_via_session`. Backspace on an empty search line cancels.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.** The engine owns a tdb search line (mode `search`), not ptk's SearchToolbar; override ptk's `/ ? n N * #` and the `cgn`-style text-object paths that read `app.current_search_state`. Store `session.last_search = (pattern, backward)` on `<CR>`.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(vi): inline Python-re search`.

---

### Task 10: Ex command line, ranges, basic commands

**Files:**
- Create: `src/tdb/vi/ex.py`
- Modify: `src/tdb/vi/engine.py` (replace Task 3's stub; implement `ExHost`)
- Test: `tests/unit/test_vi_ex.py`, `tests/unit/test_vi_engine.py`

**Interfaces:**
- Consumes: `MarkTable` (Task 8), `last_visual` (Task 7), `session.ex_history`.
- Produces:
  - `class ExError(Exception)` (message shown as an error `Notify`).
  - `@dataclass class ExRange: start: str | None; end: str | None` (raw address text) and `resolve_range(rng: ExRange | None, host: ExHost, default: Literal["current","all"]) -> tuple[int, int]` (0-based inclusive rows). Addresses: `N`, `.`, `$`, `%`, `.+N`, `.-N`, `$-N`, `'a`–`'z`, `'<`, `'>`.
  - `@dataclass class ExCommand: range: ExRange | None; name: str; bang: bool; args: str` and `parse_ex(line: str) -> ExCommand`.
  - `class ExHost(Protocol)`: `lines() -> list[str]`; `cursor_row() -> int`; `mark(name: str) -> tuple[int,int] | None`; `replace_lines(lines: list[str], cursor: tuple[int,int]) -> None`; `set_register(name: str | None, text: str, linewise: bool) -> None`; `run_normal(row: int, keys: str) -> None`; `last_search() -> str | None`; `set_last_search(pattern: str) -> None`; `save() -> None`; `leave(discard: bool) -> None`; `notify(text: str, severity: str = "information") -> None`; `goto_line(row: int) -> None`; `undo_group() -> ContextManager[None]`.
  - `run_ex(line: str, host: ExHost) -> None` — dispatches `w q q! wq x N d y m t co normal norm` here; `s`, `&`, `g`, `g!`, `v` dispatch to Tasks 11–12 (until then they raise `ExError("Not an editor command: …")`).

- [ ] **Step 1: Failing tests.** `test_vi_ex.py` with a fake `ExHost`: `parse_ex` for `"10,20d"`, `"'a,'bm0"`, `"%normal Ax"`, `".,$-1y a"`, `"q!"`, `"  wq  "`; `resolve_range` for every address form, reversed range (`20,10` → swapped as vim asks; just swap silently), out-of-range → `ExError`, unknown mark → `ExError("Mark not set: a")`; command behavior: `:3,4d` (deleted lines go to unnamed register), `:2y a`, `:1,2m$`, `:1t0`, `:co$`, `:%normal A;`, `:5` (goto), `:w`/`:q`/`:q!`/`:wq`/`:x` call `save`/`leave`. Engine tests: `:` in visual mode pre-fills `'<,'>`; history Up/Down; Backspace on empty line cancels; `:%normal` is one undo step.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement** parser, ranges, dispatcher; engine command-line mode and `ExHost` adapter (`undo_group` collapses the ptk buffer's undo stack to one entry for the group and clears redo; `run_normal` sets the cursor to the row and feeds `ViKey.parse(keys)` then forces normal mode, with `is_replaying` true).
- [ ] **Step 4: Run** — PASS, and `tests/unit/test_code_editor_vim.py::test_command_line_w_q_wq_qbang_and_line_number` still PASS.
- [ ] **Step 5: Commit** `feat(vi): ex command line with ranges, d y m t normal`.

---

### Task 11: `:s` substitute

**Files:**
- Create: `src/tdb/vi/substitute.py`
- Modify: `src/tdb/vi/ex.py`, `src/tdb/vi/engine.py` (confirm mode), `src/tdb/vi/bindings.py` (normal-mode `&`)
- Test: `tests/unit/test_vi_substitute.py`, `tests/unit/test_vi_engine.py`

**Interfaces:**
- Consumes: `ExHost`, `resolve_range`, `compile_pattern` (Task 9).
- Produces:
  - `@dataclass class SubstituteSpec: pattern: str; replacement: str; flags: str` and `parse_substitute(args: str) -> SubstituteSpec` (any non-alphanumeric, non-space delimiter; missing trailing delimiter allowed; empty pattern → last search; no args → repeat last spec).
  - `apply_substitute(lines: list[str], first: int, last: int, spec: SubstituteSpec) -> tuple[list[str], int, int, int]` returning `(new_lines, n_substitutions, n_lines_changed, last_changed_row)`; replacement via `pattern.sub`-compatible template (Python `\1`, `\g<name>`); `\n` in the replacement (backslash-n as typed) splits the line.
  - `iter_matches(lines, first, last, spec) -> Iterator[tuple[int, int, int]]` (row, start, end) for the confirm flow.

- [ ] **Step 1: Failing tests.** `test_vi_substitute.py`: `s/a/b/` first only; `g`; `i`; delimiter `#`; `s/(\w+) (\w+)/\2 \1/`; `test_substitute_backslash_and_ampersand` (`&` stays literal, `\\` yields one backslash); `\n` splits; empty pattern uses last search; `parse_substitute("")` repeats; `test_substitute_invalid_pattern` (`ViPatternError`, lines untouched). Engine: `:%s/x/y/g` reports `Notify("3 substitutions on 2 lines")`, is one undo step, cursor on last changed line first non-blank; no match → `Notify("Pattern not found: x", "warning")`; `:s` then `&` on another line repeats; `:%s/x/y/c` confirm flow — `y` replaces, `n` skips, `a` replaces rest, `l` replaces this and stops, `q`/`<Esc>` stops, mode is `confirm` and `status` is `replace with y (y/n/a/q/l)?`, whole confirm session is one undo step.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement** parse/apply; wire `s`, `&` into `run_ex`; confirm mode in the engine; `session.last_substitute`.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(vi): :s with g/i/c flags in Python re syntax`.

---

### Task 12: `:g` / `:v`

**Files:**
- Create: `src/tdb/vi/global_cmd.py`
- Modify: `src/tdb/vi/ex.py`
- Test: `tests/unit/test_vi_global.py`, `tests/unit/test_vi_engine.py`

**Interfaces:**
- Consumes: `ExHost`, `parse_ex`, `run_ex`, `apply_substitute` (Tasks 10–11).
- Produces: `run_global(cmd: ExCommand, host: ExHost, invert: bool) -> None`.

- [ ] **Step 1: Failing tests.** `:g/x/d`; `:v/x/d`; `:g!/x/d` = `:v`; `:g/x/s/a/b/g`; `:g/def/normal A:`; `:g/x/m0` (reverses matching lines, as vim); `:g/x/t$`; `:g/x/y a` (register `a` cleared then appended, linewise); default range `%`, explicit `:1,3g/x/d`; empty command → `ExError` (`:g` print is unsupported); a whole `:g` is one undo step; `test_global_on_empty_buffer`; `test_global_marks_lines_first` (`:g/x/d` with adjacent matching lines deletes all of them); performance: 5000-line buffer, `:g/7/s/1/2/` completes in < 1.0 s (`time.perf_counter`).
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement** two-pass mark-then-run (track marked rows through edits with `MarkTable`-style adjustment or identity of line objects), inside `host.undo_group()`; mirror once at the end (the engine emits one text change).
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(vi): :g and :v`.

---

### Task 13: Docs, help table, Windows CI, version check, final sweep

**Files:**
- Create: `docs/using/vim-edit-mode.md`
- Modify: `mkdocs.yml` (nav entry after `using/keybindings.md`: `- Vim Edit Mode: using/vim-edit-mode.md`), `docs/using/keybindings.md` (vim row → "full vi via prompt_toolkit — see Vim Edit Mode"), `src/tdb/keybindings/__init__.py` (`_VIM_EDIT_HELP`), `src/tdb/cli.py` only if any help text says "vim-lite", `.github/workflows/test.yml`
- Test: `tests/unit/test_keybindings.py`, `tests/unit/test_vi_compat.py`, documentation test (whichever existing test covers `tdb --doc` nav stitching)

- [ ] **Step 1: Failing tests.** `test_vim_edit_help_mentions_core_features` (help rows include `ci( ciw dap`-style text objects, `. ` repeat, `ma 'a`, `:s :g :v`, `/ ? n N * #`); `test_vi_compat_has_no_todo_differences` (no case's `difference` starts with `TODO`); doc stitching test includes `using/vim-edit-mode.md`.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Write docs.** `vim-edit-mode.md` sections: Supported (motions, operators, text objects, counts, visual char/line, registers `"a–z A–Z " 0 _`, `.`, macros, marks, search, ex commands with range syntax), **Differences from vim** (Python `re` with translation table `\(…\)`→`(…)`, `\<w\>`→`\bw\b`, `\{n}`→`{n}`, `~` unsupported, `(?i)` for ignore-case, `&` is literal in replacements; `Ctrl+V` `Ctrl+O` `Ctrl+E` `Ctrl+B` are tdb pane keys — use PageUp for page up; no block visual; marks lowercase only and kept per file for the tdb run; no clipboard registers; no `:set`; Alt+letter menu shortcuts need Esc first in insert mode; any discrepancy the golden harness found and was not fixed, e.g. insert-mode `Ctrl+W` on punctuation). Rewrite `_VIM_EDIT_HELP` to match. CI: add job `windows-vi` on `windows-latest`: checkout, `astral-sh/setup-uv`, `uv run pytest tests/unit/test_vi_engine.py tests/unit/test_vi_textedit.py tests/unit/test_vi_compat.py tests/unit/test_vi_marks.py tests/unit/test_vi_search.py tests/unit/test_vi_ex.py tests/unit/test_vi_substitute.py tests/unit/test_vi_global.py tests/unit/test_code_editor_vim.py`.
- [ ] **Step 4: Version check.** Run the vi test files under `uv run --with 'prompt_toolkit==3.0.52'` and `uv run --with 'prompt_toolkit<3.1' --upgrade-package prompt_toolkit`; both must pass, else raise the floor in `pyproject.toml` to the oldest passing version and say so in the commit.
- [ ] **Step 5: Full suite.** `memcap-pytest` (wraps `uv run pytest`). Expected: no new failures vs `main` (verify any "pre-existing" failure in a `main` worktree).
- [ ] **Step 6: Commit** `docs(vi): vim Edit mode docs, help table, Windows CI job`.

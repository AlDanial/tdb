# Vim Edit Mode on prompt_toolkit — Design

**Date:** 2026-10-30
**Branch:** `vim-prompt-toolkit` (worktree `.claude/worktrees/vim-prompt-toolkit`, from `main` a9d7b89)
**Status:** Draft, reviewed; awaiting user approval
**Builds on:** `2026-09-16-code-view-edit-mode-design.md` (Edit mode itself)

## Why

Code View's Edit mode has a vim scheme, but it is "vim-lite": a
hand-written state machine (`VimLayer` in `src/tdb/widgets/code_editor.py`)
with a handful of motions and edits. A vim user's muscle memory keeps
hitting walls. The commands that prompted this work were:

- `cw` (change word)
- `{` / `}` (paragraph motion)
- `fx` (find char on line)
- `24|` (go to column)
- `dt=` (delete up to `=`)

None of them work today. Rather than keep growing a home-grown vi, tdb
will drive **prompt_toolkit's vi engine** (`prompt_toolkit.key_binding.bindings.vi`,
~2,200 lines, maintained) headlessly behind the existing editor pane.

pyvim (github.com/prompt-toolkit/pyvim, forked as AlDanial/pyvim) was
the starting point of this investigation. Its vi behavior turned out to
be almost entirely prompt_toolkit's; pyvim adds only redo, an ex command
line and windows/tabs. So tdb depends on prompt_toolkit, **not** pyvim.
(The fork's Python 3.14 startup fix is committed separately on its
`fix-py314-event-loop` branch and is unrelated to tdb.)

**Success:** a vim user can edit in Code View the way they edit in vim
for everyday code editing (motions, operators, text objects, counts,
visual mode, registers, `.`, macros, search, marks, and `:s` / `:g` /
`:v`) without being surprised.

## Goals

1. Replace `VimLayer` with a layer that feeds every key to a headless
   prompt_toolkit vi engine and mirrors the result into the existing
   `CodeEditor` (Textual `TextArea`).
2. Fill the engine's gaps and fix its bugs (listed below) so common vim
   commands behave like vim.
3. Marks `a`–`z`, kept **per file for as long as tdb runs**, adjusted as
   lines are inserted and deleted.
4. Vim-style inline search (`/`, `?`, `n`, `N`, `*`, `#`) on the pane's
   status line, using **Python `re` syntax**.
5. An ex command line with `:s`, `:g`, `:v` (and friends, below), using
   **Python `re` syntax**.
6. Document all of it, including what differs from vim.

## Non-goals

- Vim regex syntax (`\(`, `\<`, `\v`, `~` ...). Patterns are Python `re`.
- `:set` options, `.vimrc`, `:map`, multiple windows/buffers/tabs,
  `:e`, `:r`, `:!`, folding, spell, digraphs, completion, `gq`, `=`.
- Uppercase/file marks (`mA`), special marks other than those listed.
- Persisting marks, registers or history across tdb runs.
- Persistent search highlighting (`hlsearch`).
- Block visual mode (deferred; see Decisions).
- System clipboard registers (`"+`, `"*`, `clipboard=unnamed[plus]`).
  `"+`/`"*` are rejected with a short message, not silently ignored.
- Changing the emacs or default (Notepad-style) schemes in any way.
- Upstreaming fixes to prompt_toolkit (welcome, but not part of this).

## Anti-goals (failure even if it "works")

- **Any vi command that corrupts the file or crashes tdb.** prompt_toolkit
  has such bugs today (`ci(` outside brackets duplicates the buffer;
  `"ayy` raises `IndexError`). Every key fed to the engine must be
  exception-safe: on an exception, restore the pre-key buffer text and
  cursor, show a warning notification, and keep going.
- tdb's pane-switching keys stop working while editing.
- Typing feels laggy on a few-thousand-line source file.
- The emacs or default schemes change behavior.

## Decisions made with the user

- **Engine:** prompt_toolkit's vi bindings, headless, inside Textual. Not pyvim.
- **Ctrl keys:** tdb's pane keys win. `Ctrl+C` (Code), `Ctrl+O`
  (Console), `Ctrl+E` (Evaluate), `Ctrl+V` (Variables), `Ctrl+S`
  (Stack; in Edit mode it is Save, as today), `Ctrl+B` (Breakpoints)
  and `Ctrl+Q` (Quit) never reach the engine. The docs must state that
  vim's `Ctrl+V` (visual block), `Ctrl+O` (jump back), `Ctrl+E` (scroll
  line) and `Ctrl+B` (page up) are therefore unavailable, and name the
  alternatives where one exists (PageUp for `Ctrl+B`).
  Other Ctrl keys (`Ctrl+R`, `Ctrl+D`, `Ctrl+U`, `Ctrl+F`, `Ctrl+Y`,
  `Ctrl+A`, `Ctrl+X`, `Ctrl+W`/`Ctrl+H` in insert mode) go to the engine.
- **Marks:** `ma`–`mz`, `'a` / `` `a `` as jumps *and* as operator
  motions (`d'a`, ``y`a``, `c'a`), `''` and ``` `` ``` (jump back), and
  `'<` / `'>` for the last visual selection. Per file, for the life of
  the tdb process. Marks move with inserted/deleted lines as in vim; a
  mark on a deleted line is removed.
- **Ex commands:** `:s`, `:g`, `:v` added (details below), plus the
  existing `:w :q :q! :wq :x :N`.
- **Patterns:** Python `re` syntax everywhere (search, `:s`, `:g`, `:v`),
  documented as a deliberate difference from vim.
- **Esc is layered:** if anything is pending (count, operator, register
  prefix, `f`/`t` awaiting a char, visual selection, insert/replace
  mode, the `:` or `/` line, a `:s///c` prompt), Esc cancels it as vim
  does. Otherwise Esc leaves Edit mode as today (with the existing
  unsaved-changes modal). `:q`, `:wq` etc. keep working.
- **Search** is vim-style on the pane status line, incremental (the
  cursor moves as you type, Enter keeps it, Esc returns), with `n` `N`
  `*` `#`. The last pattern is shared with `:s//…/` and `:g//…`. The
  emacs/default schemes keep tdb's existing search modal.
- **No block visual mode in this version.** Its vim keys (`Ctrl+V`,
  `Ctrl+Q`) are tdb's Variables and Quit keys. The engine's block
  visual must be unreachable (no other key enters it), and the docs
  list it as unsupported.

## Architecture

### Where it sits

```
CodeView (unchanged contract)
 └─ CodeEditor(TextArea)            renders text, gutter, highlighting, selection
     └─ key layer for scheme "vim": ViLayer   (replaces VimLayer)
         └─ ViEngine                headless prompt_toolkit Application + tdb bindings
             ├─ ptk vi bindings     (load_vi_bindings, via EditingMode.VI)
             ├─ tdb gap bindings    (fixes + additions, higher priority)
             ├─ SearchEngine        (Python re, tdb-owned)
             └─ ExCommandLine       (:, tdb-owned)
```

`CodeEditor` keeps its messages (`LeaveRequested`, `SaveRequested`,
`SubmodeChanged`); CodeView's handling of them, saving, the dirty flag,
the unsaved-changes modal and breakpoint remap are unchanged.
`EmacsLayer` and the default scheme are untouched.

All prompt_toolkit-specific code, especially any use of its internals,
lives in **one module** (suggested `src/tdb/widgets/vi_engine.py`) so a
prompt_toolkit upgrade has one place to break. `ViLayer` (Textual side)
should not import prompt_toolkit.

### The headless engine (proven in a prototype)

A prompt_toolkit `Application` is built but **never run**:

- `input=DummyInput()`, `output=DummyOutput()`, `editing_mode=EditingMode.VI`.
- A layout containing a `Window(BufferControl(buffer))` (prompt_toolkit's
  bindings look up the focused buffer through the layout).
- `app.timeoutlen = None` — otherwise the key processor tries to start a
  timeout task for ambiguous prefixes like `d`, and vim waits forever
  for the rest of a command anyway.
- Keys are delivered with
  `with set_app(app): app.key_processor.feed(KeyPress(...)); app.key_processor.process_keys()`.
- Start in `InputMode.NAVIGATION`.
- The tdb bindings are passed as the Application's `key_bindings`; in
  prompt_toolkit those take priority over the built-in vi bindings for
  the same key sequence, which is how fixes override broken behavior.

The prototype ran this inside a live Textual app (`run_test` + `Pilot`)
on Textual's asyncio loop with no problems. Some prompt_toolkit code
paths call `app.create_background_task`, which needs a running loop;
Textual provides one, but unit tests of the engine alone must run inside
an event loop too (or those paths must be avoided — e.g. no completer).

The Buffer has no completer, no auto-suggest, no validator.

### Key translation

`ViLayer.handle_key(event)` maps a Textual `Key` to a prompt_toolkit
`KeyPress`: printable `event.character` → itself; `escape`, `enter`,
`backspace`, `tab`, `delete`, arrows, `home`/`end`, `pageup`/`pagedown`
→ the matching `Keys` member; `ctrl+<letter>` → `Keys.ControlX`.
Keys that belong to tdb (the pane keys above, `Ctrl+Q`, `Ctrl+P`
command palette, `alt+*` menu keys, function keys) are returned
unhandled so they bubble exactly as they do today — **except in insert
and replace mode**, where Textual may deliver a fast `Esc`+letter (very
common over ssh/tmux) as `alt+<letter>`, and `Esc`+`f` as `ctrl+right`.
There, `alt+<key>` (and the `ctrl+right`/`ctrl+left` Textual produces
for `Esc f`/`Esc b`) is split back into `Esc` followed by the key, as
terminal vim does. Consequence, to document: the Alt+letter menu
shortcuts don't open menus from insert mode; press Esc first. (My call.) Keys with no mapping
are swallowed in normal/visual mode (a stray key must never edit) and
in insert mode too unless they map.

### Mirroring engine → TextArea

After each key: if the engine text changed, apply **one** `TextArea`
replace covering the changed span (common-prefix/common-suffix diff),
not a full `load_text` — keeps tree-sitter highlighting incremental,
keeps scroll position, and keeps it fast. Then set the TextArea cursor
from the engine cursor and the selection from the engine selection
state, and post `SubmodeChanged` when the mode label changed.

The engine is the source of truth; TextArea's own undo history and edit
keys are never used in the vim scheme (all keys go through the engine).
`TextArea.edit()` works on a `read_only` TextArea, so the builder may
set `read_only` as a safety net if it doesn't interfere with the cursor.

Line endings: the engine works on `\n` text. CRLF files must round-trip
unchanged on save (verify with a test; `source_edit.py` writes with
`newline=""`).

Selection rendering: character and line visual map onto TextArea's
selection (line visual = whole lines). Block visual is not offered in
this version (see Decisions).

Mouse: a click moves the engine cursor (and leaves visual mode); a drag
starts character visual mode over the dragged span. Bracketed paste
inserts the pasted text at the cursor as a single undo step, in any
mode (as vim does with bracketed paste), and leaves the mode unchanged.

### Mode label

Extends `CodeView.mode_label()`'s existing scheme (`Edit`, `Edit:INSERT`,
`Edit :wq`, trailing `*` when dirty) with: `:REPLACE`, `:VISUAL`,
`:V-LINE`, pending keys (e.g. `Edit "a2d`), recording
(`Edit recording @a`), the search line (`Edit /pat`), and the `:s///c`
prompt (`Edit replace with X (y/n/a/q/l)?`).

### Gaps tdb must fill (found by auditing the headless engine)

Each needs a tdb binding/override and a test whose expected result
comes from real vim (see Testing):

| Area | Problem in prompt_toolkit 3.0.53 |
|---|---|
| `.` repeat | Not implemented at all. Must repeat the last change: operator+motion/text object, `x`/`r`/`J`/`p`, insert sessions (`ciwfoo<Esc>`, `A;<Esc>`), with counts (`3.`). Visual-mode repeat is best-effort. |
| Redo | `Ctrl+R` not bound in vi mode (pyvim adds it; copy that). |
| Registers | `"ayy`, `"add`, `"acc` crash (`IndexError`); `"ap` after linewise yank. Support `""`, `"a`–`"z`, `"A`–`"Z` (append), `"0` (last yank), `"_` (black hole). |
| Macros | `@@` missing; counts on `@a` (`3@a`) must work. |
| Text objects | `ci(` / `di{` / `ci"` etc. with no enclosing pair corrupts the buffer — must be a no-op. `iw`/`aw` on whitespace runs is wrong (at column 0 it deletes the previous line's end). `ap`/`ip` wrong. |
| Join | `3J` joins one line too many. |
| Visual | `gv` missing; `U` / `u` / `~` in visual missing. Verify `o`, `J`, `>`, `<`, `r` in visual. |
| Numbers | `Ctrl+A` / `Ctrl+X` missing (decimal, negative, `0x` hex; with counts). |
| Viewport | `Ctrl+D`/`Ctrl+U`/`Ctrl+F`/`Ctrl+Y`, `H`/`M`/`L`, `zz`/`zt`/`zb` do nothing headless (no render info). tdb implements them from the TextArea's viewport (height, scroll offset). |
| Insert `Tab` | Tries completion and needs a running loop. Bind to insert indentation the way the current `CodeEditor` does (`indent_width`/indent type). |
| Marks | Not implemented (see Decisions). |
| Search | prompt_toolkit's search is literal text and uses its own toolbar. tdb replaces `/ ? n N * #` with a Python-`re` search that works both as a motion and as an operator target (`d/foo<Enter>`, `cn`). `*`/`#` search for `\bWORD\b` with the word `re.escape`d. Wraps around the file with a status notice; "Pattern not found: …" and invalid-regex errors are notifications. Case-sensitive by default; `(?i)` documents how to ignore case. |

The builder should run a broader compatibility matrix than this table
(every motion, operator, text object and command in the docs' list) and
either fix or document each discrepancy it finds.

Already verified working in the engine (still covered by tests):
`cw`, `{`/`}`, `f F t T ; ,`, `N|`, `dt=`, `ct)`, `%`, `w W b B e ge`,
`ci"`, `yiw`, `di(` inside parens, `r`, `R`, `s`, `S`, `cc`, `C`, `D`,
`g~w`, `gUiw`, `~`, `>>`, `V…>`, `dd`/`2dd`/`d2j`,
`yy p P`, `u`, `qa…q`/`@a`, `o O i a I A`.

Insert-mode keys: `Ctrl+W` and `Ctrl+H`/Backspace come from the engine.
The `Ctrl+W` behavior on punctuation differs slightly from vim; fix it
if cheap, otherwise document it.

### Marks — mechanics

- Stored as `(line, column)` per file in a session-wide map owned by the
  App or CodeView (not by the editor, which is rebuilt on each Edit-mode
  entry), keyed by the file's resolved path.
- On every text change, the prefix/suffix diff used for mirroring gives
  the changed line range and the line-count delta; shift marks below it,
  drop marks on deleted lines. (Same adjustment for `''` and `'<`/`'>`.)
- `'x` targets the line's first non-blank and is linewise for operators;
  `` `x `` is the exact position and characterwise exclusive.
- Implemented via prompt_toolkit's operator hook
  (`vi_state.operator_func` while in "waiting for text object" mode),
  wrapped in a tdb helper so the internal use is in one place.
- Discarding unsaved changes reverts text; marks set during that
  session stay (vim behaves the same when you `:e!`).

### Session-wide state (my call)

Like vim, these are global for the tdb run, not per file: registers,
the last search pattern and direction, the last `:s` (pattern,
replacement, flags), macros, and the `:` and `/` histories. Undo
history is per Edit-mode session (cleared when leaving Edit mode).

### Ex command line

Typed after `:` on the pane status line. Backspace edits, Backspace on
an empty line or Esc cancels, Enter runs, Up/Down walk the session
history.

**Ranges** (any command that takes one): none (current line), `N`, `.`,
`$`, `%`, `N,M`, `.+N`/`.-N`/`$-N`, `'a,'b` (marks), `'<,'>` (pressing
`:` in visual mode pre-fills `'<,'>` as vim does), `/pat/` and `?pat?`
as line addresses are **out of scope**.

**Commands:**

| Command | Meaning |
|---|---|
| `:w`, `:q`, `:q!`, `:wq`, `:x`, `:N` | unchanged |
| `:[range]s/pat/repl/[flags]` | substitute; flags `g` (all on line), `i` (ignore case), `c` (confirm each: `y n a q l`, Esc = `q`). Delimiter may be any non-alnum char (`:s#a/b#c#`). Empty `pat` = last search. `:s` alone or `:&` repeats last `:s` on the range; `&` in normal mode repeats on the current line. Replacement uses Python `re` syntax (`\1`, `\g<name>`); `\n` in the replacement inserts a newline. |
| `:[range]g/pat/cmd`, `:g!/pat/cmd`, `:[range]v/pat/cmd` | run `cmd` on each matching (`:g`) / non-matching (`:v`, `:g!`) line; default range `%`. `cmd` may be `d`, `s/…/…/[flags]`, `normal <keys>`, `y [x]` (append to register; `:g` clears it first as vim does with uppercase), `m <addr>`, `t <addr>` / `co <addr>`. Empty `cmd` means `p` — out of scope; report an error. |
| `:[range]d [x]`, `:[range]y [x]` | delete / yank lines (optional register) |
| `:[range]m <addr>`, `:[range]t <addr>` / `:co <addr>` | move / copy lines below `<addr>` (`0` = top) |
| `:[range]normal <keys>` / `:norm` | run vi keys on each line in range |

Rules:
- A whole `:s`, `:g` or `:v` is **one undo step** (one `u` reverts it).
- `:g` marks matching lines first, then runs `cmd` on each still-existing
  marked line, as vim does (so `:g/x/d` works when lines shift).
- After `:s`/`:g`, the cursor lands where vim puts it (last affected line).
- Status/notification messages: "N substitutions on M lines",
  "Pattern not found: pat", invalid-regex error text, "E492: Not an
  editor command: …"-style message for unknown commands (wording is the
  builder's call; tdb already notifies "Unknown command").
- The ex parser may borrow from pyvim's (`pyvim/commands/grammar.py`,
  `commands.py`; BSD-3-Clause) — keep its license notice on any copied code.

## Constraints

- **Dependency:** add `prompt_toolkit>=3.0.52,<3.1` to core
  `dependencies` (vim is the default scheme, so it can't live in an
  extra). The upper bound guards the internal APIs used. The gap audit
  was done on 3.0.53; before merging, run the engine tests once against
  3.0.52 and the newest 3.0.x and adjust the floor if they differ. No
  permanent version-matrix CI. (My call.)
- **Cross-platform:** must work on Windows. The engine must never create
  a real prompt_toolkit input or output (always the Dummy ones), so
  prompt_toolkit never touches the Win32 console. Verification: add a
  narrow `windows-latest` job to `.github/workflows/test.yml` that runs
  only the vi engine and `CodeEditor` tests (not the full suite, whose
  subprocess-heavy tests are not Windows-clean). (My call.)
- **Performance:** one key → one engine step + one diff-sized TextArea
  edit. Macro replay and `:g` over thousands of lines must finish in
  well under a second for a few-thousand-line file; mirror once per
  ex command / macro run, not once per inner step.
- Keys tdb's App binds with `priority=True` are resolved before any
  widget's `_on_key`; audit them (the existing `check_consume_key`
  special case for `Ctrl+P` under emacs is the precedent).
- Edit-mode refusal rules (non-UTF-8 files, remote/replay/post-mortem
  sources) are unchanged.

## Testing

- `uv run pytest` (project rule). Use the memcap wrapper for full-suite
  runs (see OOM incident notes).
- **Engine unit tests** (no Textual): table-driven `(start text, cursor,
  keys) → (end text, cursor, mode)` cases for every gap above and every
  "already verified" command.
- **Expected results come from real vim.** A small developer script
  (not run in CI) drives `nvim --headless -u NONE -i NONE` (the only vim
  on the dev machine; `/usr/bin/vim` is nvim) over the same table and
  writes a fixture file; the tests assert against the fixture. Baseline
  settings (my call), chosen to match what tdb users get: `:mapclear`
  (drops nvim's default `Y`=`y$`, `&` etc. mappings, so `Y` = `yy` as in
  classic vim and prompt_toolkit), `set shiftwidth=4 softtabstop=4
  expandtab autoindent nojoinspaces nrformats=bin,hex nostartofline`.
  tdb keeps prompt_toolkit's indentation-copying on Enter/`o`/`O`
  (= `autoindent`). Commit the script and the fixture. Where tdb
  intentionally differs (Python regex, lost Ctrl keys), the case is
  marked as an expected difference.
- **Textual integration tests** via `run_test`/`Pilot` on `CodeEditor`
  and through `CodeView`/App Edit mode: key routing (pane keys still
  switch panes, `Ctrl+S` saves), Esc layering, mirroring, cursor and
  selection rendering, mode label, mouse click
  and paste, marks surviving leave/re-enter and file switches, CRLF
  round trip.
- **Exception safety:** a test that injects a failing binding and checks
  the buffer is restored and a warning shown.
- Existing `tests/unit/test_code_editor_vim.py` tests: keep the ones
  whose behavior is still correct vim; rewrite or drop the ones that
  encoded vim-lite behavior (e.g. `/` opening the search modal), noting
  each dropped test in the commit message.
- Emacs/default scheme tests must pass unchanged.

## Documentation

- `docs/using/keybindings.md`: replace the "vim-lite" row; add a vim
  Edit-mode section listing supported motions, operators, text objects,
  registers, marks, search and ex commands; a **Differences from vim**
  list: Python `re` patterns (with a short translation table: `\(…\)` →
  `(…)`, `\<w\>` → `\bw\b`, `\{n}` → `{n}`, `~` unsupported), the four
  lost Ctrl keys and their alternatives, marks lowercase-only and
  per-run, no `:set`, no block visual, no system clipboard registers,
  Alt+letter menu shortcuts need Esc first when in insert mode.
- Keybindings dialog Edit Mode section (`src/tdb/widgets/modals.py`).
- `--keybindings` help text if it says "vim-lite".
- `tdb --doc` picks up the docs pages automatically (verify).

## Left to the builder

- Module/class names, how the gap bindings are organized, and the exact
  key-translation table.
- How `.` repeat records changes (e.g. capturing the key sequence from
  the start of a change until the engine returns to normal mode with the
  text changed, and replaying it).
- Exact status/notification wording.
- Whether to keep `read_only=True` on the TextArea.

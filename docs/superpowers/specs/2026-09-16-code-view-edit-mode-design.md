# Code View Edit Mode — Design

**Date:** 2026-09-16
**Branch:** `code-view-edit-mode`
**Status:** Approved design, pre-implementation

## Problem

The Code View is read-only. A user who spots a bug while stepping has
to leave tdb (or switch to another terminal), edit the file, come back,
and restart. The round trip breaks concentration and loses the cursor
position and breakpoint context that tdb was holding.

## Goal

A third Code View mode, **Edit**, reachable by the same Esc cycle that
already toggles Debug and Navigate. In Edit mode the user can change
the file being viewed and save it to disk. Editing keys follow the
user's chosen keybinding scheme: vim users get a vim-style modal
editor, emacs users get emacs bindings, and everyone else gets a
Notepad-style editor that only needs arrow, page, insert, delete and
backspace keys.

## Scope

**In version one**

- `Mode.EDIT`, cycled by Esc: Debug → Navigate → Edit → Debug.
- Editor built on Textual's `TextArea` (Textual 8.x is already the
  pinned framework).
- Three editing key layers matching the three existing schemes:
  Notepad (`default`), emacs, and vim-lite.
- Atomic save to the file on disk; dirty marker in the pane title;
  Save / Discard / Cancel modal on every path that would abandon
  unsaved changes (leave Edit mode, quit, restart).
- Breakpoint line remap after a save, and a "press R to restart"
  notification. No hot reload.
- An `Edit` menu: Save, Revert to Disk, Discard and Exit Edit Mode,
  Open in $EDITOR.
- Relabel of the `default` scheme as "Notepad-style" in the
  Keybindings dialog, `--keybindings` help, and README. The config
  value stays `default`.
- Optional `textual-debugger[edit]` extra for syntax highlighting
  inside the editor.

**Explicitly out of scope**

- Vim visual mode, registers, text objects, `.` repeat, macros, `:s`,
  marks, and ex commands beyond `:w :q :wq :q! :<N>`.
- Emacs kill ring beyond a single yank, rectangle commands, dired,
  M-x.
- Hot-patching the running debuggee. The new code runs only after
  restart.
- Editing files that tdb cannot write: remote-attach in-memory
  sources, replay sessions, post-mortem snapshots.
- Multi-file editing. Only the file currently shown is editable.
- Editing from the MCP / JSON-RPC server.

## Design

### 1. Mode model and the Esc cycle

`tdb.keybindings.Mode` gains `EDIT = "Edit"`. `CodeView._on_key`
cycles Debug → Navigate → Edit → Debug on Esc, except that:

- On the vim scheme, when the editor is in insert mode, Esc returns
  to normal mode and does not leave Edit mode. Esc in normal mode
  leaves Edit mode and continues the cycle to Debug.
- On emacs and Notepad, a single Esc leaves Edit mode.

Other exits from Edit mode: vim `:q` / `:wq` / `:q!`, emacs
Ctrl+X Ctrl+C, and the Edit menu items. All exits go through one
method, `CodeView.leave_edit_mode(discard: bool = False)`, which owns
the unsaved-changes check (section 4). Every exit path must call it;
there is no second way out.

The pane title (set by `TdbApp._update_code_title`) shows the mode and
editor state:

| State | Title |
|-------|-------|
| Edit, clean | `Code [Edit]` |
| Edit, unsaved | `Code [Edit*]` |
| Edit, vim insert | `Code [Edit:INSERT]` / `Code [Edit:INSERT*]` |
| Edit, vim command line | `Code [Edit :wq]` — the typed command shown in the pane title itself, not the footer |

`CodeView.ModeChanged` is posted on every transition, as today, and
additionally whenever the dirty flag or vim sub-mode changes so the
title stays current.

Entering Edit mode is refused, with `app.notify` explaining why, when:

- the source was installed by `load_content` (remote attach, file not
  on the tdb host),
- the app is running a replay driver,
- the app is in post-mortem snapshot mode,
- no file is loaded.

Refusal keeps the cycle moving: Esc from Navigate goes straight to
Debug in those cases.

### 2. Widget integration

New module `src/tdb/widgets/code_editor.py`:

```
class CodeEditor(TextArea):
    """TextArea configured for tdb: line numbers, no soft wrap,
    tab inserts indentation, optional tree-sitter language."""

class VimLayer:
    """Normal-mode key state machine for the vim scheme (section 3)."""

class EmacsLayer:
    """Ctrl+X chord state for the emacs scheme (section 3)."""
```

`CodeView` keeps its existing line-API renderer. On entering Edit
mode it:

1. Hides `_CodeContent` (`display = False`).
2. Mounts a `CodeEditor` loaded with `"\n".join(self._lines)` plus a
   trailing newline if the original file had one (the original
   trailing-newline state is remembered from `load_file`).
3. Moves the editor cursor to `(cursor_line - 1, 0)` and scrolls it
   into view.
4. Focuses the editor.

On leaving Edit mode it:

1. Reads `editor.text`, remembers the editor cursor row.
2. Unmounts the editor and shows `_CodeContent` again.
3. Reinstalls the resulting text (the saved text, or the on-disk
   text after Discard) via `_install_source` so highlighting, step
   units, valid breakpoint lines and the maximum line width are
   recomputed exactly as after File > Open. Cancel never reaches this
   step because it stays in Edit mode.
4. Restores `cursor_line` to the editor row.

`CodeView` exposes `is_editing`, `is_dirty`, and `editor_submode`
(`None`, `"normal"`, `"insert"`, `"command"`) for the title and for
tests. The `TextArea` instance is not reached into from `TdbApp`; the
app talks to `CodeView` only.

Editor configuration: `show_line_numbers=True`, `soft_wrap=False`,
`tab_behavior="indent"`, `language=<lexer>` when tree-sitter is
available and the language is one of Textual's built-ins (python,
bash, go, rust), otherwise `language=None`. The editor theme follows
the app theme via `TextArea`'s default `css` theme.

Mouse clicks inside the editor move the caret (TextArea default);
they do not toggle breakpoints. The breakpoint gutter is not drawn
while editing.

### 3. Keybinding schemes in Edit mode

`KeybindingConfig` gains an `edit_scheme` property that is the same
string as `scheme`, and `format_bindings(Mode.EDIT)` returns the
editing keys for the Keybindings dialog. The bindings themselves live
in `code_editor.py`, not in the key→action tables, because they
operate on `TextArea` rather than on `CodeView` actions.

**Notepad (`default`)**

`TextArea` defaults, unchanged:

| Key | Action |
|-----|--------|
| Arrows, Home, End, PageUp, PageDown | Move |
| Shift + movement | Select |
| Insert | Swallowed (see note) |
| Delete, Backspace | Delete right / left |
| Ctrl+Z / Ctrl+Y | Undo / redo |
| Ctrl+X / Ctrl+C / Ctrl+V | Cut / copy / paste |
| Ctrl+S | Save |
| Esc | Leave Edit mode |

Note on Insert: `TextArea` has no overwrite mode. Insert is swallowed
silently (no footer hint) so the key does nothing surprising;
overwrite mode is not implemented.

**Emacs**

`TextArea` defaults plus these overrides, applied by `EmacsLayer`:

| Key | Action |
|-----|--------|
| Ctrl+N / Ctrl+P | Down / up |
| Ctrl+F / Ctrl+B | Right / left |
| Alt+F / Alt+B (and Ctrl+Right / Ctrl+Left, see README note on Alt) | Word right / left |
| Ctrl+A / Ctrl+E | Line start / end |
| Ctrl+D | Delete right |
| Ctrl+K | Kill to end of line (kill text kept for Ctrl+Y) |
| Ctrl+Y | Yank last kill |
| Ctrl+/ , Ctrl+_ | Undo |
| Ctrl+S | Open the search modal (matches emacs navigation) |
| Ctrl+X Ctrl+S | Save |
| Ctrl+X Ctrl+C | Leave Edit mode |
| Ctrl+G | Cancel a pending Ctrl+X chord |
| Alt+< / Alt+> | Start / end of buffer |
| Esc | Leave Edit mode |

`EmacsLayer` holds one bit of state, "Ctrl+X pending", and a one-slot
kill buffer.

**Vim-lite**

`VimLayer` is a state machine with sub-modes `normal`, `insert`,
`command`. Insert mode passes every key to `TextArea` untouched.
Command mode collects a line after `:` and executes it on Enter.
Normal mode interprets:

| Keys | Action |
|------|--------|
| `h j k l`, arrows | Move (count applies) |
| `w b e` | Word motions |
| `0 ^ $` | Line start / first non-blank / line end |
| `gg`, `G`, `<N>G` | Buffer start / end / line N |
| Ctrl+F / Ctrl+B | Page down / up |
| `x X` | Delete char under / before cursor |
| `dd`, `<N>dd` | Delete line(s) into the yank buffer |
| `dw`, `D` | Delete word / to end of line |
| `yy`, `<N>yy` | Yank line(s) |
| `p P` | Put after / before (line-wise if the buffer is line-wise) |
| `u`, Ctrl+R | Undo / redo |
| `J` | Join lines |
| `i a I A o O` | Enter insert mode at the corresponding position |
| `/`, `?` | Open the existing search modal; `n`/`N` step results |
| `:` | Enter command mode |
| Esc | Leave Edit mode |

Command mode accepts `w`, `q`, `wq`, `x`, `q!`, and an integer line
number. Anything else shows "Unknown command" in a notification and
stays in normal mode.

Unbound keys in normal mode are swallowed, so typing a letter never
inserts text by accident. A single yank buffer holds text plus a
line-wise flag.

The Keybindings dialog gains an **Edit Mode** section after Debug Mode
listing the active scheme's keys from the tables above. The scheme
radio buttons read `Vim`, `Emacs`, `Notepad-style`.

### 4. Save, dirty state, and unsaved changes

`is_dirty` is `editor.text != text_as_loaded`. It is recomputed on
`TextArea.Changed`.

**Save** (`CodeView.save_file()`):

1. Encode `editor.text` as UTF-8, preserving the original
   trailing-newline state.
2. Resolve `source_path` to its real target with `os.path.realpath`
   (so saving through a symlink writes the linked-to file, not a new
   regular file in its place), write to `<dir>/.<name>.tdb-tmp` in the
   resolved target's directory, flush and fsync, copy the target's
   file mode onto the temp file (tolerating a missing target — a new
   file just keeps the umask mode), then `os.replace` onto the
   resolved target. This is atomic on POSIX and Windows.
3. On `OSError` (read-only filesystem, permission denied, missing
   directory): remove the temp file if it exists, `app.notify` with
   the error, keep the buffer dirty, return `False`.
4. On success: update `text_as_loaded`, clear the dirty flag, run the
   post-save hook (section 5), return `True`.

Ctrl+S saves in every scheme (except emacs, which uses Ctrl+X Ctrl+S
because Ctrl+S is search there).

**Unsaved-changes modal** (`_UnsavedChangesModal` in
`code_editor.py`, returning `"save" | "discard" | "cancel"`):

Shown by `leave_edit_mode()` whenever `is_dirty` and `discard` is
`False`. Save attempts a save and, on failure, stays in Edit mode.
Discard reloads the file from disk into the viewer. Cancel stays in
Edit mode with the buffer intact.

The same modal guards:

- every quit path in `TdbApp` (`action_quit_debugger`,
  `_detach_and_exit`, `_terminate_and_exit`, the `q` confirm flow,
  Ctrl+Q) via one helper `TdbApp._confirm_discard_edits()` that
  resolves `True` when it is safe to proceed;
- restart (`R`, the restart menu item, the crash modal's restart
  choice) via the same helper;
- File > Open, which would replace the buffer.

Cancel in any of these aborts the quit / restart / open.

### 5. Interaction with the debug session

Editing is allowed whether the debuggee is paused, running, or
terminated. Stop events that arrive while editing update the stack,
variables and status bar as today, but do not move the editor caret
and do not switch modes. The current-line highlight is not shown in
the editor.

**Post-save hook** (`TdbApp.on_code_view_file_saved`, driven by a new
`CodeView.FileSaved(path, old_lines, new_lines)` message):

1. Compute a line map old→new with
   `difflib.SequenceMatcher(None, old_lines, new_lines)`: lines inside
   `equal` blocks map by offset; lines inside `replace` blocks map to
   the corresponding line in the new block when the block sizes match,
   otherwise to the first line of the new block; lines inside `delete`
   blocks are dropped.
2. Apply the map to `controller.state.breakpoints[path]`, preserving
   condition, hit condition and enabled flag; drop breakpoints whose
   line was deleted; collapse duplicates that land on the same line.
3. Re-push the file's breakpoints through the controller so the
   adapter sees the new lines, and refresh the Breakpoint View.
4. Clear the Code View's current-line marker, because the debuggee is
   still executing the old code.
5. `app.notify("Saved <name>. Press R to restart with the new code.")`
   when the session supports restart, or `"Saved <name>."` otherwise.

Restart already reloads the file via the normal stop pipeline, so no
special reload is needed. Saved breakpoints on quit (persist.py)
pick up the remapped lines automatically.

### 6. Menu and configuration

`TdbApp.compose` adds an `Edit` menu between the `File` label and
`Configure`:

| Item | Behavior |
|------|----------|
| Save | `code_view.save_file()`; when not editing, only `app.notify`s ("Nothing to save: not in Edit mode.") — it does not enter Edit mode |
| Revert to Disk | `code_view.revert_to_disk()`: replaces the buffer with the on-disk text *in place*, staying in Edit mode. No modal — this action itself only discards buffer content in favor of disk content, so nothing further needs confirming — and it is undoable via the editor's own undo |
| Discard and Exit Edit Mode | `leave_edit_mode(discard=True)`, with no unsaved-changes modal — the menu item's name already says what it does, so a second confirmation would be redundant |
| Open in $EDITOR | See below |

Menu items act on the current file whether or not Edit mode is active;
Save and Revert to Disk are no-ops with a notification when there is
nothing to save or nothing unsaved to revert.

**Open in $EDITOR**: resolve `$VISUAL`, then `$EDITOR`; on Windows fall
back to `notepad`, on POSIX to `vi`. Refuse with a notification in
the same cases that refuse Edit mode, plus when a dirty tdb buffer
exists (the modal offers to save first). Run
`with self.app.suspend(): subprocess.run([editor, path])`. On return,
reload the file and run the post-save hook if the mtime changed. The
editor command is split with `shlex.split(value)` on POSIX so
`EDITOR="code -w"` works; on Windows it is split with
`shlex.split(value, posix=False)` (POSIX quoting/escaping rules don't
apply to Windows paths, e.g. backslashes), so
`EDITOR="C:\Tools\ed.exe -n"` becomes `["C:\Tools\ed.exe", "-n"]`
rather than one unresolvable argv[0].

Alt+E opens the Edit menu, following the existing Alt+letter
convention in `menu_bar.py`. The first letter is highlighted green.

**Relabel**: `_KeybindingsModal._SCHEMES` keeps `("vim", "emacs",
"default")` but the radio label for `default` reads `Notepad-style`.
`--keybindings` help reads `vim, emacs, or default (Notepad-style
editing)`. Saved configs with `keybindings = "default"` are unchanged.

### 7. Syntax highlighting while editing

`pyproject.toml` gains an optional extra:

```
[project.optional-dependencies]
edit = ["textual[syntax]>=8.0.0"]
```

`CodeEditor` checks `textual._tree_sitter.TREE_SITTER` at construction
and passes `language=` only when it is true and the lexer is one of
python, bash, go, rust. The core install is unchanged, and the editor
works without highlighting when the extra is absent. README documents
`uv pip install "textual-debugger[edit]"`.

### 8. Cross-platform notes

- Atomic save uses `os.replace`, which works on Windows since the
  temp file is in the same directory.
- `$EDITOR` handling has an explicit Windows branch (section 6).
- Alt+F / Alt+B in the emacs layer also accept Ctrl+Right / Ctrl+Left,
  matching the existing terminal quirk documented in the README.
- Read-only config directories are already tolerated; saving the
  source file follows the same tolerate-`OSError` rule.
- CRLF source files: `load_file` splits the on-disk text into
  `self._lines` with `str.splitlines()`, which discards the original
  line-ending characters, and `CodeView._editor_text()` rejoins them
  with `\n`. A CRLF file therefore enters the editor (and is written
  back by `save_file()`) with LF line endings, not its original CRLF.
  This is a known v1 limitation (no CRLF round-trip); a later spec can
  restore the original per-file line ending on save if it matters in
  practice.

### 9. Testing

**Unit (no TUI)**

- `VimLayer`: every normal-mode key in the table, counts, `dd`/`yy`/
  `p` line-wise round trip, `:` commands including unknown command,
  Esc from insert returns to normal without leaving Edit mode.
- `EmacsLayer`: Ctrl+X chord state, Ctrl+G cancel, kill/yank buffer.
- Breakpoint remap: insert above, insert below, delete the breakpoint
  line, replace block of equal size, replace block of different size,
  duplicate collapse, condition and enabled flags preserved.
- Atomic save: trailing newline preserved, temp file removed on
  `OSError`, dirty flag semantics.

**Textual pilot**

- Esc cycle on each scheme: Debug → Navigate → Edit → Debug, and vim
  insert → normal → Debug.
- Title rendering for every state in the section 1 table.
- Unsaved-changes modal appears on Esc, `q`, Ctrl+Q, `R`, and File >
  Open with a dirty buffer; Cancel aborts, Discard reloads, Save
  writes.
- Edit mode refused (with notification) for remote in-memory source,
  replay, and post-mortem snapshot.
- Ctrl+S saves on Notepad and vim, Ctrl+X Ctrl+S on emacs, and the
  `FileSaved` message remaps a breakpoint.
- Keybindings dialog shows the Edit Mode section and the
  `Notepad-style` label.
- Existing `tests/unit/test_code_view_*` pass unchanged.

`uv run pytest` is the completion gate, per CLAUDE.md.

## Open items deferred to later specs

- Vim visual mode and text objects.
- Overwrite mode for the Insert key.
- Hot reload for languages that support it.
- Editing a file other than the one shown (a file picker inside Edit
  mode).

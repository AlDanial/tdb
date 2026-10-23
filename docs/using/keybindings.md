# Navigation and Keybindings


The Code View shows syntax-highlighted source (lexer chosen per language) with line numbers.
A cursor line (blue) tracks your position; the current execution line is highlighted in gold.

**View focus shortcuts (global):**

| Key | View |
|-----|------|
| `Ctrl+C` | Code View |
| `Ctrl+O` | Console View |
| `Ctrl+E` | Evaluate Console |
| `Ctrl+V` | Variable View |
| `Ctrl+S` | Stack View |
| `Ctrl+B` | Breakpoint View |

Pressing the shortcut of the view that already has focus returns focus to
the previous view, so e.g. `Ctrl+E` jumps from the Code View to the
Evaluate Console and a second `Ctrl+E` jumps back.
The shortcuts also work while typing in the Evaluate Console and the
Console View's stdin line, where `Ctrl+E` and `Ctrl+V` take precedence over
end-of-line and paste (`End` and the terminal's own paste still work).

**Menu-bar shortcuts (global):**

`Alt+<first-letter>` opens the corresponding tab in the menu bar.

| Key | Menu |
|-----|------|
| `Alt+F` | File (open a different script to debug) |
| `Alt+E` | Edit (Save, Revert to Disk, Discard and Exit Edit Mode, Open in $EDITOR) |
| `Alt+C` | Configure (Color Theme, Keybindings, Step Mode) |
| `Alt+T` | Threads |
| `Alt+P` | Processes |
| `Alt+A` | Async Tasks |
| `Alt+H` | Help (Documentation, About) |

File > Open works for every supported language: the file picker filters to
the current session's language (by extension) and refuses a pick that
detects as a different language, so you can only switch to another script
written in the same language as the one currently being debugged.

**Navigation (vim-style by default):**

By default the Code View is in Debug mode.  `Escape` cycles Debug → Navigate → Edit → Debug.
In Navigate mode, you can move around the file with the following keys:

| Key | Action |
|-----|--------|
| `j` / `k` | Move cursor down / up |
| `5j`, `10k` | Move N lines down / up with count prefix |
| `G` | Go to end of file (with count: `42G` jumps to line 42)|
| `[` / `]` | Jump to previous / next paragraph boundary |
| `/` | Search forward |
| `?` | Search backward |
| `n` / `N` | Next / previous search result |
| `PageUp` / `PageDown` | Scroll by page |

Press `Escape` again to enter Edit mode (below), and once more to return to Debug mode.

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
to their new lines, clears the current-line marker, and (when restart is
available) reminds you to press `R` to restart with the new code. In
`tdb --run` sessions, which cannot restart, you will see only "Saved
`<file>`." Edit mode is unavailable when no file is loaded, for sources
that are not on this machine (remote attach), during replay, and in
post-mortem mode.

The `Edit` menu (`Alt+E`) offers Save, Revert to Disk, Discard and Exit
Edit Mode, and Open in $EDITOR, which suspends tdb, runs `$VISUAL` /
`$EDITOR` (`notepad` on Windows, `vi` otherwise) on the file, and reloads
it when the editor exits.

Syntax highlighting inside the editor needs tree-sitter, an optional
extra: `uv pip install "textual-debugger[edit]"` (highlights Python, Bash,
Go, and Rust; other languages edit as plain text). Highlighting in the
normal Code View is unaffected.

CRLF source files are written back with LF line endings after a save in
Edit mode (a v1 limitation — no CRLF round-trip yet).

> **Note:** Many terminals send the byte sequence `ESC+f` for `Alt+F`, which Textual's
ANSI parser rewrites to `Ctrl+Right` (the readline "forward-word" convention).
`tdb` binds both so `Alt+F` works as expected.

## Keybinding Schemes

```bash
tdb --keybindings vim my_program.py    # default
tdb --keybindings emacs my_program.py
tdb --keybindings default my_program.py   # Notepad-style editing
```

The keybinding choice is saved to `~/.config/tdb/config.json` and remembered for subsequent
runs. View the full keybinding reference from the menu: **Configure > Keybindings**.

# Vim Edit Mode

With `--keybindings vim` (the default), Code View's Edit mode (press
`Escape` twice from Debug mode) is driven by a vi engine built on
prompt_toolkit's vi bindings plus tdb's own additions: marks, Python-`re`
search, and an ex command line with `:s`, `:g` and `:v`. The engine runs
headless and is mirrored into the editor, so what you see is the real
buffer. This page lists what is supported and, importantly, where tdb
differs from vim.

## Supported

**Modes.** Normal, insert, replace (`R`), characterwise visual (`v`) and
linewise visual (`V`). `gv` reselects the last visual area.

**Motions.** `h j k l`, `w b e W B E ge`, `0 ^ $ |`, `gg G NG`, `f F t T ; ,`,
`% ( ) { }`, `H M L`, `+ - _`, `gj gk`, `PageUp` / `PageDown`, and
`'a` / `` `a `` mark jumps. A count prefix works on all of them.

**Operators.** `d c y > < = ~ g~ gu gU J gJ`, doubled for lines (`dd`,
`cc`, `yy`, `>>`), with any motion or text object, and with counts
(`d3w`, `3dd`, `2d3w`). `x X D C s S Y r R p P gp gP u Ctrl+R U`,
`Ctrl+A` / `Ctrl+X` (increment / decrement the number at or after the
cursor, with a count).

**Text objects.** `iw aw iW aW`, `ip ap`, quotes `i" a" i' a' i\` a\``,
brackets `i( a( ib ab i[ a[ i{ a{ iB aB i< a<`: `ciw`, `ci(`, `dap`,
`yi"`, and so on.

**Insert entry.** `i a I A o O gi gI`.

**Registers.** `"a` to `"z`, `"A` to `"Z` (append), the unnamed register
`""`, the yank register `"0`, and the black hole `"_`. `.` repeats the
last change, with a new count if you give one.

**Macros.** `qa ... q` records, `@a` plays, `@@` repeats the last macro,
counts work (`5@a`).

**Marks.** `ma` to `mz` set, `'a` and `` `a `` jump. `'<` and `'>` are the
visual-area marks and `''` / ` `` ` return to the previous position.

**Search.** `/` and `?` with `n N`, and `* #` for the word under the
cursor. Up / Down on the `/` and `:` lines recall history.

**Ex commands.**

| Command | Meaning |
|---------|---------|
| `:s/pat/rep/flags` | Substitute. Flags `g i c I e &`. Also `&`, `g&` and `:&` repeat the last substitute. |
| `:g/pat/cmd`, `:g!/pat/cmd`, `:v/pat/cmd` | Run `cmd` on matching (`:g`) or non-matching (`:g!`, `:v`) lines |
| `:d` `:y` `:m` `:t` `:co` | Delete, yank, move, copy |
| `:normal keys` | Run normal-mode keys on each line of the range |
| `:w` `:q` `:q!` `:wq` `:x` | Save, leave Edit mode, discard, save and leave |
| `:N` | Go to line N |

Ranges: `N`, `.`, `$`, `%`, `.+N`, `.-N`, `$-N`, `'a`, `'<`, `'>`, and
`a,b` pairs of these. One `:s`, `:g` or `:v` is a single undo step.

## Differences from vim

tdb tries to match vim (the engine is checked against real nvim by a
golden test suite), but these differences are known.

**Regular expressions are Python `re`.** Patterns in `/`, `?`, `:s`, `:g`
and `:v` are case-sensitive by default; use `(?i)` at the start of a
pattern, or the `i` flag of `:s`, to ignore case. A translation table maps
the common vim spellings: `\(...\)` becomes `(...)`, `\<word\>` becomes
`\bword\b`, `\{n}` becomes `{n}`. `~` (previous replacement) is not
supported. `re.MULTILINE` is always on, so `^` and `$` match per line, but
`\s` also matches a newline. Python's `\w` differs from vim's `iskeyword`
for some non-ASCII text. There is no timeout, so a pattern with
catastrophic backtracking freezes the UI while you type it.

**Search.** No search offsets (`/foo/e`, `/foo/+1`); a `/` inside a
pattern is literal.

**Substitute.** `&` in a replacement is a literal `&`; use `\g<0>` for the
whole match (`\0` is not supported). Python templates `\1` and `\g<name>`
work, and `\n` / `\r` split the line. Flags `n p # l r` and the count
argument are not supported. A trailing zero-width match with `g` behaves
differently at end of line. Counts are reported only for three or more
substitutions. `:s///c` inside `:g` is refused.

**Ex.** No `;` range separator, no `/pat/` addresses, no `|` chaining, and
`:w` takes no file name. A ranged `:w` is refused. `:x` always saves.
`:g` with no command (print) is unsupported. `:normal` nests at most 20
deep, and parses `<Esc>`-style key names that vim would treat as literal
text. A `:normal` that changes nothing leaves no undo step. There is no
"N fewer lines" message. `:set` does not exist. Registers holding `:`
lines are not run as ex commands by `@a`, and there is no `Ctrl+V` on the
command line.

**Failed motions do not abort.** A failed motion does not stop the rest of
a `:normal`, a macro or `.`: `:normal jdd` on the last line deletes it.
`2dd` past the end of the file deletes the last line (vim leaves the text
alone). `d}` does not apply vim's exclusive-to-linewise rule, and `VjX`
does nothing.

**tdb keys win.** `Ctrl+V`, `Ctrl+O`, `Ctrl+E`, `Ctrl+B`, `Ctrl+C`,
`Ctrl+S`, `Ctrl+Q`, `Ctrl+P` and all function keys are tdb pane keys and
never reach the editor, so block visual mode (`Ctrl+V`), scroll lines
(`Ctrl+E`) and page-up (`Ctrl+B`) are unavailable; use `PageUp`. `Ctrl+C`
cannot cancel the `:s///c` prompt; use `q` or `Esc`. `Alt+letter` menu
shortcuts need `Esc` first in insert mode.

**Registers.** No numbered registers `"1` to `"9`, no small-delete `"-`,
and no clipboard registers: `"+` and `"*` show "Clipboard registers are
not supported" and change nothing.

**Marks.** Only lowercase marks, kept per file for the run of tdb. Undo
does not bring back a mark deleted along with its line. `'[ '] '. '^ '"`
are not supported. `dd` on the first of two identical adjacent lines moves
the other line's mark.

**Text objects and editing.** `i"` / `a"` ignore counts, and brackets
inside quotes are not special-cased. No tag objects (`it`, `at`) and no
sentence objects (`is`, `as`). Decimal `Ctrl+A` / `Ctrl+X` do not wrap at
64 bits. `r<Tab>` does nothing. `{count}gi` and `{count}gI` do not repeat
the insert. In insert mode, `Ctrl+W` on punctuation and `<CR>` inside
leading whitespace (autoindent) behave differently from vim. Typing into
an empty buffer leaves no final newline. `:g/x/normal j` resets the wanted
column, and an error in a `:g` nested through `:normal` does not stop the
outer `:g`.

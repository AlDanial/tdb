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
`% { }`, `+ -`, `_` (without a count or operator), `gj gk`, `H M L`,
`PageUp` / `PageDown`, and `'a` / `` `a `` / `''` / ` `` ` mark jumps. A
count prefix works on all of them except `_`. `H M L` and `PageUp` /
`PageDown` need a rendered editor (they act on the visible window).

**Operators.** `d c y > < g~ gu gU`, doubled for lines (`dd`, `cc`, `yy`,
`>>`, `gUU`), with any motion or text object, and with counts (`d3w`,
`3dd`, `2d3w`). `J gJ` (with a count), `~`, `x X D C s S Y r R p P u
Ctrl+R`, and in visual mode `d c y > < ~ u U J`.
`Ctrl+A` / `Ctrl+X` increment / decrement the number at or after the
cursor, with a count (decimal, hex `0x`, negative).

**Text objects.** `iw aw iW aW`, `ip ap`, quotes `i" a" i' a' i\` a\``,
brackets `i( a( ib ab i[ a[ i{ a{ iB aB i< a<`: `ciw`, `ci(`, `dap`,
`yi"`, and so on.

**Insert entry.** `i a I A o O`.

**Registers.** `"a` to `"z`, `"A` to `"Z` (append), the unnamed register
`""`, the yank register `"0`, and the black hole `"_`. `.` repeats the
last change, with a new count if you give one.

**Macros.** `qa ... q` records, `@a` plays, `@@` repeats the last macro,
counts work (`5@a`). As in vim, a command that fails stops the rest of a
macro, of a `.` repeat and of a `:normal` (a motion at the edge of the
buffer, `f` or a search that finds nothing, `2dd` on the last line, an
unknown key), so `qajddq99@a` stops at the end of the file. In `:g`, a
`:normal` that fails on one line stops only that line. When you type keys
yourself a failure does not stop the keys after it, and tdb does not beep.

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
and `:v` are passed to Python's `re` as written, with no translation of
vim syntax: write groups as `(...)` (not `\(...\)`, which matches a
literal parenthesis), word boundaries as `\b` (not `\<` `\>`) and counts as
`{n}` (not `\{n}`). Patterns are case-sensitive by default; use `(?i)` at
the start of a pattern, or the `i` flag of `:s`, to ignore case. `~`
(previous replacement) is not supported. `re.MULTILINE` is always on, so `^` and `$` match per line, but
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

**Not implemented.** The operators `=` (reindent), `gq` and `gw`
(format), `g?` (rot13), `g@` and `zf` (fold): they take their motion,
change nothing and show "Not supported". `!{motion}` opens the `:` line
with the range, as vim does, but shell commands and filters (`:!`) are
refused. Any other key or key pair tdb does not know (`g8`, `ga`, `zj`,
`[q`, `ZZ`, `q:`, `Ctrl+W j`, `K`) shows "Not supported", cancels the
command and its count, and is a failure (it stops a macro); the keys after
it are never run as part of it. Also not implemented: `U` (undo line),
sentence motions `( )` and sentence objects `is as`, tag objects `it at`,
`[{ ]}`-style unmatched-bracket motions, `gi` and `gI` (they behave like
`i` and `I`), `gp` and `gP` (like `p` and `P`), `g*` and `g#`, and `p` in
visual mode (it does nothing). `_` with a count or an operator does
nothing. `~` toggles a single character (there is no `tildeop`). `d}`
does not apply vim's exclusive-to-linewise rule, and `VjX` does nothing.
`ci(` before the brackets fails: nvim searches forward for the next pair.

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
inside quotes are not special-cased. Decimal `Ctrl+A` / `Ctrl+X` do not wrap at
64 bits. `r<Tab>` does nothing. In insert mode, `Ctrl+W` on punctuation and `<CR>` inside
leading whitespace (autoindent) behave differently from vim. Typing into
an empty buffer leaves no final newline. `:g/x/normal j` resets the wanted
column, and an error in a `:g` nested through `:normal` does not stop the
outer `:g`.

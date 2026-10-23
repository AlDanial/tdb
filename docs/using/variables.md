# Variable Inspection

The Variable View shows a tree of scopes with all variables in the current frame. The scopes
themselves are language-dependent: Locals, Globals, plus Environment for bash; Lexicals,
Globals, Specials for Perl (see the [Bash](../languages/bash.md) and [Perl](../languages/perl.md) pages for
details). Expand nodes to drill into complex objects.  Children are loaded lazily on demand.
Variable values can be changed in the Evaluate Console.

Double-click a variable, or highlight the variable with the text cursor in
the Variables View and press `Enter`
to display that variable in a modal.  This simplifies inspection of
large or deeply nested data structures.

## Display list

Like `gdb`'s `display` command, a variable (or any expression) can be
pinned so its value is re-read at every stop. Pinned entries appear
under a **Display** scope at the top of the Variables View, but only
while they are defined in the current frame: step out of the function
that owns `b` and the entry vanishes, step back into it and the entry
returns. Two ways to pin, two ways to unpin:

| | Evaluate Console | Variables View |
|---|---|---|
| pin | `display b` | right-click the variable's row |
| unpin | `undisplay b` | right-click its row under **Display** |

```
>>> display b
```

```
▼ Display
    b = 6
▼ Locals
    a = 3
    b = 6
```

Right-clicking a child of an expanded object pins the full path
(`obj.count`, `items[2]`), and a bare `display` lists everything pinned.
Left-click works the same under **Display** as in every other scope:
it expands or collapses an object, and a double-click opens it in a modal.
A click in the Variables View is recorded exactly as the typed command,
so `--record`/`--replay` sessions reproduce it. Some terminals keep
right-click for their own paste or context menu (Windows Terminal and
the VS Code terminal do by default); use the `display` command there.

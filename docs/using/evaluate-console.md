# Evaluate Console

A read-evaluate-print loop (REPL) at the bottom left permits
interactive evaluation of expressions in the current scope:

```
>>> len(items)
42
>>> sorted(data, key=lambda x: x.priority)[:3]
[Item(priority=1), Item(priority=2), Item(priority=3)]
```

- **Up/Down arrows** cycle through expression history
- **Tab** triggers DAP-based completion
- **Trailing `?`** shows help (signature + docstring):

```
>>> os.path.join?
(a, *p) : Join two or more pathname components...
```

Variable values set here are reflected in the running code.
The Variables View redraws after every entry, so an assignment typed at
the prompt shows up immediately rather than at the next stop.

## Interactive variables

A variable *created* at the prompt is tracked and listed under an extra
**Interactive** scope in the Variables View, alongside Locals, Globals and
the other language-specific scopes. The value is re-read at every stop,
so it works like a watch list of the names you introduced:

```
>>> newvar = 42
```

```
▼ Locals
    a = 3
    b = 6
▼ Interactive
    newvar = 42
```

What counts as "created" depends on the language, and so does how long the
variable lives:

| Language | Creates an interactive variable | Lifetime |
|---|---|---|
| Python | `name = …` | the current frame (gone once it returns) |
| Perl | `$name = …`, `@name = …`, `%name = …`, `our …` | the whole run (`my` lexicals vanish with the eval and are not tracked) |
| Bash | `name=…`, `local`/`declare`/`export …` | the whole run; `local` follows the function |
| tcsh | `set name …`, `setenv NAME …` | the whole run |
| Ruby | `name = …`, `$name = …`, `@name = …` | globals and instance variables persist; plain locals live only in the eval's binding and read back as unavailable after the next step |
| PowerShell | `$name = …`, `$global:name = …` | the function scope, or the whole run for `$global:` |
| C/C++/Fortran/Rust/OCaml under gdb | `set $name = …` (a gdb convenience variable) | the whole run |
| C/C++/Fortran/Rust/OCaml under lldb-dap | `int $name = …` (an lldb persistent variable) | the whole run |
| Go (dlv), OCaml under ocamlearlybird | not possible: their evaluate cannot create variables | — |

A tracked name that no longer resolves (a Python local after its frame
returned, for example) stays in the list and shows as `<unavailable>`.

## Cut / Paste

Expression for the Evaluate Console are often copied from the Code View.
Doing this in `tdb` differs from traditional terminal behavior, because `textual` applications
capture mouse events for their own use.

Instead, hold the `Shift` key while performing your conventional cut/paste keystrokes or mouse
operation to get the expected behavior.

# Tcsh

`tdb` bundles its own tcsh adapter (`tcsh-tdb`) so no separate adapter
is needed, just a stock `tcsh` on `PATH` (or
`{"adapters": {"tcsh": "/path/to/tcsh"}}` in `config.json`). Stock tcsh has
no debugger hooks, so the adapter instruments a temporary copy of the script
(and of any literal `source`d files), runs it with `tcsh -f`, and
coordinates stops through private FIFOs. tdb always shows the original
source paths and line numbers; the generated copies are private adapter
details and are cleaned up when the session ends.

**Launching a script:**

```bash
tdb script.csh
```

Core debugging works as described in [Multi-Language Debugging](index.md): line breakpoints, stepping
(`next` steps over `source`d files, `stepIn` steps into instrumented ones),
stack navigation across `source` nesting, variable inspection, and the
evaluate console with the v1 caveats listed in
[What works for non-Python languages](index.md#what-works-for-non-python-languages),
most notably: no conditional breakpoints, no pause of a free-running
script, and stepping is per logical source line. `--terminal` is supported
(see [External Terminal Support](../using/external-terminal.md)).

The Variables view shows four scopes for tcsh: Shell Variables (`set`),
Environment (`setenv`), Aliases, and Arguments (`argv`). All of them show
the live state of the single tcsh process. The evaluate console executes
text directly in the paused shell. It can inspect *and mutate* state
(`set name = value` takes effect immediately), and a syntax error in
evaluated text can terminate the debuggee; there is no isolation.

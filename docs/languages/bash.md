# Bash

`tdb` bundles its own bash adapter (`bash-tdb`) so no separate adapter install
needed, just a `bash` ≥ 4.4 on `PATH`. It drives stock bash's own `DEBUG`
trap (with `extdebug` for return-value control) under the hood via a small
harness script sourced through `BASH_ENV`, so it works with any bash already
on the system.

**Launching a script:**

```bash
tdb script.sh
```

Core debugging works as described in [Multi-Language Debugging](index.md) (breakpoints, stepping,
continue/pause, stack, variables, evaluate console), with the v1 caveats
listed in [What works for non-Python languages](index.md#what-works-for-non-python-languages).
Most notably, a breakpoint can't be set on a `func() {` line itself (the
`DEBUG` trap never fires there), stops never land inside subshells or
pipeline segments, child bash processes run uninstrumented, and only the
innermost frame's locals are inspectable. There is no remote-attach mode for
Bash. `--terminal` is supported (see
[External Terminal Support](../using/external-terminal.md)).

The Variables view shows three scopes for bash: Locals (innermost frame
only), Globals (unexported shell variables), and Environment (exported
variables, both inherited and script-`export`ed).

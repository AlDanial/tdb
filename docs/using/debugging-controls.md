# Debugging Controls

Keybindings for stepping, continuing, pausing, and stack navigation match
those for gdb/pdb, with some aliases and extras thrown in for convenience.

| Key | Action |
|-----|--------|
| `n` | Step over (next statement) |
| `s` | Step into function call |
| `o` / `f` / `r` | Step out of current function (also aliased as "finish" and "return") |
| `c` | Continue execution |
| `p` | Pause a running program |
| `t` | Run to cursor position |
| `u` / `d` | Navigate stack up (caller) / down (callee) |
| `j` / `k` | Move cursor down / up (with count: `5j`, `10k`) |
| `G` | Go to last line (with count: `42G` jumps to line 42) |
| `e` | Re-display the last error (traceback) |
| `R` | Restart the debug session |
| `q q` | Quit |
| `Ctrl+q` | Quit |

> **Note:** `f` ("finish") and `r` ("return") are both aliases for step-out. DAP's only
"exit-a-function" primitive is `stepOut`, which runs the rest of the current function
normally and stops at the return point. A true gdb-style immediate-return (skipping
remaining code in the function without executing side effects) is not supported by DAP/debugpy.

**Step granularity (statement vs. line):** by default, `n` (step over) and `s` (step into)
treat a multi-line source statement as a single step. For example, stepping over

```python
results = await asyncio.gather(
    fetch(1, 2),
    fetch(2, 1),
    fetch(3, 3),
)
print(results)  # next stop lands here, not on each sub-line above
```

lands on `print(results)`, not on each interior sub-line of the `gather` call. Switch to
**Line** mode (Configure > Step Mode) to get debugpy's native per-line behavior, which
stops on each physical line--useful for inspecting how a complex expression is built up.
The choice is saved to `~/.config/tdb/config.json`.

Statement mode requires a source-language model and is currently Python-only;
other languages step at their debugger's native granularity — per line,
except PowerShell, whose debugger stops per statement, see
[PowerShell](../languages/powershell.md) (the Step Mode menu says so if you try).

# Remote Attach

Remote attachment is useful in situations where you can't launch the debuggee directly
with `tdb`, for example, if it is launched from another program or runs in an environment
where you can't install `tdb`.  Two requirements must still be met though:
1. the `debugpy` package must be installed in the debuggee's Python environment
2. you need write access to the debuggee's code to add the following code at the point
where you want to attach the debugger:

```python
# In the target program:
import debugpy

debugpy.listen(("0.0.0.0", 5678))
print("Waiting for tdb to attach on port 5678...")
debugpy.wait_for_client()  # optional: pause until debugger connects
print("tdb is attached!")
```

When the debuggee runs and hits the `debugpy.wait_for_client()` line, it starts a
debugpy server listening on port 5678.
Attach `tdb` to it with the `-r` switch, specifying the host and port.
If the debuggee is on the same machine, you can omit the host or use `localhost`.
This example assumes the debuggee runs on 192.168.1.10 and listens on port 5678:

```bash
# Attach from tdb:
tdb -r 5678   # to localhost
tdb -r 192.168.1.10:5678

# With breakpoints:
tdb -r 5678 -k my_program.py:42
```

All debugging features (breakpoints, stepping, variable inspection, threads, processes,
async tasks) work in remote attach mode. The Code View automatically navigates to the
source file when the program stops.

**Mapping remote paths to local copies (`--local-root` / `--remote-root`):** when the
debuggee lives on another machine, or in a container, or simply in a different directory
on the same machine, the source paths it reports (and the paths it expects breakpoints
to refer to) won't match anything on the `tdb` host. To bridge that gap, give `tdb` one
or more `--local-root` / `--remote-root` pairs. Each `--local-root` points at a local
directory containing a copy of the code; each `--remote-root` is the corresponding path
on the debuggee. The two flags must be supplied in equal numbers and are paired in CLI
order via `zip()`, so the first `--local-root` matches the first `--remote-root`, the
second matches the second, and so on. `debugpy` then translates paths bidirectionally:
breakpoints set on a local file land on the matching remote file, and source paths
returned in stopped-events / stack-traces are rewritten back to the local copy so the
Code View loads directly from disk (no DAP `source` round-trip needed).

These flags are required whenever you want to set a `-k` breakpoint against a remote
debuggee whose code lives at a different path than your local copy. For example, if the
remote runs `program.py` at `/path/to/code/program.py` and your local copy is at
`/local/project/code/program.py`, set a breakpoint at line 321 with:

```bash
tdb -r RHOST:15678 \
    --local-root /local/project/code \
    --remote-root /path/to/code \
    -k program.py:321
```

With `--local-root` set, a relative `-k FILE:LINE` is resolved by searching each
`--local-root` directory in CLI order (first match wins); absolute paths still work as
before. Multiple pairs can be supplied to mirror multiple source trees (e.g. an
application directory and a shared library directory) in one invocation.

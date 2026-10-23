# Process Inspector

For programs using `multiprocessing`, the menu bar shows a **Processes (N)** label when there
are 2 or more child processes. Click it to open a modal with:

- **Left pane**: list of child processes with PID, name, and status (alive/exited)
- **Right pane**: process details, full stack trace, and expandable variable tree for the selected process

`tdb` automatically attaches to child processes spawned via `multiprocessing.Process`, `multiprocessing.Pool`,
or `concurrent.futures.ProcessPoolExecutor`. Breakpoints set in the parent are propagated to all
child processes. When any process hits a breakpoint, all other processes are paused. Pressing `p`
pauses all processes; `c` continues all.

**Stepping in multi-process programs:** step commands (`n`, `s`, `o`, `f`, `r`) apply only to the
process whose stack is currently shown in the Code View (the one that hit the breakpoint).
Other processes remain paused throughout the step. To step in a different process, open
the Processes tab and select it first. The Code View then switches focus to that process,
and subsequent step commands operate on it.

RPC equivalents:

```bash
# List all child processes
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"list_processes","params":[]}'

# Inspect a specific process by name or PID
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"inspect_process","params":["ForkPoolWorker-1"]}'
```

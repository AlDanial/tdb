# Function Tracing

`tdb --trace` records every call to the functions you name, in your Python program and in every
Python process it starts, and writes the result as a
[Perfetto](https://ui.perfetto.dev) trace. Nothing is debugged: the program runs at full speed
with no breakpoints and no TUI, and tdb only records. Use it to see how threads, `asyncio`
tasks and several services interleave in time.

```bash
tdb --trace SPEC.yaml PROGRAM [-- PROGRAM-ARGS]    # trace one program
tdb --trace-merge A.jsonl B.jsonl [--trace-out BASE]   # weave several captures into one trace
tdb --trace-discover OUT.yaml PROGRAM              # write a starter spec
```

Put `--` before the program's own options so tdb does not try to read them.

## The spec file

A spec is a YAML mapping from source files to the functions to record in them:

```yaml
# Keys are paths relative to this file, globs, or dotted module names.
orders/workers.py:
  - process_order                  # a plain name
  - Cart.add                       # Class.method; "Cart::add" works too
  - handle_*                       # fnmatch patterns
  - charge_card:                   # a one-key mapping adds options
      capture: [order.id, amount]  # expressions evaluated at entry
      return: true                 # also record the return value
      if: "amount > 100"           # record only when this is true
      max_hits: 50                 # stop recording after 50 calls
"orders/**/*.py":                  # a glob matches many files
  - "*.run"
orders.billing:                    # a module name; found wherever it is imported from
  - invoice
```

- A key containing `/` or `\`, or ending in `.py`, is a path, relative to the spec file's
  directory. A bare dotted name is a module.
- `capture` expressions see the function's arguments and globals. A value is a `repr` string cut
  to 200 characters; Python's `reprlib` limits apply, so lists stop after 6 items, dicts after 4,
  and dict and set elements are sorted. An error while evaluating a value shows as
  `<error: Type: message>`, or `<error: Type>` when the message is empty.
- Every problem in a spec is reported in one pass before the program starts: files that do not
  exist, names that match no function (with a "did you mean" hint), expressions that do not
  compile, and unknown options.
- After the run, tdb lists entries that were never called (`tdb: never called: ...`).

## Launch forms

There are three:

```bash
tdb --trace spec.yaml prog.py -- --port 8000        # a .py file
tdb --trace spec.yaml -m orders.main -- --port 8000  # a module
tdb --trace spec.yaml ./run-service.sh               # any command
```

- With a `.py` file or `-m`, `--python` and `--pv` choose the interpreter. A bare
  `--python NAME` is looked up on `PATH`; a value with a directory part is made absolute against
  tdb's current directory. With `--cwd`, `--pv` (`.venv/bin/python`) is still resolved under
  tdb's current directory, not under `--cwd`.
- A command is run as is. A script with a Python shebang uses its own interpreter.
- The program runs under the path you typed, so `sys.argv[0]` and `__file__` are unchanged. With
  `--cwd` the path is made absolute, but symlinks are not resolved.
- `--trace-name NAME` sets the service name used in file names and lane labels (default: the
  program's file stem, or the module name). `--trace-out BASE` sets the output base path.
  `--trace-max-events N` caps events per process (default 5,000,000).

Child processes (`multiprocessing`, `subprocess`, `os.fork`) are traced too, as separate lanes
of the same service.

### Signals and exit status

- Ctrl-C and Ctrl-\ reach the program exactly as they would without tdb. tdb survives them, and
  prints `tdb: waiting for NAME to exit` once.
- SIGTERM and SIGHUP sent to tdb are passed on to the program. A signal tdb was started with
  ignored, for example under `nohup`, stays ignored and is not passed on. When the terminal
  hangs up, the kernel already signals the program, so tdb does not pass the SIGHUP on
  (unless tdb is the session leader).
- tdb exits the way the program did. If the program died from signal N, tdb dies from signal N,
  so shells and `subprocess` see the same result as untraced. The exceptions are signals that
  dump core, and Windows: there tdb exits with 128+N.
- The capture header's `exit_code` records 128+N for a death by signal.

## Walkthrough: two services and a merge

`examples/Python/trace/` holds two small services built on `asyncio` streams and the standard
library only, a driver, and one spec for both:

- `inventory_service.py` answers `CHECK sku qty` and looks the stock up on a thread pool whose
  threads are named `db_0`, `db_1`, ...
- `orders_service.py` answers `ORDER sku qty` by asking the inventory service, and has a daemon
  thread named `heartbeat`.
- `send_orders.py` sends orders concurrently.
- `checkout.yaml` names the handler, client and executor functions of both.

In the commands below, `EX` stands for `<examples dir>/Python/trace`, where `<examples dir>` is
the "examples dir" row that `tdb --info` prints (in a checkout, `EX` is `examples/Python/trace`).

Terminal 1, the inventory service. `--port 0` picks a free port and the service prints it:

```bash
tdb --trace $EX/checkout.yaml --trace-name inventory --trace-out inv \
    $EX/inventory_service.py -- --port 0 --max-requests 4
# listening on 43687
```

Terminal 2, the orders service, pointing at the inventory port:

```bash
tdb --trace $EX/checkout.yaml --trace-name orders --trace-out ord \
    $EX/orders_service.py -- --port 0 --inventory-port 43687 --max-requests 4
# listening on 39433
```

Terminal 3, the driver, using the orders port:

```bash
python $EX/send_orders.py --port 39433 --count 4
# order 1 (apple x1): OK
# order 2 (pear x2): OK
# order 3 (plum x3): NO
# order 4 (fig x4): NO
```

Both services exit by themselves after four requests (`--max-requests`); to stop a service
earlier, press Ctrl-C in its terminal and tdb still writes the trace. Each tdb then reports:

```
tdb: trace "inventory": 1 process, 5 threads, 4 tasks, 8 calls
tdb: wrote inv.jsonl
tdb: wrote inv.perfetto.json  (open in https://ui.perfetto.dev)
tdb: never called: orders_service.py: handle_order
...
```

The `never called` lines appear because both services share one spec: each lists the other's
functions too. They are harmless.

Merge the two captures into one trace:

```bash
tdb --trace-merge inv.jsonl ord.jsonl --trace-out all
# tdb: wrote all.perfetto.json  (open in https://ui.perfetto.dev)
```

Open `all.perfetto.json` in <https://ui.perfetto.dev> (Open trace file). You get two groups,
`inventory · MainProcess (pid)` and `orders · MainProcess (pid)`. Orders' four `handle_order`
tasks run side by side, each waiting inside `ask_inventory` while an inventory `handle_client`
task hands its `lookup` to a `db_N` thread.

## Reading the trace

- Each service is a group of lanes, one per process, named `service · process (pid)`.
- Each thread gets a lane. Each `asyncio` task gets its own lane, named like
  `Task-7 · MainThread`.
- Calls are bars. A coroutine running inside an `asyncio` task is one bar for its whole life,
  with `(suspended)` child bars while it waits. Select the bar to see where the time went;
  grey out `(suspended)` to see only the time spent running.
- Generators, async generators, and coroutines that are not in an `asyncio` task get one bar per
  run, each marked with a `run` argument.
- A bar's details show `file` (with line), `call_id`, captured values, the return value if
  requested, and `wall`, the local wall-clock time the call began. Calls that raised
  show a `raise` marker, and crashes and interruptions show their own markers.
- The time axis is relative to the start of the trace. Wall-clock time is in each bar's `wall`
  arg, and the trace metadata holds the absolute start.
- `args.clipped` marks a bar that was trimmed so that the lane stays nested, and `args.unfinished`
  a call still running when the program ended. Their real timing differs slightly.
- Merged services appear in the order you gave the captures. Two captures with the same
  service name get `#2`, `#3` suffixes. Merging captures from different runs works, but tdb
  warns when their time ranges do not overlap and they land far apart on the axis.

!!! note
    Perfetto's importer is picky. A one-time manual check that a trace of your own opens with no
    import errors in ui.perfetto.dev is worthwhile.

## Discovery

You do not need to write a spec by hand:

```bash
tdb --trace-discover spec.yaml prog.py -- --port 8000
```

runs the program, counts the calls it makes, and writes a spec of them. Remove what you do not
need, then `tdb --trace spec.yaml prog.py`. Output looks like:

```yaml
inventory_service.py:
  - lookup         # 1 call
  - handle_client  # 1 call, async
  - main           # 1 call, async
```

- Only calls in your own code are counted: not the standard library, not site-packages and not
  dist-packages, apart from the launched program's own file and its folder. An editable install
  counts as your code.
- Counting stops at 10,000 calls per function; such a line reads `≥10,000 calls  (hot)`. Hot
  functions make big traces, so think before keeping them.
- Only functions that the spec loader can find in their source files are written. Others
  (code compiled from templates, a zipapp's members, `type X = ...` values) are counted as
  omitted, with a note.
- tdb's own Python parses the sources, so under tdb on 3.11 a file that needs 3.12 syntax
  (the `type` statement, PEP 695 generics) defines nothing, and `--trace` rejects it too.
- A `-m` module installed in site-packages counts as library code, so discovery lists nothing
  for it. Run the module's file instead.
- Keys are relative to `OUT.yaml`'s directory; `--trace-name`, `--python`, `--pv` and `--cwd`
  work as for `--trace`.

## Output files and the capture schema

`--trace` writes, next to the current directory (or at `--trace-out`):

| File | Contents |
|---|---|
| `BASE.jsonl` | the capture: one JSON object per line |
| `BASE.perfetto.json` | the Perfetto trace for this one service |

Both are written atomically. The default `BASE` is `NAME-YYYYmmdd-HHMMSS`. While the program
runs, per-process event files go in `BASE.parts/`, which is deleted after both outputs are
written. If tdb itself is killed, or a write fails, `BASE.parts/` is kept, and
`tdb --trace-merge BASE.parts --trace-out BASE` turns it into a trace. `--trace-merge`
writes only `BASE.perfetto.json` (default `merged-YYYYmmdd-HHMMSS`), and takes `.jsonl` files and
`.parts` directories.

The capture is JSON Lines, sorted by `ts` (epoch nanoseconds). Line 1 is the header:

```json
{"type":"capture","version":1,"name":"inventory","tdb":"0.3.1","command":[...],"cwd":"...",
 "spec":"...","host":"...","start_ns":0,"end_ns":0,"exit_code":0}
```

Then one `process` record per process:

```json
{"type":"process","pid":505143,"ppid":505141,"name":"MainProcess","argv":[...],
 "python":"3.12.8","start_ns":0,"end_ns":0,"ended":"clean"}
```

`ended` is one of:

- `clean`;
- `abrupt`: no exit record, so the process was killed or crashed hard;
- `signal:SIGNAME`, for the main process;
- `exec`: the process ran `exec` into another traced Python program, which gets a second
  process record with the same pid.

Then the events. Every event has `type`, `ts`, `pid`, `process`, `tid`, `thread`, `task_id` and
`task` (null outside a task), plus:

| `type` | Extra fields |
|---|---|
| `call` | `file`, `function`, `line`, `kind` (`function`, `coroutine`, `generator`, `async_generator`), `call_id`, `values` (expression to `repr`, or null) |
| `return` | `call_id`, `value` (or null) |
| `raise` | `call_id`, `exception` (`"ValueError: bad"`) |
| `suspend`, `resume` | `call_id` |
| `crash` | `exception`, `where`, `traceback` |
| `note` | `message` |

`call_id` is unique per pid. Because the capture is plain JSON Lines, `jq` and a few lines of
Python are enough to build your own reports.

## Limitations

- Traced programs need Python 3.12 or newer (tdb itself runs on 3.11). Free-threaded builds
  running without the GIL are detected and skipped.
- Child processes that bypass the start-up hook are invisible: interpreters started with `-I`,
  `-E` or `-S`, or with a replaced environment that drops `PYTHONPATH`. Processes that load the
  hook but cannot be traced (Python below 3.12, free-threaded) are reported as skipped.
- A hard crash or `kill -9` loses at most the last 0.25 to 0.5 s of events. A process killed by
  SIGTERM, for example `Pool.terminate()` workers, loses the same.
- On Linux, `os.fork()` from a traced program shows no "multi-threaded fork"
  DeprecationWarning. On macOS it may still show one, for direct `os.fork()` calls from
  `__main__`; this is unverified.
- A SIGINT that lands inside the tracer's fork hooks is reported by Python as "Exception
  ignored" and dropped. This is a CPython limitation; the window is about 0.5 ms per fork.
- A group-wide `kill -TERM -PGID` reaches the program twice: once directly and once passed on by
  tdb. A SIGINT sent to tdb's pid alone is not passed on.
- The tracer wraps `sys.excepthook` and `threading.excepthook` to record crashes, so
  `sys.excepthook is sys.__excepthook__` is false in a traced program. A hook the program
  installs later without chaining hides crash records; the crash still shows as `raise` events
  and in the exit status.
- `asyncio` is the only async framework with per-task lanes. trio, curio and gevent code is
  traced under its thread.
- Perfetto ignores the trace's `displayTimeUnit`, which is harmless.
- Windows support is implemented but has not been run.

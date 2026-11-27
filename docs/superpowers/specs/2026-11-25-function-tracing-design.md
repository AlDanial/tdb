# Function tracing (`tdb --trace`) — design

Date: 2026-11-25. Status: approved. Branch: `function-tracing`.

## Intent

Help a developer understand what a running program is doing, especially
code that mixes asyncio tasks, threads and processes, by recording every
entry into (and exit from) functions the developer names, and showing
the result as swim lanes in Perfetto (ui.perfetto.dev).

The motivating use: several interacting Python microservices, each with
asynchronous and threaded parts. During development all services run on
one machine, each started by hand in its own terminal. The developer
traces **each service separately** (one `tdb --trace` per terminal),
drives a few chosen interactions for a few minutes, stops the services,
then **weaves the per-service captures into one "super swimlane"** view.
Kubernetes deployment is the eventual target for the services, but not
for this tool now.

## Goals

1. `tdb --trace SPEC.yaml PROGRAM [-- ARGS]` launches one service with an
   in-process tracer and records, for each call of a spec'd function:
   entry, normal return, exit by exception, and (for coroutines and
   generators) each suspend and resume.
2. Every event carries: timestamp as epoch nanoseconds; process (name +
   pid); thread (name + id); asyncio task (name + id) when in a task;
   source file; function qualified name.
3. Child processes of the service (multiprocessing with any start method,
   `subprocess` running Python, `os.fork`) are traced too, each as its
   own process group of lanes.
4. When the service ends — clean exit, Ctrl-C, uncaught exception, kill
   by signal, segfault — tdb writes two files:
   - a **capture** (`BASE.jsonl`, JSON Lines) holding every event, and
   - a **Perfetto trace** (`BASE.perfetto.json`) to open at
     ui.perfetto.dev.
5. `tdb --trace-merge CAPTURE... ` weaves several captures (one per
   service) into one Perfetto trace, all services on one timeline.
6. Optional per-function capture of variable values at entry, of the
   return value, a recording condition, and a hit limit.
7. `tdb --trace-discover OUT.yaml PROGRAM [-- ARGS]` runs the program and
   writes a ready-to-edit spec listing every user-code function that was
   called, with call counts.
8. Low overhead and low perturbation: the program's threads and tasks
   must interleave as they would without tdb.

## Non-goals (this version)

- Languages other than Python. (The capture format, Perfetto writer and
  merge are language-neutral so Ruby/Perl/PowerShell/Bash/native tracers
  can be added later and reuse them; see "Later languages".)
- Python older than 3.12 in the traced program (the tracer uses
  `sys.monitoring`). tdb itself still runs on 3.11+.
- Attaching to an already-running process (`-a PID`).
- Multi-host / Kubernetes tracing and clock alignment between machines.
- Arrows linking a call in one service to the handler in another
  (needs a request ID carried over the wire).
- Opening the TUI when the traced program crashes.
- A hand-built HTML report, and YAML output. (The spec *input* stays
  YAML.)
- Async frameworks other than asyncio (trio, curio, gevent greenlets):
  their code is traced, but appears under its thread, not per task.
- Line-level tracing; turning recording on and off mid-run.
- Perfetto's protobuf trace format (see "Perfetto output" for why JSON).
- Free-threaded Python builds running without the GIL (detected and
  reported as skipped).

## Anti-goals (would be failures even if it "works")

- Tracing that stops or serializes threads/tasks, or slows untraced code
  noticeably, so the interleaving shown is not the real one.
- The traced program behaving differently because of tdb: an exception
  from the tracer reaching user code, a changed `sys.argv`, `__main__`,
  `sys.path[0]`, stdout/stderr, exit code, or Ctrl-C behavior.
- A silently empty or partial trace. Typos in the spec, child processes
  that loaded the hook but could not be traced, too-old interpreters and
  dropped events must all be reported. (Children that bypass the hook
  completely are a documented limitation; see below.)

## Constraints

- Cross-platform: Linux, macOS, Windows (CLAUDE.md). `os.fork` handling
  is POSIX-only; everything else must work on Windows.
- The tracer runs inside the user's interpreter, which may not have tdb
  or any third-party package installed (`--python`/`--pv` interpreters,
  venvs). It must be **standard-library only** and must **not import
  tdb**.
- tdb's existing conventions: the debuggee inherits the shell's cwd
  unless `--cwd` is given; `--python` / `--pv` select the interpreter;
  program arguments that start with `-` go after `--`.
- PyYAML becomes a runtime dependency of tdb (today it is dev-only) to
  read the spec. *(My call; it is small and has wheels everywhere.)*
- A feature is done only when `uv run pytest` passes (CLAUDE.md).

## Approaches considered

1. **Standalone in-process tracer, no debugger (chosen).** tdb starts the
   service with a small stdlib-only tracer loaded at interpreter
   start-up; the tracer uses `sys.monitoring` on only the spec'd
   functions and writes events to per-process files; tdb supervises,
   then assembles the outputs. Lowest overhead and perturbation, simplest
   to build, child processes handled the way coverage.py and
   `opentelemetry-instrument` do it. Cost: a crash is recorded in the
   trace but does not open the TUI.
2. **Same tracer inside a normal debugpy session** (reuse `--run`).
   Gains crash-into-TUI. Costs: debugpy (pydevd) lives in every process
   with its own monitoring hooks, helper thread and subprocess patching,
   perturbing exactly the code under study; the tracer must be installed
   in each child through its debugger session; more work.
3. **Debugpy logpoints / `--eval`-style stop-and-continue.** Rejected:
   stop-and-continue suspends every thread on each hit (debugpy's
   suspend policy is `ALL` in DAP mode) and tdb's multi-process layer
   pauses sibling processes, so it destroys the interleaving; logpoints
   are line-based and cannot see a function's return, so no durations
   and no suspend/resume.

The user chose approach 1.

## User-facing behavior

### Commands

```
# one terminal per service
tdb --trace checkout.yaml --trace-name orders   --pv services/orders/main.py  -- --port 8001
tdb --trace checkout.yaml --trace-name payments --pv services/payments/main.py -- --port 8002

# later, weave them together
tdb --trace-merge orders-20261125-104212.jsonl payments-20261125-104215.jsonl \
    --trace-out checkout
```

New flags:

| Flag | Meaning |
|---|---|
| `--trace SPEC.yaml` | Trace PROGRAM using the spec. Requires a program. |
| `--trace-name NAME` | Service name used in output names and lane labels. Default: the program's file stem (`main` for `main.py`). |
| `--trace-out BASE` | Output base path. `--trace` writes `BASE.jsonl` and `BASE.perfetto.json` (default BASE `NAME-YYYYmmdd-HHMMSS` in the cwd); `--trace-merge` writes `BASE.perfetto.json`. |
| `--trace-merge INPUT...` | Merge captures into `BASE.perfetto.json` (default BASE `merged-YYYYmmdd-HHMMSS`). Takes no program. |
| `--trace-discover OUT.yaml` | Run PROGRAM and write a spec of called functions to OUT.yaml. |
| `--trace-max-events N` | Per-process cap on recorded events (default 5,000,000); events past the cap are dropped and the drop is reported. |
| `-m MODULE` | With `--trace` / `--trace-discover` only: run `python -m MODULE` instead of a PROGRAM (see below). Rejected in every other mode for now (*my call*; debugpy can launch modules, so normal debugging could adopt it later). |

`--trace-out` does not apply to `--trace-discover` (its output is the
named OUT.yaml).

Allowed with `--trace` / `--trace-discover`: `-m`, `--python`, `--pv`,
`--cwd`, `--lang` (must be `python`), `--config-dir`. Every other mode
flag (`--run`, `-e`, `-a`, `-r`, `-k`, `-t`, `--if`, `--record`,
`--replay*`, `--headless`, `--server`, `--mcp`, `--terminal`,
`--post-mortem`, `--test`, `--vi`, ...) is rejected with a
`--trace cannot be combined with X` error, following the pattern
`parse_args` uses for `--run`/`--eval`. `--trace`, `--trace-merge` and
`--trace-discover` are mutually exclusive.

Add a `ProfileCapabilities.tracer: str | None` field (`"python"` for the
Python profile), following the existing capability pattern. It gates the
trace modes wherever the language is known: a `.py` PROGRAM, `-m`, or an
explicit `--lang`. `--lang X` for a language whose `tracer` is `None` is
rejected: `--trace is not yet supported for X`.

### Three ways to name what runs

Trace modes do **not** run tdb's language detection
(`registry.detect`) on a command: it classifies every native executable
as C/C++ and `#!/bin/sh` wrappers as Bash, which would reject
`uvicorn.exe` on Windows and shell wrappers that start Python. Instead:

1. **PROGRAM is a `.py` file.** Run it with `--python`/`--pv` if given,
   else tdb's own interpreter (the same rule as normal `tdb PROGRAM`).
   Default `--trace-name`: the file stem.
2. **`-m MODULE`.** Run `INTERPRETER -m MODULE ARGS` with the same
   interpreter rule. There is no PROGRAM; every positional after `--`
   is an argument to the module
   (`tdb --trace s.yaml -m orders.main -- --port 8001`). Default
   `--trace-name`: the module name.
3. **PROGRAM is anything else** (e.g. `.venv/bin/uvicorn`, `uvicorn` on
   `PATH`, a shell script, a Windows `.exe` console-script launcher).
   tdb resolves it as a path, then by `PATH` lookup (`shutil.which`;
   new in trace modes), and executes it as is; the tracer loads in
   whatever Python it starts. A Python-shebang script therefore runs
   under its own shebang's interpreter, so a venv's console scripts pick
   up the venv. `--python`/`--pv` with this form is an error. Default
   `--trace-name`: the command's file stem (`uvicorn`). If the command
   is not Python at all, the run ends with the "zero traced processes"
   warning.

For forms 1 and 2, tdb probes the interpreter's version before launching
and refuses a Python older than 3.12 with a clear message. For form 3
there is no pre-check; the skipped-process marker reports a too-old
Python at the end.

### Running, stopping, exit code

- The program inherits tdb's terminal directly: stdin, stdout and stderr
  are not piped, and the program stays in tdb's process group (unlike
  `--run`, which isolates it). So Ctrl-C reaches the service exactly as
  it would without tdb, and its own shutdown runs (and is traced).
- tdb itself survives SIGINT while the program runs (prints one line:
  `tdb: waiting for orders to exit`). SIGTERM or SIGHUP sent to tdb is
  forwarded to the program. When the program's main process exits, tdb
  waits briefly (2 s, *my call*) for traced children to finish flushing,
  assembles the outputs, prints a summary, and exits with the program's
  exit code (on POSIX, 128+N for death by signal N).
- Summary, on stderr:
  ```
  tdb: trace "orders": 3 processes, 7 threads, 41 tasks, 18,204 calls
  tdb: wrote orders-20261125-104212.jsonl
  tdb: wrote orders-20261125-104212.perfetto.json  (open in https://ui.perfetto.dev)
  tdb: never called: services/payments/api.py: Refund.apply
  ```
  plus warnings for anything that makes the trace incomplete (see
  "Nothing silent").

## The spec file

YAML. A mapping from a **file key** to a list of **function entries**.

```yaml
# simple form (the original request's format, still valid)
path/to/module.py:
  - Function_a
  - ClassName::method1
  - ClassName.method2            # Python's own spelling also works
  - ClassB::__init__

# wildcards
services/orders/handlers.py:
  - handle_*
  - Cart::*                      # every method of Cart
services/common/**/*.py:         # glob over files
  - "*"                          # every function in each file

# module name instead of a path (for installed / editable packages)
orders.workers:
  - Worker.run

# long form: per-function options
services/orders/api.py:
  - checkout:
      capture: [request.path, cart.total, self.client_id]  # evaluated on entry
      return: true                                         # record the return value
      if: "request.method == 'POST'"                       # record only these calls
      max_hits: 500                                        # stop recording after 500
```

Rules:

- **File keys.** A key containing `/` or `\`, or ending in `.py`, is a
  path (glob patterns allowed, `**` recursive). Relative paths resolve
  against the **spec file's directory**, so a spec travels with the
  project. Any other key that is a dotted identifier is a **module
  name**, matched by path suffix (`orders/workers.py` or
  `orders/workers/__init__.py`) against the files the program executes.
- **Function names** match the code object's `co_qualname`. `::` and `.`
  are interchangeable separators. `<locals>` segments may be omitted
  (`outer.inner` matches `outer.<locals>.inner`). `*` and `?` wildcards
  (fnmatch) apply to the whole qualified name. `"*"` matches every
  function in the file (not the module body itself). Lambdas,
  comprehensions and generator expressions are never matched by
  wildcards.
- **Options** (long form only): `capture` (list of Python expressions
  evaluated in the frame on entry), `return` (bool), `if` (Python
  expression; a call for which it is false or raises is not recorded at
  all, including its exit/suspend/resume), `max_hits` (int). Expression
  errors and `repr` errors are recorded as `<error: ExcType: msg>`, never
  raised. Values are recorded as `reprlib`-limited `repr` strings
  (default limit 200 characters, *my call*).
- **Validation before launch** (fail fast, exit 2 with all problems
  listed):
  - malformed YAML or wrong shape (key → list; entry is a string or a
    one-key mapping with known options);
  - a path key that matches no existing file;
  - a function name or pattern that matches nothing in a matched file,
    found by a static `ast` scan that builds qualified names from
    `def` / `async def` / `class` nesting — with "did you mean" hints
    (difflib) from the file's actual names;
  - an option expression that does not compile.
  Module-name keys cannot be checked statically (importing to find them
  would run code); they are checked at the end of the run instead.
- Runtime matching uses the same rules, so a name the static scan
  accepted is traced when its code first runs.

## Architecture

New package `src/tdb/trace/`:

| Module | Runs in | Job |
|---|---|---|
| `spec.py` | tdb | Load + validate YAML, resolve paths/globs, static `ast` check, write the resolved agent config (JSON). |
| `launch.py` | tdb | Pick interpreter, build environment, spawn, signal handling, wait, call assembly, print summary, exit code. |
| `capture.py` | tdb | Read per-process part files → one capture `.jsonl` (schema below). Also reads a capture back for merge. |
| `perfetto.py` | tdb | One or more captures → Chrome/Perfetto JSON trace. Used by `--trace` and `--trace-merge`. |
| `discover.py` | tdb | Agent discovery parts → spec YAML with comments (hand-written emitter; PyYAML cannot write comments). |
| `agent/sitecustomize.py` | traced program | Start-up hook: if `TDB_TRACE_CONFIG` is set, load the agent, then chain to any other `sitecustomize`. |
| `agent/_tdb_trace_agent.py` | traced program | The tracer. Stdlib only, never imports tdb. |

The `agent/` directory must contain only those two files, because tdb
puts it on the program's `PYTHONPATH`. It must ship in the wheel
(package-data glob if it is not a package); a test must check the built
wheel contains it (see the packaged-examples gotcha: stale
`build/`/egg-info hide missing files).

### How the tracer gets into every Python process

tdb runs the program with:

- `PYTHONPATH` = the agent directory, prepended to any existing value;
- `TDB_TRACE_CONFIG` = path of the resolved config JSON (spec entries as
  absolute-path patterns / module suffixes, options, caps, mode
  `trace` or `discover`);
- `TDB_TRACE_PARTS` = the parts directory for this run.

Any Python process that starts with this environment (the service, its
multiprocessing spawn/forkserver children, Python subprocesses) imports
`sitecustomize` at start-up, which loads the agent. Before writing the
environment, tdb removes any inherited `TDB_TRACE_*` variables and any
earlier agent directory from `PYTHONPATH`, so a traced program that
itself runs `tdb --trace` starts a clean, separate trace.

`sitecustomize.py` must:

- do nothing but chain if `TDB_TRACE_CONFIG` is unset or unreadable;
- on Python < 3.12, write a one-line "skipped" marker
  (`{pid, argv, version}`) to the parts directory and do nothing else, so
  tdb can report the untraced process;
- after starting the agent, import the *next* `sitecustomize` on
  `sys.path` (temporarily removing the agent directory and the cached
  module), so distro or venv `sitecustomize` files still run;
- never raise: any failure is written to the parts directory as an
  agent-error marker and otherwise swallowed.

Python older than 3.12 and free-threaded builds running without the GIL
load the hook but are not traced; their markers are reported (see
"Nothing silent"). Processes the hook never reaches at all
(interpreters started with `-I`, `-E` or `-S`, or children whose
environment the program replaced) leave no trace of themselves and
**cannot be reported**; this is a documented limitation (*my call*). An
audit hook on `subprocess`/`os.exec*` events could detect them, but
audit hooks cannot be removed and add a cost to every audited event in
the program (open, import, socket calls), which works against the
low-perturbation goal.

`os.fork`: the agent registers `os.register_at_fork(after_in_child=...)`
to discard the parent's unflushed buffer, open a new part file for the
child's pid, write its header, and restart the flusher. Monitoring state
survives fork, so nothing else needs re-registering.

### The tracer (`_tdb_trace_agent.py`)

- **Tool id:** 3, else 4 (the unnamed ids). Never `DEBUGGER_ID`,
  `COVERAGE_ID` or `PROFILER_ID`: taking `PROFILER_ID` makes cProfile
  inside the traced program fail with "Another profiling tool is already
  active". If neither 3 nor 4 is free, write an agent-error marker and
  do nothing. It must coexist with coverage.py, cProfile and debuggers.
- **Free-threaded builds:** if the GIL is disabled
  (`sys._is_gil_enabled()` exists and returns False), write a skipped
  marker with that reason and do nothing. The agent's shared state
  (buffer, counters, caches) assumes the GIL. Supporting free-threaded
  builds is later work (*my call*).
- **Finding the functions:** enable `PY_START` globally. The callback
  checks the code object (`co_filename` realpath against path patterns or
  module suffixes, `co_qualname` against name patterns; result cached per
  code object). Not matched → return `sys.monitoring.DISABLE`, so that
  code never calls back again. Matched → turn on local `PY_RETURN`,
  `PY_YIELD`, `PY_RESUME` for that code object and record the entry.
  Exceptions leaving a traced frame need the global `PY_UNWIND` and
  `PY_THROW` events (they cannot be local); their callbacks must return
  fast for untraced code.
- **Call identity:** each recorded entry gets a per-process call id; the
  agent maps the live frame to its call id so exits, suspends and resumes
  find their call, and calls rejected by `if:` or past `max_hits` /
  `--trace-max-events` record nothing for their whole lifetime.
- **Context per event:** `threading.get_native_id()` and the current
  thread's name; the asyncio task, only when `asyncio` is already in
  `sys.modules`, via the running loop's current task — task id is a
  per-process counter assigned on first sight (weak-keyed, never
  `id(task)`), task name from `get_name()`; process name from
  `multiprocessing.current_process().name` only if `multiprocessing` is
  already imported, else `"MainProcess"`. The agent must not import
  `asyncio` or `multiprocessing` itself.
- **Timestamps:** `time.perf_counter_ns()` per event, plus one anchor pair
  `(time.time_ns(), time.perf_counter_ns())` per process taken at agent
  start; epoch ns = anchor_wall + (pc − anchor_pc). (`perf_counter` is
  system-wide on Linux, macOS and Windows; `time.monotonic_ns` on Windows
  has ~16 ms resolution and must not be used.)
- **Values:** `capture` expressions compiled once at agent start,
  evaluated with the frame's globals/locals (`sys._getframe` from the
  callback), on entry; return value from the `PY_RETURN` callback.
- **Never break the program:** every callback is wrapped in
  `try/except BaseException` (an exception escaping a monitoring callback
  is raised in the user's code); a per-thread re-entrancy guard, because
  `capture` expressions can call traced functions; the agent's own files
  are never traced.
- **Writing:** events append to an in-memory buffer; a daemon thread
  named `tdb-trace-flush` writes it to `PARTS/<pid>.jsonl` at least every
  0.25 s (*my call*) and the buffer is flushed at `atexit`. Requirement:
  a hard crash (SIGKILL, segfault, `os._exit` — which multiprocessing's
  fork children use, skipping `atexit`) loses at most the last ~0.5 s of
  events. The flusher thread is excluded from the trace.
- **Crashes:** chain `sys.excepthook` and `threading.excepthook` to
  record a `crash` event (exception type, message, innermost file:line,
  traceback text limited to ~50 lines) before calling the previous hook.
  At normal shutdown write an `exit` record. A part with no `exit` record
  means the process died abruptly; tdb marks it.

### Part files (internal)

`PARTS/<pid>.jsonl`, compact, append-only, one JSON object per line: a
header (pid, ppid, process name, argv, python version, anchor pair),
then definition records (function id → file, qualname, first line;
thread id → name; task id → name) emitted on first use, then events
referencing those ids. The exact compact encoding is the builder's call.
Markers: `skipped-<pid>.json`, `agent-error-<pid>.json`.

The parts directory is `BASE.parts/` next to the outputs. tdb deletes it
after writing both outputs successfully. If tdb itself is killed, the
directory survives, and `--trace-merge` accepts a parts directory in
place of a capture file, so nothing is lost.

### Capture file (`BASE.jsonl`, user-facing, stable)

One JSON object per line, sorted by timestamp, **self-contained** so
`jq`/`grep` work without joins. First line is the header:

```json
{"type":"capture","version":1,"name":"orders","tdb":"0.3.0","command":["/repo/.venv/bin/python","services/orders/main.py","--port","8001"],"cwd":"/repo","spec":"/repo/checkout.yaml","host":"devbox","start_ns":1764067332000000000,"end_ns":1764067512000000000,"exit_code":0}
```

Then one record per process, then events:

```json
{"type":"process","pid":4121,"ppid":4100,"name":"MainProcess","argv":["services/orders/main.py","--port","8001"],"python":"3.12.7","start_ns":...,"end_ns":...,"ended":"clean"}
{"type":"call","ts":1764067340123456789,"pid":4121,"process":"MainProcess","tid":4123,"thread":"MainThread","task_id":7,"task":"Task-7","file":"/repo/services/orders/api.py","function":"OrderApi.checkout","line":88,"call_id":881,"values":{"request.path":"'/checkout'"}}
{"type":"suspend","ts":...,"pid":4121,"call_id":881}
{"type":"resume","ts":...,"pid":4121,"call_id":881}
{"type":"return","ts":...,"pid":4121,"call_id":881,"value":"<Order 17>"}
{"type":"raise","ts":...,"pid":4121,"call_id":882,"exception":"TimeoutError: payment service"}
{"type":"crash","ts":...,"pid":4121,"tid":4123,"thread":"MainThread","exception":"KeyError: 'sku'","where":"/repo/services/orders/cart.py:41","traceback":"..."}
{"type":"note","ts":...,"pid":4122,"message":"event cap reached; 1,204 events dropped"}
```

`task_id`/`task` are `null` outside a task. `ended` is `clean`,
`signal:SIGKILL` (main process only, from the exit status) or `abrupt`.
`call_id` is unique per pid. Field names and the `version` field are a
compatibility promise: later languages write the same schema.

### Perfetto output (`BASE.perfetto.json`)

Chrome JSON trace-event format (`{"traceEvents": [...]}`), which
ui.perfetto.dev opens directly. *JSON rather than Perfetto's protobuf
format is my call:* it is simple, inspectable and needs no encoder; its
limits (two-level process/thread hierarchy, no wall-clock axis, size
past ~1 GB) are acceptable for few-minute captures and are listed as
later work.

- **Lanes.** Perfetto "process" = one traced OS process, named
  `orders · MainProcess (4121)`; `process_sort_index` groups each
  service's processes together, services in command-line order. Perfetto
  "thread" lanes inside it: one per OS thread for code running outside
  any task, plus one per asyncio task, named
  `Task-7 · MainThread`, sorted so each thread's task lanes follow it.
  Lanes are synthetic ids, created only for threads/tasks that ran a
  traced function. pids are remapped when two inputs share one.
- **Slices.** Each recorded call of an ordinary function is a complete
  event (`ph: "X"`) on its lane, from entry to return/raise. How
  suspendable code is drawn depends on whether its lifetime nests
  (*my call*):
  - a **coroutine running inside an asyncio task** is one slice for its
    whole lifetime on the task's lane, with `(suspended)` children (see
    below). Its awaits nest strictly inside its awaiting coroutine, so
    the lane stays valid;
  - **generators, async generators, and coroutines outside any asyncio
    task** (trio, curio, hand-driven coroutines) are drawn as one slice
    **per run**: each resume (or the first start) to the next yield,
    return or raise, on the lane where that run happened. Each run's
    args carry the `call_id` and a run number. A lifetime slice would
    wrongly show the consumer's work between `next()` calls as the
    generator's, and would overlap without nesting (`zip`, pipelines,
    generators outliving their consumer), which Perfetto drops.

  The capture file records all of these the same way (call, suspend,
  resume, return); only the drawing differs. Slice name: the qualified
  name. Args:
  file:line, captured values, return value or exception, wall-clock
  entry time (`HH:MM:SS.ffffff`, local time) — Perfetto's time axis is
  relative, so wall-clock time lives in the slice details and in the
  trace metadata (`start` as ISO time).
- **Suspensions.** Each suspend→resume interval of a traced coroutine in
  an asyncio task is a child slice named `(suspended)` inside the
  innermost traced call that was suspended, so time spent waiting is
  visible as distinct from time running.
- **Nesting must be valid** per lane (Perfetto drops badly nested
  slices). Calls still open when the process ended are closed at the
  process's end time with arg `unfinished: true`. A coroutine resumed in
  a different task than it started in stays on its starting lane (rare;
  builder may add a note arg).
- **Events:** `raise` → an instant marker on the lane; `crash` → a
  process-scoped instant marker named `CRASH: KeyError`; `note` →
  instant marker.
- **Timestamps:** Perfetto `ts` is microseconds as a float. To keep
  sub-µs precision, write `ts` relative to a base epoch (the earliest
  event across all inputs) and record the base in the trace metadata.
  Merged inputs use the same base, which is what aligns services.

### Merge (`--trace-merge`)

Inputs: capture `.jsonl` files and/or `.parts/` directories, at least
one. Each input becomes one service (named from its header; duplicate
names get `#2`, `#3`). All timestamps are epoch ns from the same machine
clock, so no alignment step is needed. Output: `BASE.perfetto.json`
only. Merging a single capture re-renders it (useful when the writer
improves). Inputs whose time ranges do not overlap are allowed (warn).

### Discovery (`--trace-discover OUT.yaml`)

Same launch path and agent, mode `discover`:

- global `PY_START`; count calls per code object **in user code** — not
  under the interpreter's `sysconfig` stdlib/site-packages paths, not
  `<frozen …>`/`<string>`, not the agent. Editable installs (sources
  outside site-packages) count as user code.
- Stop counting a function at 10,000 calls (*my call*): return `DISABLE`
  and report `≥10,000`, which keeps discovery overhead bounded.
- Write OUT.yaml in spec format, paths relative to OUT.yaml's directory,
  files and functions in source order, using `::` separators,
  lambdas/comprehensions omitted:

  ```yaml
  # tdb --trace-discover: services/orders/main.py, 2026-11-25 10:42, 3m12s
  # Remove what you do not need; hot functions make traces large.
  services/orders/api.py:
    - OrderApi::checkout        # 1,204 calls, async
    - OrderApi::_validate       # 1,204 calls
    - parse_header              # ≥10,000 calls  (hot)
  ```

### Nothing silent

The end-of-run summary warns about every way the trace can be
incomplete that tdb can see: processes skipped (Python < 3.12 or
free-threaded without the GIL, with their argv), agent errors, processes that ended abruptly (no `exit` record), event-cap
drops, spec entries (including module-name keys) that never ran, and —
the worst case — zero traced processes (with likely causes: `-I`/`-E`
interpreter, `PYTHONPATH` overridden, not a Python program). Child
processes that bypass the start-up hook entirely are invisible to tdb;
the docs say so and list the ways it happens.

## Later languages (shape only, not built now)

Each later language supplies a tracer that writes the same part-file
schema; `capture.py`, `perfetto.py` and merge stay unchanged, and
`spec.py` gains a per-language static checker. Likely hooks: Ruby
`TracePoint#enable(target:)`, Perl `DB::sub` in tdb's `Devel::` module,
PowerShell `Set-PSBreakpoint -Command -Action`, Bash via tdb's harness
(`FUNCNAME` in the DEBUG trap), native C/C++/Rust/Fortran/OCaml and Go
via DAP logpoints. The `ProfileCapabilities.tracer` field gates each.

## Testing

All with `uv run pytest`; no network; portable to Windows except fork
tests (skip on Windows). Integration tests need a Python ≥ 3.12 to run
the traced program (skip with a reason otherwise).

- **spec:** shapes, path vs module keys, globs, relative-to-spec
  resolution, `::`/`.` equivalence, `<locals>` omission, wildcards, the
  static `ast` checker and its "did you mean" hints, option validation.
- **agent** (run small programs under the agent, assert part contents):
  plain calls with nesting; threads with names; asyncio tasks with
  suspend/resume and exceptions; generators; `if`/`max_hits`/`capture`/
  `return`; an expression that raises; a `capture` that calls a traced
  function (re-entrancy); `sys.excepthook` crash record; event cap;
  chaining to a pre-existing `sitecustomize`; tool-id collision;
  cProfile still works inside a traced program; free-threaded build
  skipped (simulated if no such interpreter is available); untraced code
  is DISABLEd (callback count stays flat).
- **processes:** multiprocessing with spawn (and fork / forkserver on
  POSIX); `subprocess` running Python; `os.fork`; a child that exits via
  `os._exit`; a child on an interpreter < 3.12 if one is available
  (else simulate the marker).
- **launch:** exit-code passthrough; SIGINT reaches the program and tdb
  survives and writes outputs; SIGTERM to tdb forwarded; SIGKILL of the
  program still yields outputs; all three launch forms (`.py`, `-m`, a
  console script found on `PATH`, and a shell wrapper that starts
  Python); `--python` rejected with form 3; `-m` rejected outside trace
  modes; < 3.12 interpreter refused; conflicting flags rejected.
- **capture/perfetto/merge:** schema of every record type; sorted
  output; valid per-lane nesting; suspension slices; per-run slices for
  generators (consumed by `zip`, outliving their consumer) and for a
  coroutine driven by hand outside asyncio; unfinished calls;
  lane naming and sort order; pid collision remap; merge of two captures
  and of a parts directory; relative-ts precision.
- **packaging:** the built wheel contains `tdb/trace/agent/*`.
- **overhead check** (marked slow, not a hard CI gate): a loop calling an
  untraced function runs within a small factor of its untraced time
  under the agent; record the numbers in the PR.

## Docs and examples

- `docs/python/tracing.md` (and mkdocs `nav`, which also feeds `tdb
  --doc`): spec format, commands, reading the trace in Perfetto, merge,
  discovery, limitations.
- `examples/Python/trace/`: two tiny stdlib-only services that talk over
  asyncio streams on localhost, each with a thread pool, plus a spec, so
  the docs' walk-through (two terminals, then merge) runs as written.
  Used by the integration tests too.
- README: short section with a link.

## Left to the builder

- Compact part-file encoding and buffer/flush implementation details
  (within the ≤ 0.5 s loss requirement).
- Exact wording of messages and warnings.
- Internal module boundaries beyond the table above.
- How the `(suspended)` slice is attributed when several traced frames
  in one await chain suspend together (requirement: one suspended slice
  per interval per lane, under the innermost traced call).
- Default limits marked *my call* above may be tuned with evidence.

## Later work (recorded, not planned)

Attach to a running service; cross-service arrows via a propagated
request ID (or an existing OpenTelemetry context); multi-host clock
alignment; Perfetto protobuf output (four-level service → process →
thread → task hierarchy, real wall-clock axis, smaller files); packing
short-lived task lanes; arrows from task creation to the new task;
recording on/off by signal; crash-into-TUI; Python 3.11 via
`sys.setprofile`; other languages.

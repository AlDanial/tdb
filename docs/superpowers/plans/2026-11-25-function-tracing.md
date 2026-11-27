# Function Tracing (`tdb --trace`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `tdb --trace SPEC.yaml PROGRAM`, `tdb --trace-merge`, and `tdb --trace-discover`: record entries into, exits from, and suspensions of named Python functions across a service's tasks, threads and processes, and write a JSON Lines capture plus a Perfetto trace. Then merge several services' captures into one Perfetto view.

**Architecture:** A stdlib-only tracer (`_tdb_trace_agent.py`) loads into every Python process of the service through a `sitecustomize.py` placed on `PYTHONPATH`. It uses `sys.monitoring` on only the spec'd code objects and appends compact records to per-process *part files*. tdb itself never debugs the program. It validates the spec, launches the program in its own process group, waits, assembles the parts into a self-contained capture `.jsonl`, and renders Chrome/Perfetto JSON. Merge renders several captures onto one timeline.

**Tech Stack:** Python 3.11+ for tdb, Python 3.12+ for traced programs (`sys.monitoring`), PyYAML (new runtime dependency), argparse, pytest.

**Spec:** `docs/superpowers/specs/2026-11-25-function-tracing-design.md`. Read it before any task. This plan pins names, formats and tests; the spec gives the reasons.

## Global Constraints

- Traced program: Python ≥ 3.12. tdb's own modules under `src/tdb/trace/` (except `agent/`) must run on Python 3.11.
- `src/tdb/trace/agent/` holds exactly two files, `sitecustomize.py` and `_tdb_trace_agent.py`. They are stdlib-only and never `import tdb`. `sitecustomize.py` must parse on Python 3.7+: no walrus, no `match`, no f-string `=`, no `from __future__ import annotations`. It imports `_tdb_trace_agent` only after its version check.
- Defaults (constants in `tdb/trace/contract.py`):
  - values truncated to 200 characters;
  - flush every 0.25 s;
  - 5,000,000 events per process;
  - discovery stops counting a function at 10,000 calls;
  - tdb waits 2.0 s for children to finish flushing.
- `sys.monitoring` tool id 3, else 4. Never 0, 1, 2 or 5.
- Environment variables: `TDB_TRACE_CONFIG`, `TDB_TRACE_PARTS`.
- Paths are compared after `os.path.normcase(os.path.realpath(p))`.
- Output names:
  - `BASE.jsonl`, `BASE.perfetto.json` and `BASE.parts/`;
  - default BASE is `NAME-YYYYmmdd-HHMMSS` in tdb's cwd;
  - the merge default is `merged-YYYYmmdd-HHMMSS`.
- All tdb messages go to stderr, prefixed `tdb: ` (warnings `tdb: warning: `).
- The program inherits tdb's terminal and process group. tdb never sets SIGINT to `SIG_IGN`, because a child would inherit that.
- Cross-platform: Linux, macOS and Windows. Tests that use fork or POSIX signals skip on Windows (`os.name == "nt"`). Integration tests skip when `sys.version_info < (3, 12)`.
- Testing: a task is done when its tests pass with `uv run pytest <files>`. Run the full suite at the end with `memcap-pytest` (the memory-capped wrapper around `uv run pytest`, see the 2026-08-23 OOM incident).
- Commit after every task, ending the message with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Never push.

## Review Focus

1. **Ctrl-C while traced calls are in flight in several asyncio tasks and a thread.** The outputs are still written, and every lane in Perfetto nests validly: calls ended by `KeyboardInterrupt`/`CancelledError` show as raises, and the rest are closed as `unfinished`. Test: Task 8, `test_sigint_with_calls_in_flight`.
2. **A part file whose last line was cut off by SIGKILL** still assembles; the summary names the unreadable line. Test: Task 6, `test_truncated_last_line_is_ignored`.
3. **Overlapping spec entries for one function** (a glob key plus an explicit key, or `C.*` plus `C.m` with options) record each call once. The most specific entry's options win. Test: Task 4, `test_overlapping_entries_specific_wins`.
4. **Hostile captured values.** A `__repr__` that raises, a 1 MB string, a lone surrogate, or raw bytes is recorded safely, and every part line is still valid JSON. Test: Task 4, `test_hostile_values`.
5. **Cancelled and timed-out coroutines** (`task.cancel()`, `asyncio.wait_for` timeout) close their calls with a resume, then a raise of `CancelledError`. Test: Task 3, `test_cancelled_coroutine`.

---

## Shared formats (referenced by several tasks)

### Agent config JSON (written by tdb, read by the agent)

```json
{"version": 1, "mode": "trace", "parts": "/abs/BASE.parts", "agent_dir": "/abs/.../tdb/trace/agent",
 "targets": [TARGET, ...], "max_events": 5000000, "repr_limit": 200,
 "flush_interval": 0.25, "discover_cap": 10000}
```

`mode` is `"trace"` or `"discover"`; `targets` is `[]` in discover mode. A TARGET is:

```json
{"files": ["<norm_path>", ...], "suffixes": ["orders/workers.py", "orders/workers/__init__.py"],
 "functions": [{"pattern": "Cart.*", "capture": ["x"], "return": false, "if": null, "max_hits": null}]}
```

A path key has `files` filled and `suffixes: []`; a module key the reverse. `pattern` is already normalized: `::` becomes `.`, and `.<locals>` segments are removed.

### Part files (written by the agent, read by `capture.py` and `discover.py`)

The agent writes `PARTS/<pid>.jsonl`, created exclusively. If that name exists it uses `<pid>-2.jsonl`, `<pid>-3.jsonl`, and so on. Each line is one JSON object, written with `json.dumps(obj, separators=(",", ":"))`, which keeps the default `ensure_ascii=True`. `t` is always `time.perf_counter_ns()`.

| `k` | Fields |
|---|---|
| `hdr` | `pid`, `ppid`, `proc` (process name), `argv`, `py` (`"3.12.8"`), `wall` (`time.time_ns()`), `pc` (`perf_counter_ns()` read right after `wall`), `mode` |
| `proc` | `t`, `name`: the process name changed (e.g. multiprocessing set it after start-up) |
| `fn` | `id` (int), `file` (norm_path), `qual` (normalized qualname), `line` (`co_firstlineno`), `kind`: `"function"`, `"coroutine"`, `"generator"` or `"async_generator"` |
| `th` | `tid` (`threading.get_native_id()`), `name`; re-emitted when the name changes |
| `task` | `id` (per-process counter), `name`, `tid`; re-emitted when the name changes |
| `call` | `t`, `c` (call id, per-process counter from 1), `fn`, `tid`, `task` (id or `null`), `v` (dict of expr → repr, only when `capture` is set) |
| `ret` | `t`, `c`, `tid`, `task`, `v` (repr of the return value, only when `return` is true) |
| `raise` | `t`, `c`, `tid`, `task`, `exc` (`"ValueError: bad"`, cut to repr_limit) |
| `susp` | `t`, `c`, `tid`, `task` |
| `res` | `t`, `c`, `tid`, `task` |
| `crash` | `t`, `tid`, `exc`, `where` (`"/abs/file.py:41"`), `tb` (traceback text, at most 50 lines) |
| `note` | `t`, `msg` |
| `count` | `file`, `qual`, `line`, `kind`, `n` (cumulative), `capped` (bool). Discover mode only; emitted at each flush for code whose count changed. |
| `exit` | `t`, `how`: `"atexit"` or `"_exit"` |

Marker files:
- `PARTS/skipped-<pid>.json` = `{"marker": "skipped", "pid", "argv", "python", "reason"}`
- `PARTS/agent-error-<pid>.json` = `{"marker": "agent-error", "pid", "argv", "error"}`

`PARTS/run.json` is written by tdb before launch. It holds the capture header fields known at launch: `name`, `tdb`, `command`, `cwd`, `spec`, `host`. `PARTS/config.json` is the agent config.

### Capture file `BASE.jsonl` (written by `capture.py`; user-facing)

The design's schema, plus `kind` on calls and the lane fields on every event (a compatible superset). Records are sorted by `ts`, with ties kept in file order.
- Line 1: `{"type":"capture","version":1,"name","tdb","command","cwd","spec","host","start_ns","end_ns","exit_code"}`.
- Then one `process` record per part: `{"type":"process","pid","ppid","name","argv","python","start_ns","end_ns","ended"}`, where `ended` is `"clean"`, `"abrupt"` or `"signal:SIGKILL"`.
- Then events. Every event has `type`, `ts` (epoch ns), `pid`, `process`, `tid`, `thread`, `task_id`, `task`. Per type:
  - `call` adds `file`, `function`, `line`, `kind`, `call_id`, `values` (dict or `null`);
  - `return` adds `call_id`, `value` (or `null`);
  - `raise` adds `call_id`, `exception`;
  - `suspend` and `resume` add `call_id`;
  - `crash` adds `exception`, `where`, `traceback`;
  - `note` adds `message`.

  `ts = hdr.wall + (t - hdr.pc)`. `thread` and `task` are the names in force at that event.

---

### Task 1: Spec loader and validator

**Files:**
- Create: `src/tdb/trace/__init__.py` (docstring only)
- Create: `src/tdb/trace/spec.py`
- Modify: `pyproject.toml`: add `"pyyaml>=6.0"` to `dependencies`, then run `uv lock`
- Test: `tests/unit/test_trace_spec.py`

**Interfaces:**
- Produces:
  - `class SpecError(Exception)` with `.problems: list[str]`; `str(err) == "\n".join(problems)`.
  - `@dataclass(frozen=True) class FunctionEntry: raw: str; pattern: str; capture: tuple[str, ...] = (); record_return: bool = False; condition: str | None = None; max_hits: int | None = None`.
  - `@dataclass(frozen=True) class FileEntry: key: str; kind: Literal["path", "module"]; files: tuple[str, ...]; suffixes: tuple[str, ...]; functions: tuple[FunctionEntry, ...]`.
  - `@dataclass(frozen=True) class TraceSpec: path: Path; entries: tuple[FileEntry, ...]`, with method `agent_targets() -> list[dict]` returning the TARGET shape.
  - `load_spec(path: str | os.PathLike) -> TraceSpec`, which raises `SpecError` listing every problem.
  - `normalize_name(name: str) -> str`.
  - `norm_path(p: str | os.PathLike) -> str`.
  - `defined_functions(path: str) -> list[tuple[str, int]]`.
  - `unmatched_entries(spec: TraceSpec, called: set[tuple[str, str]]) -> list[str]`, where `called` holds `(norm_path file, normalized qualname)` pairs.

Rules from the spec "The spec file" section:
- A key is a path if it contains `/` or `\` or ends in `.py`. Otherwise it is a module if it matches `^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$`; anything else is a problem.
- Path keys resolve against `spec.path.parent`, with `glob.glob(..., recursive=True)` filtered to files.
- Module `a.b` → suffixes `("a/b.py", "a/b/__init__.py")`, with no static check.
- Wildcards use `fnmatch.fnmatchcase`.
- Did-you-mean hints come from `difflib.get_close_matches(pattern, names, n=3, cutoff=0.6)`.

Exact problem strings:
- `"invalid YAML: {exc}"`
- `"spec is empty"`
- `"spec must map file names to lists of functions"`
- `"{key}: not a path or module name"`
- `"{key}: no such file"` (a non-glob key)
- `"{key}: matches no files"` (a glob key)
- `"{key}: no functions listed"`
- `"{key}: entry {n} must be a name or a one-key mapping"` (n is 1-based)
- `"{key}: {raw!r}: unknown option {opt!r}"`
- `"{key}: {raw!r}: capture must be a list of expressions"`
- `"{key}: {raw!r}: return must be true or false"`
- `"{key}: {raw!r}: max_hits must be a positive integer"`
- `"{key}: {raw!r}: cannot compile {expr!r}: {msg}"`
- `"{file}: cannot parse: {msg}"`
- `"{key}: {raw!r} matches no function in {where}"` + `" (did you mean: {a, b}?)"` when there are hints. `where` is the key for a single file, else `"any of {n} files"`.

`unmatched_entries` returns `"{key}: {raw}"` strings.

- [ ] **Step 1: Write the failing tests** in `tests/unit/test_trace_spec.py`. Use a helper `write(tmp_path, name, text)` and a fixture module `mod.py`:

```python
MOD = '''\
def handle_request(r):
    pass
class Cart:
    def add(self, x):
        def helper():
            pass
    class Inner:
        def x(self): pass
async def fetch():
    pass
f = lambda: 1
'''

def test_defined_functions_qualnames(tmp_path):
    p = write(tmp_path, "mod.py", MOD)
    assert defined_functions(str(p)) == [
        ("handle_request", 1), ("Cart.add", 4), ("Cart.add.helper", 5),
        ("Cart.Inner.x", 8), ("fetch", 9)]

def test_normalize_name():
    assert normalize_name("Cart::add") == "Cart.add"
    assert normalize_name("outer.<locals>.inner") == "outer.inner"

def test_simple_form_loads(tmp_path):
    write(tmp_path, "mod.py", MOD)
    s = load_spec(write(tmp_path, "s.yaml", "mod.py:\n  - handle_request\n  - Cart::add\n  - Cart.Inner.x\n"))
    (e,) = s.entries
    assert e.kind == "path" and e.files == (norm_path(tmp_path / "mod.py"),)
    assert [f.pattern for f in e.functions] == ["handle_request", "Cart.add", "Cart.Inner.x"]

def test_paths_resolve_relative_to_spec_dir(tmp_path, monkeypatch):
    write(tmp_path / "src", "mod.py", MOD)
    spec = write(tmp_path / "conf", "s.yaml", "../src/mod.py:\n  - fetch\n")
    monkeypatch.chdir(tmp_path / "src")
    assert load_spec(spec).entries[0].files == (norm_path(tmp_path / "src/mod.py"),)

def test_glob_key_expands_recursively(tmp_path):
    write(tmp_path / "pkg/a", "one.py", "def f(): pass\n")
    write(tmp_path / "pkg", "two.py", "def g(): pass\n")
    s = load_spec(write(tmp_path, "s.yaml", "pkg/**/*.py:\n  - '*'\n"))
    assert len(s.entries[0].files) == 2

def test_module_key(tmp_path):
    s = load_spec(write(tmp_path, "s.yaml", "orders.workers:\n  - Worker::run\n"))
    e = s.entries[0]
    assert (e.kind, e.files, e.suffixes) == ("module", (), ("orders/workers.py", "orders/workers/__init__.py"))

def test_unknown_function_reports_did_you_mean(tmp_path):
    write(tmp_path, "mod.py", MOD)
    with pytest.raises(SpecError) as ei:
        load_spec(write(tmp_path, "s.yaml", "mod.py:\n  - handel_request\n"))
    assert ei.value.problems == ["mod.py: 'handel_request' matches no function in mod.py (did you mean: handle_request?)"]

def test_long_form_options(tmp_path):
    write(tmp_path, "mod.py", MOD)
    s = load_spec(write(tmp_path, "s.yaml",
        "mod.py:\n  - handle_request:\n      capture: [r.path, 'len(r)']\n      return: true\n"
        "      if: \"r.method == 'POST'\"\n      max_hits: 500\n"))
    f = s.entries[0].functions[0]
    assert (f.capture, f.record_return, f.condition, f.max_hits) == (("r.path", "len(r)"), True, "r.method == 'POST'", 500)

def test_all_problems_reported_together(tmp_path):
    write(tmp_path, "mod.py", MOD)
    with pytest.raises(SpecError) as ei:
        load_spec(write(tmp_path, "s.yaml",
            "mod.py:\n  - fetch: {bogus: 1}\n  - fetch: {capture: x}\n  - fetch: {max_hits: 0}\n"
            "  - fetch: {if: 'a ==='}\n  - fetch: {return: maybe}\n  - 42\n"
            "missing.py:\n  - f\n" "nope/*.py:\n  - f\n" "bad key!:\n  - f\n" "mod2.py:\n"))
    p = ei.value.problems
    assert len(p) == 10
    assert "mod.py: 'fetch': unknown option 'bogus'" in p
    assert "mod.py: entry 6 must be a name or a one-key mapping" in p
    assert "missing.py: no such file" in p and "nope/*.py: matches no files" in p
    assert "bad key!: not a path or module name" in p and "mod2.py: no functions listed" in p

@pytest.mark.parametrize("text,problem", [("", "spec is empty"), ("- a\n", "spec must map file names to lists of functions"), ("a: [\n", None)])
def test_shape_errors(tmp_path, text, problem):
    with pytest.raises(SpecError) as ei:
        load_spec(write(tmp_path, "s.yaml", text))
    assert (problem in ei.value.problems) if problem else ei.value.problems[0].startswith("invalid YAML: ")

def test_agent_targets_shape(tmp_path):
    write(tmp_path, "mod.py", MOD)
    s = load_spec(write(tmp_path, "s.yaml", "mod.py:\n  - Cart::*\n  - handle_request: {capture: [r]}\n"))
    assert s.agent_targets() == [{"files": [norm_path(tmp_path / "mod.py")], "suffixes": [], "functions": [
        {"pattern": "Cart.*", "capture": [], "return": False, "if": None, "max_hits": None},
        {"pattern": "handle_request", "capture": ["r"], "return": False, "if": None, "max_hits": None}]}]

def test_unmatched_entries(tmp_path):
    write(tmp_path, "mod.py", MOD)
    s = load_spec(write(tmp_path, "s.yaml", "mod.py:\n  - fetch\n  - Cart::*\norders.workers:\n  - Worker.run\n"))
    called = {(norm_path(tmp_path / "mod.py"), "Cart.add")}
    assert unmatched_entries(s, called) == ["mod.py: fetch", "orders.workers: Worker.run"]
```

Module keys match a called file when the file's `/`-normalized path ends with `"/" + suffix`.

- [ ] **Step 2: Run** `uv run pytest tests/unit/test_trace_spec.py -q`. Expected: FAIL (`ModuleNotFoundError: tdb.trace`).
- [ ] **Step 3: Implement `spec.py`** with the signatures above. `load_spec` uses `yaml.safe_load` and collects every problem before raising. `defined_functions` walks `ast` with a name stack: `FunctionDef`/`AsyncFunctionDef` are recorded and then recursed into, `ClassDef` is recursed into without being recorded, and `Lambda` is ignored. Add `pyyaml>=6.0` to `dependencies` and run `uv lock`.
- [ ] **Step 4: Run** `uv run pytest tests/unit/test_trace_spec.py -q`. Expected: all pass.
- [ ] **Step 5: Commit** `src/tdb/trace/__init__.py src/tdb/trace/spec.py tests/unit/test_trace_spec.py pyproject.toml uv.lock` with the message "Add trace spec loader and validator".

---

### Task 2: Agent contract, start-up hook, and tracer core

Covers ordinary functions, threads, flushing, exit, markers, chaining to another `sitecustomize`, the tool id, and disabling untraced code.

**Files:**
- Create: `src/tdb/trace/contract.py`
- Create: `src/tdb/trace/agent/sitecustomize.py`
- Create: `src/tdb/trace/agent/_tdb_trace_agent.py`
- Create: `tests/integration/trace_helpers.py`
- Test: `tests/unit/test_trace_contract.py`, `tests/integration/test_trace_agent_core.py`

**Interfaces:**
- Produces in `contract.py`:
  - `AGENT_DIR: Path`, the `agent/` directory next to the module;
  - `CONFIG_ENV = "TDB_TRACE_CONFIG"`, `PARTS_ENV = "TDB_TRACE_PARTS"`;
  - `DEFAULT_MAX_EVENTS = 5_000_000`, `DEFAULT_REPR_LIMIT = 200`, `FLUSH_INTERVAL = 0.25`, `DISCOVER_CAP = 10_000`, `CHILD_FLUSH_WAIT = 2.0`;
  - `write_config(path: Path, *, mode: str, parts_dir: Path, targets: list[dict], max_events: int = DEFAULT_MAX_EVENTS, repr_limit: int = DEFAULT_REPR_LIMIT, flush_interval: float = FLUSH_INTERVAL, discover_cap: int = DISCOVER_CAP) -> None`;
  - `build_env(base: Mapping[str, str], *, config_path: Path, parts_dir: Path) -> dict[str, str]`. It drops every inherited `TDB_TRACE_*` variable and every `PYTHONPATH` entry equal to `AGENT_DIR` (compared with normcase/abspath). It then prepends `AGENT_DIR` to `PYTHONPATH` with `os.pathsep` and sets both env vars.
- Produces in `sitecustomize.py`: `skip_reason(version: tuple, gil_enabled) -> str | None`. It returns `"python {M}.{m} is older than 3.12"`, `"free-threaded build without the GIL"` (when `gil_enabled is False`), or `None`.
- Produces in `_tdb_trace_agent.py`:
  - `start(config: dict) -> None`;
  - `pick_tool_id() -> int | None`;
  - `stats() -> dict[str, int]` with keys `py_start_callbacks` and `recorded_events`;
  - `final_flush(how: str) -> None`.
- Produces in `tests/integration/trace_helpers.py`:
  - `target(path, *functions: str | dict) -> dict`, a TARGET with `files=[norm_path(path)]`. A str function becomes default options; a dict is merged over the defaults.
  - `@dataclass class Parts: dir: Path; records: dict[str, list[dict]]; markers: list[dict]; returncode: int; stdout: str; stderr: str`, with methods:
    - `all(kind) -> list[dict]`;
    - `qual_of(stem, fn_id) -> str`;
    - `calls(qual) -> list[dict]` (each call record gains `"_stem"`);
    - `events_for(stem, call_id) -> list[dict]`.
  - `run_traced(tmp_path, source: str, targets: list[dict] | None = None, *, mode="trace", max_events=DEFAULT_MAX_EVENTS, python=sys.executable, args=(), timeout=60, extra_env=None, files: dict[str, str] | None = None) -> Parts`. It writes `prog.py` (and `files`) into `tmp_path` and the config via `write_config`. The env comes from `build_env` over `os.environ` updated with the `extra_env` keys that do not start with `TDB_TRACE_`; the `TDB_TRACE_*` keys of `extra_env` are applied after `build_env`, so tests can override them. It then runs `[python, prog.py, *args]` with `cwd=tmp_path`. `targets=None` means "trace every function in `prog.py`" (`target(prog, "*")`).
  - `start_traced(...) -> tuple[subprocess.Popen, Path]`, the same as `run_traced` but without waiting (stdout piped). Use `read_parts(parts_dir) -> Parts` afterwards.

Agent behavior to implement (spec section "The tracer"):
- `sitecustomize.py` does nothing (beyond chaining) when `TDB_TRACE_CONFIG` is unset. Otherwise:
  1. A skip reason leads to the skipped marker.
  2. Load the config. If that fails, or `start` raises, write the agent-error marker.
  3. Remove `agent_dir` from `sys.path`.
  4. Chain to the next `sitecustomize`: delete `sys.modules["sitecustomize"]`, import with `agent_dir` filtered out of `sys.path`, and restore `sys.path` afterwards minus `agent_dir`.

  It never raises.
- `start`:
  - claims the tool id (if none is free, raise, which sitecustomize turns into the agent-error marker);
  - registers `PY_START`, `PY_RETURN`, `PY_YIELD`, `PY_RESUME`, `PY_UNWIND` and `PY_THROW` callbacks, and sets global events `PY_START | PY_UNWIND | PY_THROW`;
  - opens the part file with `os.open(..., O_WRONLY|O_CREAT|O_EXCL|O_APPEND)` and writes with `os.write`. Never use a buffered file object, because fork could inherit a held lock.
  - writes `hdr`, starts the daemon thread `tdb-trace-flush` (excluded from tracing), and registers `atexit.register(final_flush, "atexit")`;
  - replaces `os._exit` with a wrapper that calls `final_flush("_exit")` and then the original.
- Context:
  - the thread name comes from `threading._active.get(threading.get_ident())` (never `current_thread()`, which creates dummy threads), falling back to `f"thread-{native_id}"`;
  - the process name is `sys.modules["multiprocessing"].current_process().name` if that module is loaded, else `"MainProcess"`. It is re-checked at every flush, including `final_flush` (short-lived pool workers get their name after start-up), and a change writes a `proc` record.
- **Matching** a code object against the targets:
  - its file (`norm_path(co_filename)`) is in a target's `files`, or ends with `"/" + suffix` for a module target (compare with `/` separators);
  - its normalized qualname (`co_qualname` with `.<locals>` removed) matches a pattern with `fnmatch.fnmatchcase`.

  Only function bodies are eligible (`co_flags & 0x1`, CO_OPTIMIZED). This excludes module and class bodies. Wildcard patterns never match qualnames containing `<` (lambdas, comprehensions). Each decision is cached per code object.
- Untraced code returns `sys.monitoring.DISABLE` from `PY_START`. Matched code gets local events `PY_RETURN | PY_YIELD | PY_RESUME` the first time.
- Live calls are kept in `dict[id(frame)] -> (call_id, code, entry)`, with the code checked on lookup. No strong references to frames are kept: holding one would delay generator finalization.
- `final_flush` is idempotent. It writes `exit` and the remaining buffer, unregisters all callbacks, frees the tool id, and makes later callbacks return at once.
- Every callback is wrapped in `try/except BaseException: pass`.

- [ ] **Step 1: Write the failing tests.** `tests/unit/test_trace_contract.py`:

```python
def test_build_env_prepends_agent_dir_and_strips_stale(tmp_path):
    base = {"PYTHONPATH": os.pathsep.join([str(AGENT_DIR), "/x"]), "TDB_TRACE_OLD": "1", "HOME": "/h"}
    env = build_env(base, config_path=tmp_path / "c.json", parts_dir=tmp_path / "p")
    assert env["PYTHONPATH"].split(os.pathsep) == [str(AGENT_DIR), "/x"]
    assert "TDB_TRACE_OLD" not in env and env["HOME"] == "/h"
    assert env[CONFIG_ENV] == str(tmp_path / "c.json") and env[PARTS_ENV] == str(tmp_path / "p")

def test_agent_dir_has_only_two_files():
    assert sorted(p.name for p in AGENT_DIR.iterdir() if p.name != "__pycache__") == ["_tdb_trace_agent.py", "sitecustomize.py"]

def test_skip_reason():  # load sitecustomize.py via importlib.util.spec_from_file_location under the name "tdb_sitecustomize_under_test"
    assert sc.skip_reason((3, 11, 9), None) == "python 3.11 is older than 3.12"
    assert sc.skip_reason((3, 13, 1), False) == "free-threaded build without the GIL"
    assert sc.skip_reason((3, 12, 0), True) is None

def test_sitecustomize_parses_on_python_3_7():
    ast.parse((AGENT_DIR / "sitecustomize.py").read_text(), feature_version=(3, 7))

def test_pick_tool_id_skips_taken_ids():  # import _tdb_trace_agent from AGENT_DIR; claim 3 then 3+4 with sys.monitoring.use_tool_id; free them in finally
    ...  # assert pick_tool_id() == 4 when 3 is taken; None when 3 and 4 are taken
```

`tests/integration/test_trace_agent_core.py` (module-level `pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="sys.monitoring")`):

```python
NEST = "def g():\n    return 2\ndef f():\n    return g()\nf()\n"

def test_records_entry_and_return_with_nesting(tmp_path):
    parts = run_traced(tmp_path, NEST)
    (stem,) = parts.records
    seq = [(r["k"], parts.qual_of(stem, r.get("fn")) if r["k"] == "call" else r["c"])
           for r in parts.records[stem] if r["k"] in ("call", "ret")]
    assert seq == [("call", "f"), ("call", "g"), ("ret", 2), ("ret", 1)]
    fns = {r["qual"]: r for r in parts.all("fn")}
    assert fns["f"]["kind"] == "function" and fns["f"]["file"] == norm_path(tmp_path / "prog.py")
    ts = [r["t"] for r in parts.records[stem] if "t" in r]
    assert ts == sorted(ts)

def test_header_and_exit_records(tmp_path):
    recs = next(iter(run_traced(tmp_path, NEST).records.values()))
    hdr = recs[0]
    assert hdr["k"] == "hdr" and hdr["proc"] == "MainProcess" and hdr["mode"] == "trace"
    assert hdr["argv"][0].endswith("prog.py") and hdr["py"].startswith("3.")
    assert isinstance(hdr["wall"], int) and isinstance(hdr["pc"], int)
    assert recs[-1] == {"k": "exit", "t": recs[-1]["t"], "how": "atexit"}

def test_thread_names(tmp_path):
    src = "import threading\ndef f():\n    pass\nt = threading.Thread(target=f, name='worker-1')\nt.start(); t.join()\nf()\n"
    parts = run_traced(tmp_path, src)
    names = {r["tid"]: r["name"] for r in parts.all("th")}
    assert sorted(names[c["tid"]] for c in parts.calls("f")) == ["MainThread", "worker-1"]

def test_untraced_code_is_disabled(tmp_path):
    def callbacks(n):
        src = (f"def hot():\n    return 1\nfor _ in range({n}):\n    hot()\n"
               "import _tdb_trace_agent\nprint(_tdb_trace_agent.stats()['py_start_callbacks'])\n")
        out = run_traced(tmp_path, src, [target(tmp_path / "prog.py", "nothing_here")]).stdout
        return int(out.splitlines()[-1])
    assert abs(callbacks(100_000) - callbacks(10)) <= 2

def test_program_sees_unchanged_sys_path_and_argv(tmp_path):
    src = "import sys, json\nprint(json.dumps([sys.path, sys.argv]))\n"
    traced = run_traced(tmp_path, src, args=("--port", "8001")).stdout
    plain = subprocess.run([sys.executable, "prog.py", "--port", "8001"], cwd=tmp_path, capture_output=True, text=True).stdout
    assert traced == plain

def test_chains_existing_sitecustomize(tmp_path):
    user = tmp_path / "usersite"; user.mkdir()
    (user / "sitecustomize.py").write_text("print('user-site-ran')\n")
    parts = run_traced(tmp_path, NEST, extra_env={"PYTHONPATH": str(user)})
    assert parts.stdout.startswith("user-site-ran") and parts.calls("f")

def test_cprofile_still_works(tmp_path):
    src = NEST + "import cProfile\ncProfile.run('f()')\n"
    parts = run_traced(tmp_path, src)
    assert parts.returncode == 0 and "function calls" in parts.stdout

def test_old_python_writes_skipped_marker(tmp_path):
    py311 = shutil.which("python3.11") or pytest.skip("no python3.11")
    parts = run_traced(tmp_path, NEST + "print('ran')\n", python=py311)
    assert parts.records == {} and parts.stdout.strip() == "ran"
    assert [(m["marker"], m["reason"]) for m in parts.markers] == [("skipped", "python 3.11 is older than 3.12")]

def test_unreadable_config_writes_agent_error(tmp_path):
    parts = run_traced(tmp_path, NEST + "print('ran')\n", extra_env={"TDB_TRACE_CONFIG": str(tmp_path / "missing.json")})
    assert parts.returncode == 0 and parts.stdout.strip() == "ran"
    assert [m["marker"] for m in parts.markers] == ["agent-error"]

@pytest.mark.skipif(os.name == "nt", reason="SIGKILL")
def test_flush_bounds_loss_on_sigkill(tmp_path):
    src = NEST + "import time\nprint('ready', flush=True)\ntime.sleep(30)\n"
    proc, parts_dir = start_traced(tmp_path, src)
    assert proc.stdout.readline().strip() == "ready"
    time.sleep(0.6); proc.kill(); proc.wait()
    assert read_parts(parts_dir).calls("f")
```

`run_traced` with `extra_env` containing `TDB_TRACE_CONFIG` overrides the value that `build_env` set.

- [ ] **Step 2: Run** `uv run pytest tests/unit/test_trace_contract.py tests/integration/test_trace_agent_core.py -q`. Expected: FAIL (imports missing).
- [ ] **Step 3: Implement** `contract.py`, `sitecustomize.py`, the agent core in `_tdb_trace_agent.py`, and `trace_helpers.py`, to the interfaces and behavior above. The `kind` flag values come from `co_flags`: `0x80` → coroutine, `0x200` → async_generator, `0x20` → generator, else function.
- [ ] **Step 4: Run** the same command. Expected: all pass (the 3.11 test may skip).
- [ ] **Step 5: Commit** with the message "Add trace agent start-up hook and tracer core".

---

### Task 3: Coroutines, tasks, generators, exceptions, crashes

**Files:**
- Modify: `src/tdb/trace/agent/_tdb_trace_agent.py`
- Test: `tests/integration/test_trace_agent_async.py`

**Interfaces:**
- Consumes: Task 2's agent and `trace_helpers`.
- Produces: `susp`, `res`, `raise`, `task` and `crash` part records, exactly as in the shared formats.

Behavior:
- The asyncio task is looked up only if `"asyncio" in sys.modules`. Use `asyncio._get_running_loop()` and then `asyncio.current_task(loop)`. Task ids come from a `weakref.WeakKeyDictionary` mapping tasks to a counter; the name comes from `get_name()`.
- `PY_YIELD` → `susp`; `PY_RESUME` → `res`; `PY_THROW` on a live traced frame → `res`; `PY_UNWIND` on a live traced frame → pop it and write `raise` with `f"{type(e).__name__}: {e}"` cut to repr_limit (just the type name when `str(e)` is empty).
- Never return `DISABLE` from `PY_UNWIND` or `PY_THROW`.
- Crash records come from wrapping `sys.excepthook` and `threading.excepthook`. Each wrapper records the crash and then calls the previous hook. `where` is the innermost traceback frame's `file:line`. `tb` is `traceback.format_exception` text, at most 50 lines, with `traceback` imported lazily.

(Verified on 3.12 and 3.14: in an `outer` → `inner` → `sleep` chain, the yields arrive inner then outer and the resumes outer then inner. Cancellation gives `PY_THROW` then `PY_UNWIND` for each frame, inner first. `gen.close()` gives `PY_THROW` then `PY_UNWIND`.)

- [ ] **Step 1: Write the failing tests:**

```python
CHAIN = ("import asyncio\nasync def inner():\n    await asyncio.sleep(0.01)\n    return 2\n"
         "async def outer():\n    return await inner()\n")

def kinds(parts, qual):
    (c,) = parts.calls(qual)
    return [r["k"] for r in parts.events_for(c["_stem"], c["c"])]

def test_coroutine_suspend_resume_in_task(tmp_path):
    parts = run_traced(tmp_path, CHAIN + "asyncio.run(outer())\n")
    assert kinds(parts, "inner") == ["call", "susp", "res", "ret"]
    assert kinds(parts, "outer") == ["call", "susp", "res", "ret"]
    (c,) = parts.calls("outer")
    assert c["task"] is not None
    assert {t["id"]: t["name"] for t in parts.all("task")}[c["task"]] == "Task-1"

def test_task_names_and_unique_ids(tmp_path):
    src = CHAIN + ("async def main():\n    for i in range(50):\n"
                   "        await asyncio.create_task(outer(), name=f'fetch-{i}')\nasyncio.run(main())\n")
    parts = run_traced(tmp_path, src)
    ids = {c["task"] for c in parts.calls("outer")}
    names = {t["id"]: t["name"] for t in parts.all("task")}
    assert len(ids) == 50 and {names[i] for i in ids} == {f"fetch-{i}" for i in range(50)}

def test_generator_runs(tmp_path):
    src = "def gen(n):\n    for i in range(n):\n        yield i\nlist(zip(gen(3), gen(3)))\n"
    parts = run_traced(tmp_path, src)
    for c in parts.calls("gen"):
        ks = [r["k"] for r in parts.events_for(c["_stem"], c["c"])]
        assert ks[0] == "call" and ks.count("susp") == ks.count("res") and ks[-1] in ("ret", "raise")

def test_exception_exit_records_raise(tmp_path):
    parts = run_traced(tmp_path, "def boom():\n    raise ValueError('bad')\ntry:\n    boom()\nexcept ValueError:\n    pass\n")
    assert kinds(parts, "boom") == ["call", "raise"]
    assert parts.all("raise")[0]["exc"] == "ValueError: bad"

def test_generator_close_records_resume_then_raise(tmp_path):
    parts = run_traced(tmp_path, "def gen():\n    yield 1\n    yield 2\ng = gen(); next(g); g.close()\n")
    assert kinds(parts, "gen") == ["call", "susp", "res", "raise"]
    assert parts.all("raise")[0]["exc"] == "GeneratorExit"

def test_cancelled_coroutine(tmp_path):  # Review Focus 5
    src = CHAIN + ("async def main():\n    t = asyncio.create_task(outer())\n    await asyncio.sleep(0.001)\n"
                   "    t.cancel()\n    try:\n        await t\n    except asyncio.CancelledError:\n        pass\n"
                   "    try:\n        await asyncio.wait_for(outer(), 0.001)\n    except TimeoutError:\n        pass\n"
                   "asyncio.run(main())\n")
    parts = run_traced(tmp_path, src, [target(tmp_path / "prog.py", "inner", "outer")])
    for qual in ("inner", "outer"):
        for c in parts.calls(qual):
            evs = parts.events_for(c["_stem"], c["c"])
            assert [r["k"] for r in evs][-2:] == ["res", "raise"] and evs[-1]["exc"].startswith("CancelledError")

def test_uncaught_exception_writes_crash(tmp_path):
    parts = run_traced(tmp_path, "x = 1\nraise KeyError('sku')\n")
    (cr,) = parts.all("crash")
    assert cr["exc"] == "KeyError: 'sku'" and cr["where"].endswith("prog.py:2") and "Traceback" in cr["tb"]
    assert parts.returncode == 1 and "KeyError" in parts.stderr

def test_thread_crash_recorded(tmp_path):
    src = "import threading\ndef bad():\n    raise RuntimeError('t')\nt = threading.Thread(target=bad, name='w')\nt.start(); t.join()\n"
    parts = run_traced(tmp_path, src)
    (cr,) = parts.all("crash")
    assert {r["tid"]: r["name"] for r in parts.all("th")}[cr["tid"]] == "w"
    assert "Exception in thread w" in parts.stderr
```

- [ ] **Step 2: Run** `uv run pytest tests/integration/test_trace_agent_async.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the behavior above in `_tdb_trace_agent.py`.
- [ ] **Step 4: Run** `uv run pytest tests/integration/test_trace_agent_async.py tests/integration/test_trace_agent_core.py -q`. Expected: all pass.
- [ ] **Step 5: Commit** with the message "Trace coroutines, tasks, generators and crashes".

---

### Task 4: Per-function options, event cap, re-entrancy

**Files:**
- Modify: `src/tdb/trace/agent/_tdb_trace_agent.py`
- Test: `tests/integration/test_trace_agent_options.py`

**Interfaces:**
- Consumes: TARGET `functions[*]` options (`capture`, `return`, `if`, `max_hits`) and config `max_events`, `repr_limit`.
- Produces: `call.v`, `ret.v`, and the cap notes.

Behavior:
- Compile `capture` and `if` once at `start` (`mode="eval"`) and evaluate them with `sys._getframe(1)`'s `f_globals`/`f_locals`.
- A false or raising `if` records nothing for that call's whole life. So do `max_hits` (counted over recorded calls per function entry) and the cap.
- Values use a `reprlib.Repr` subclass with `maxstring`/`maxother`/`maxlong` = repr_limit. The subclass overrides `repr_instance` to call `builtins.repr` without catching, because the stock one hides a failing `__repr__` as `<Bad instance at 0x…>`. The result is cut to repr_limit. Errors become `"<error: {Type}: {msg}>"`.
- **Specificity:** when several entries match a code object, a pattern with no `*`, `?` or `[` beats one with wildcards; otherwise the first in spec order wins. Wildcard patterns never match qualnames containing `<` (lambdas, comprehensions, `<module>`).
- **Event cap:** count `call`, `ret`, `raise`, `susp` and `res` records. Once the count reaches `max_events`, stop recording new calls, but keep recording ends and suspensions of calls already recorded.
  - On the first dropped call, write `note` `"event cap of {max_events:,} reached; later calls not recorded"`.
  - At `final_flush`, write `note` `"{n:,} calls not recorded (event cap)"`.
- **Re-entrancy guard:** a per-thread set of thread idents that are inside a callback. A nested callback returns at once without `DISABLE`.

- [ ] **Step 1: Write the failing tests:**

```python
def test_capture_values_on_entry(tmp_path):
    parts = run_traced(tmp_path, "def f(a, b):\n    pass\nf(1, 2)\n",
                       [target(tmp_path / "prog.py", {"pattern": "f", "capture": ["a", "b * 2", "missing"]})])
    assert parts.calls("f")[0]["v"] == {"a": "1", "b * 2": "4",
                                         "missing": "<error: NameError: name 'missing' is not defined>"}

def test_return_value_recorded_only_when_asked(tmp_path):
    src = "def f():\n    return {'k': 1}\ndef g():\n    return 5\nf(); g()\n"
    parts = run_traced(tmp_path, src, [target(tmp_path / "prog.py", {"pattern": "f", "return": True}, "g")])
    rets = {parts.qual_of(c["_stem"], c["fn"]): parts.events_for(c["_stem"], c["c"])[-1] for c in parts.calls("f") + parts.calls("g")}
    assert rets["f"]["v"] == "{'k': 1}" and "v" not in rets["g"]

def test_if_condition_filters_whole_call(tmp_path):
    src = "import asyncio\nasync def h(n):\n    await asyncio.sleep(0)\nasync def main():\n    for n in range(4):\n        await h(n)\nasyncio.run(main())\n"
    parts = run_traced(tmp_path, src, [target(tmp_path / "prog.py", {"pattern": "h", "capture": ["n"], "if": "n % 2 == 0"})])
    recorded = {c["c"] for c in parts.calls("h")}
    assert [c["v"]["n"] for c in parts.calls("h")] == ["0", "2"]
    assert all(r["c"] in recorded for k in ("susp", "res", "ret") for r in parts.all(k))

def test_condition_that_raises_skips_call(tmp_path):
    parts = run_traced(tmp_path, "def f(x):\n    pass\nf(1)\n", [target(tmp_path / "prog.py", {"pattern": "f", "if": "x.nope"})])
    assert parts.calls("f") == [] and parts.returncode == 0

def test_max_hits(tmp_path):
    parts = run_traced(tmp_path, "def f():\n    pass\nfor _ in range(10):\n    f()\n", [target(tmp_path / "prog.py", {"pattern": "f", "max_hits": 3})])
    assert len(parts.calls("f")) == 3 and len(parts.all("ret")) == 3

def test_event_cap(tmp_path):
    parts = run_traced(tmp_path, "def f():\n    pass\nfor _ in range(50):\n    f()\n", max_events=10)
    assert len(parts.calls("f")) == 5
    assert [n["msg"] for n in parts.all("note")] == ["event cap of 10 reached; later calls not recorded", "45 calls not recorded (event cap)"]

def test_reentrant_capture_does_not_recurse(tmp_path):
    src = "def helper():\n    return 1\ndef f():\n    pass\nf(); helper()\n"
    parts = run_traced(tmp_path, src, [target(tmp_path / "prog.py", {"pattern": "f", "capture": ["helper()"]}, "helper")])
    assert parts.calls("f")[0]["v"] == {"helper()": "1"} and len(parts.calls("helper")) == 1

def test_hostile_values(tmp_path):  # Review Focus 4
    src = ("class Bad:\n    def __repr__(self):\n        raise RuntimeError('nope')\n"
           "def f(a, b, c, d):\n    pass\nf(Bad(), 'x' * 1_000_000, '\\udcff', b'\\xff')\n")
    parts = run_traced(tmp_path, src, [target(tmp_path / "prog.py", {"pattern": "f", "capture": ["a", "b", "c", "d"]})])
    v = parts.calls("f")[0]["v"]
    assert v["a"] == "<error: RuntimeError: nope>" and len(v["b"]) <= 200 and v["d"] == "b'\\xff'"
    assert v["c"] == repr("\udcff")

def test_overlapping_entries_specific_wins(tmp_path):  # Review Focus 3
    src = "class C:\n    def m(self, x):\n        pass\nC().m(7)\n"
    prog = tmp_path / "prog.py"
    parts = run_traced(tmp_path, src, [target(prog, "C.*"), target(prog, {"pattern": "C.m", "capture": ["x"]})])
    (c,) = parts.calls("C.m")
    assert c["v"] == {"x": "7"}
```

- [ ] **Step 2: Run** `uv run pytest tests/integration/test_trace_agent_options.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the behavior above.
- [ ] **Step 4: Run** `uv run pytest tests/integration/test_trace_agent_*.py -q`. Expected: all pass.
- [ ] **Step 5: Commit** with the message "Trace options: capture, return, if, max_hits, event cap".

---

### Task 5: Child processes

**Files:**
- Modify: `src/tdb/trace/agent/_tdb_trace_agent.py`
- Test: `tests/integration/test_trace_agent_processes.py`

**Interfaces:**
- Consumes: Task 2's part-file naming, `final_flush`, and the `os._exit` wrapper.
- Produces: one part file per traced process. Each child's `hdr.ppid` is its parent's pid.

Behavior:
- Use `os.register_at_fork(before=<flush buffer now>, after_in_child=<reinit>)` (POSIX only). The child's reinit:
  - discards the inherited buffer;
  - `os.close`s the inherited fd and opens `<childpid>.jsonl`;
  - writes a new `hdr`;
  - clears the live-call map and the thread/task caches, and restarts call ids at 1;
  - starts a new flusher thread and resets the "exit written" flag.
- Spawn, forkserver and `subprocess` children inherit the environment, so the hook loads by itself; nothing extra is needed beyond what Task 2 does.

- [ ] **Step 1: Write the failing tests:**

```python
WORK = "def work(x):\n    return x * x\n"

@pytest.mark.parametrize("method", ["spawn"] + ([] if os.name == "nt" else ["forkserver", "fork"]))
def test_pool_workers_traced(tmp_path, method):
    # close()+join() so workers exit normally; the `with` block's terminate() would SIGTERM them
    src = WORK + (f"if __name__ == '__main__':\n    import multiprocessing as mp\n"
                  f"    with mp.get_context('{method}').Pool(2) as p:\n        print(p.map(work, range(4)))\n"
                  f"        p.close(); p.join()\n")
    parts = run_traced(tmp_path, src, [target(tmp_path / "prog.py", "work")])
    assert parts.stdout.strip() == "[0, 1, 4, 9]" and len(parts.calls("work")) == 4
    worker_names = {r["name"] for r in parts.all("proc")} | {r["proc"] for r in parts.all("hdr")}
    prefix = {"spawn": "SpawnPoolWorker", "forkserver": "ForkServerPoolWorker", "fork": "ForkPoolWorker"}[method]
    assert any(n.startswith(prefix) for n in worker_names)

@pytest.mark.skipif(os.name == "nt", reason="fork")
def test_fork_child_traced_and_parent_events_not_duplicated(tmp_path):
    src = ("import os\ndef f(tag):\n    pass\nf('parent')\npid = os.fork()\n"
           "if pid == 0:\n    f('child'); os._exit(0)\nos.waitpid(pid, 0)\n")
    parts = run_traced(tmp_path, src, [target(tmp_path / "prog.py", {"pattern": "f", "capture": ["tag"]})])
    hdrs = {stem: recs[0] for stem, recs in parts.records.items()}
    main = next(s for s, h in hdrs.items() if h["ppid"] == os.getpid())
    child = next(s for s, h in hdrs.items() if h["ppid"] == hdrs[main]["pid"])
    assert [c["v"]["tag"] for c in parts.calls("f") if c["_stem"] == child] == ["'child'"]
    assert parts.records[child][-1]["how"] == "_exit"

def test_subprocess_python_child(tmp_path):
    files = {"child.py": "def in_child():\n    pass\nin_child()\n"}
    src = "import subprocess, sys\nsubprocess.run([sys.executable, 'child.py'], check=True)\n"
    parts = run_traced(tmp_path, src, [target(tmp_path / "child.py", "in_child")], files=files)
    assert len(parts.calls("in_child")) == 1 and len(parts.records) == 2

def test_old_python_child_marker(tmp_path):
    py311 = shutil.which("python3.11") or pytest.skip("no python3.11")
    src = f"import subprocess\nsubprocess.run([{py311!r}, '-c', 'pass'], check=True)\n"
    parts = run_traced(tmp_path, src)
    assert [m["reason"] for m in parts.markers] == ["python 3.11 is older than 3.12"]
```

- [ ] **Step 2: Run** `uv run pytest tests/integration/test_trace_agent_processes.py -q`. Expected: the fork test FAILs (no child part); the spawn tests may already pass.
- [ ] **Step 3: Implement** the fork handling.
- [ ] **Step 4: Run** `uv run pytest tests/integration/test_trace_agent_*.py -q`. Expected: all pass.
- [ ] **Step 5: Commit** with the message "Trace forked, spawned and subprocess children".

---

### Task 6: Capture assembly

**Files:**
- Create: `src/tdb/trace/capture.py`
- Test: `tests/unit/test_trace_capture.py`

**Interfaces:**
- Consumes: the part-file and marker formats.
- Produces:
  - `@dataclass class Capture: header: dict; processes: list[dict]; events: list[dict]`, using capture-schema dicts.
  - `@dataclass class CaptureSummary: processes: int; threads: int; tasks: int; calls: int; skipped: list[dict]; agent_errors: list[dict]; abrupt: list[int]; notes: list[str]; called: set[tuple[str, str]]`. `called` holds `(file, function)` from call records. `notes` holds the agent `note` messages as `"pid {pid}: {msg}"` plus unreadable-line notes.
  - `assemble(parts_dir: Path, *, header: dict, main_pid: int | None = None, main_signal: str | None = None) -> tuple[Capture, CaptureSummary]`. `header` holds the `run.json` fields plus `exit_code`; `assemble` fills in `type`, `version`, `start_ns` and `end_ns`.
  - `write_capture(capture: Capture, path: Path) -> None`.
  - `load_capture(path: Path) -> Capture`.
  - `load_input(path: Path) -> Capture`, which takes a `.jsonl` capture or a `.parts` directory. A directory uses its `run.json` as the header, with `exit_code: null`.
  - `PART_FILE = re.compile(r"^\d+(-\d+)?\.jsonl$")`.

Rules:
- A process's `start_ns` is `hdr.wall`. Its `end_ns` is the epoch of its `exit` record, else of its last record.
- `ended`:
  - `"signal:{main_signal}"` when `pid == main_pid` and `main_signal` is given;
  - else `"clean"` if an `exit` record exists;
  - else `"abrupt"`.

  `abrupt` lists the pids of abrupt processes.
- The process `name` is the last `proc` record's name, else `hdr.proc`.
- Unparsable lines are skipped and noted as `"{n} unreadable line(s) in {file name}"`.
- Count threads as distinct `(pid, tid)` and tasks as distinct `(pid, task id)`.

- [ ] **Step 1: Write the failing tests.** Build parts by hand with a helper `part(dir, pid, records)` that writes JSONL.

```python
H = lambda pid, wall, pc, ppid=1: {"k": "hdr", "pid": pid, "ppid": ppid, "proc": "MainProcess", "argv": ["p.py"], "py": "3.12.8", "wall": wall, "pc": pc, "mode": "trace"}
FN = {"k": "fn", "id": 1, "file": "/r/api.py", "qual": "Api.checkout", "line": 88, "kind": "coroutine"}

def test_assemble_converts_to_epoch_and_sorts(tmp_path):
    part(tmp_path, 10, [H(10, 1_000_000, 500), FN, {"k": "th", "tid": 7, "name": "MainThread"},
                        {"k": "call", "t": 600, "c": 1, "fn": 1, "tid": 7, "task": None}, {"k": "exit", "t": 900, "how": "atexit"}])
    part(tmp_path, 11, [H(11, 2_000_000, 1_000_050, ppid=10), FN, {"k": "th", "tid": 8, "name": "MainThread"},
                        {"k": "call", "t": 1_000_000, "c": 1, "fn": 1, "tid": 8, "task": None}])
    cap, summ = assemble(tmp_path, header={"name": "orders", "exit_code": 0})
    assert [(e["pid"], e["ts"]) for e in cap.events] == [(10, 1_000_100), (11, 1_999_950)]
    assert {p["pid"]: p["ended"] for p in cap.processes} == {10: "clean", 11: "abrupt"} and summ.abrupt == [11]

def test_call_record_is_self_contained(tmp_path):
    part(tmp_path, 10, [H(10, 0, 0), FN, {"k": "th", "tid": 7, "name": "MainThread"},
                        {"k": "task", "id": 3, "name": "Task-3", "tid": 7},
                        {"k": "call", "t": 5, "c": 1, "fn": 1, "tid": 7, "task": 3, "v": {"x": "1"}},
                        {"k": "susp", "t": 6, "c": 1, "tid": 7, "task": 3}])
    cap, _ = assemble(tmp_path, header={"name": "o", "exit_code": 0})
    assert cap.events[0] == {"type": "call", "ts": 5, "pid": 10, "process": "MainProcess", "tid": 7, "thread": "MainThread",
                             "task_id": 3, "task": "Task-3", "file": "/r/api.py", "function": "Api.checkout", "line": 88,
                             "kind": "coroutine", "call_id": 1, "values": {"x": "1"}}
    assert cap.events[1] == {"type": "suspend", "ts": 6, "pid": 10, "process": "MainProcess", "tid": 7, "thread": "MainThread",
                             "task_id": 3, "task": "Task-3", "call_id": 1}

def test_main_signal_and_process_name(tmp_path):
    part(tmp_path, 10, [H(10, 0, 0), {"k": "proc", "t": 1, "name": "SpawnPoolWorker-1"}])
    cap, _ = assemble(tmp_path, header={"name": "o", "exit_code": 137}, main_pid=10, main_signal="SIGKILL")
    assert cap.processes[0]["name"] == "SpawnPoolWorker-1" and cap.processes[0]["ended"] == "signal:SIGKILL"

def test_truncated_last_line_is_ignored(tmp_path):  # Review Focus 2
    part(tmp_path, 10, [H(10, 0, 0), FN, {"k": "th", "tid": 7, "name": "MainThread"},
                        {"k": "call", "t": 5, "c": 1, "fn": 1, "tid": 7, "task": None}])
    with open(tmp_path / "10.jsonl", "a") as f:
        f.write('{"k":"ret","t":9,"c"')
    cap, summ = assemble(tmp_path, header={"name": "o", "exit_code": 0})
    assert len(cap.events) == 1 and "1 unreadable line(s) in 10.jsonl" in summ.notes

def test_summary_counts_markers_and_called(tmp_path):
    ...  # two threads, one task, two calls, a skipped marker and an agent-error marker -> exact counts and lists;
         # summ.called == {("/r/api.py", "Api.checkout")}

def test_write_load_roundtrip_and_parts_dir_input(tmp_path):
    ...  # write_capture then load_capture gives an equal Capture; load_input(parts_dir) uses run.json's name and exit_code None
```

The two tests above whose bodies are `...` are written in the same style. Their assertions are the ones stated in their comments.

- [ ] **Step 2: Run** `uv run pytest tests/unit/test_trace_capture.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `capture.py`.
- [ ] **Step 4: Run** the same command. Expected: all pass.
- [ ] **Step 5: Commit** with the message "Assemble trace parts into capture files".

---

### Task 7: Perfetto writer

**Files:**
- Create: `src/tdb/trace/perfetto.py`
- Create: `tests/trace_checks.py` (shared assertions for Tasks 7, 8 and 11)
- Test: `tests/unit/test_trace_perfetto.py`

**Interfaces:**
- Consumes: `Capture`.
- Produces:
  - `build_trace(captures: list[Capture]) -> dict`, giving `{"traceEvents": [...], "displayTimeUnit": "ns", "metadata": {"tdb_base_epoch_ns": base, "start": ISO-8601 local time of base, "services": [names]}}`;
  - `write_trace(captures: list[Capture], path: Path) -> None`;
  - in `tests/trace_checks.py`: `assert_nested(trace: dict) -> None`. For every `(pid, tid)`, the `X` events, sorted by `(ts, -dur)`, must form a proper nesting (no partial overlap).

Rules (spec "Perfetto output", plus the reviewer-settled drawing rules):
- Base = the minimum `ts` over all events of all captures. Event `ts` and `dur` are `round((ns - base) / 1000, 3)`.
- **Services:** names come from `header["name"]`; duplicates become `name#2`, `name#3`.
- **Process ids:** synthetic pids are numbered 1.. by `(service index, real pid)`.
- **Process metadata:**
  - `process_name` = `"{service} · {process name} ({real pid})"`;
  - `process_sort_index` = `service_index * 1000 + order` of the process by `start_ns`.
- **Lanes:** a lane is `(service, pid, tid, task_id)`. Synthetic tids are numbered 1.. globally, and lanes are created only for events that occur.
- **Lane metadata:**
  - `thread_name` = the thread name, or `"{task} · {thread}"` for a task lane;
  - `thread_sort_index` puts each thread lane first, followed by its task lanes in order of their first event.
- **Ordinary function:** one `X` from call to return/raise on the call's lane, with `cat` `"call"`.
- **Lifetime-drawn calls:** `kind == "coroutine"` and the call record's `task_id` is not null. Each gets one `X` from call to end on the call's lane. Its suspensions become `X` events named `"(suspended)"` (`cat` `"suspended"`) on the same lane.
  - Drop any suspended interval contained in another suspended interval on the same lane. This keeps only the innermost traced frame's suspension.
- **Per-run calls:** generators, async generators, and coroutines with a null `task_id`. Each run gets an `X`, from call or resume to the next suspend or end, on the lane of the event that started the run.
  - The run's args add `"run": n` (1-based).
- **Args on call slices:**
  - `"file": "{file}:{line}"`;
  - `"call_id"`;
  - `"wall"`: local time of entry as `HH:MM:SS.ffffff`;
  - `"values"` when not null;
  - `"return"` when not null;
  - `"exception"` for a raise;
  - `"unfinished": true` for calls with no end, which end at their process's `end_ns`.
- **Instants:**
  - each raise → `{"ph": "i", "s": "t", "name": "raise {Type}"}` on the lane;
  - each crash → `{"ph": "i", "s": "p", "name": "CRASH: {Type}"}`, or `"INTERRUPTED"` when the type is `KeyboardInterrupt`, with args `exception`, `where`, `traceback`;
  - each note → `{"ph": "i", "s": "p", "name": "note", "args": {"message": ...}}`.

  `{Type}` is the text before the first `:`.

- [ ] **Step 1: Write the failing tests.** Use a helper `cap(name, processes, events)` that builds a `Capture` directly. Events need only the fields `perfetto.py` reads.

```python
def test_names_and_sorting(): ...           # process_name "orders · MainProcess (4121)"; thread lanes "MainThread" and "Task-7 · MainThread"; thread lane sorts before its task lane
def test_call_becomes_complete_event():     # call ts=base+2000, return ts=base+5000 -> {"ph":"X","ts":2.0,"dur":3.0,"name":"Api.checkout","cat":"call"} with args file "/r/api.py:88"
    ...
def test_lifetime_coroutine_with_suspended_child(): ...   # one call X spanning call..return, one "(suspended)" X inside it
def test_innermost_suspension_only(): ...   # outer and inner both suspend (inner's interval contains outer's) -> exactly one "(suspended)" X, equal to inner's interval
def test_generator_drawn_per_run(): ...     # call, susp, res, susp, res, return -> three X events with args run 1, 2, 3
def test_coroutine_outside_task_drawn_per_run(): ...  # kind coroutine, task_id None, one suspension -> two X events
def test_unfinished_call_closed_at_process_end(): ... # no end -> dur reaches process end_ns, args["unfinished"] is True
def test_raise_crash_and_note_instants(): ...         # "raise ValueError" (s "t"), "CRASH: KeyError" (s "p"), "INTERRUPTED" for KeyboardInterrupt, "note"
def test_merge_two_services_pid_remap_and_names(): ...  # both captures use pid 4121 -> two distinct synthetic pids; names "orders · …", "payments · …"; payments' sort index > orders'
def test_duplicate_service_names_get_suffix(): ...      # "orders", "orders" -> metadata services == ["orders", "orders#2"]
def test_relative_ts_precision(): ...                   # an event 1 ns after base -> ts == 0.001; metadata tdb_base_epoch_ns == base
def test_nesting_valid_for_mixed_capture():             # coroutine lifetimes, generators, sync calls and an unfinished call on shared threads
    assert_nested(build_trace([mixed_capture()]))
```

Each test whose body is `...` gets explicit values, written from the rule its comment names.

- [ ] **Step 2: Run** `uv run pytest tests/unit/test_trace_perfetto.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `perfetto.py` and `tests/trace_checks.py`.
- [ ] **Step 4: Run** the same command. Expected: all pass.
- [ ] **Step 5: Commit** with the message "Render trace captures as Perfetto JSON".

---

### Task 8: `--trace` launch and CLI

**Files:**
- Create: `src/tdb/trace/launch.py`
- Modify: `src/tdb/languages/base.py` (add a `ProfileCapabilities.tracer: str | None = None` field, with a comment in the file's style)
- Modify: `src/tdb/languages/python.py:240` (`tracer="python"` in the Python profile's capabilities)
- Modify: `src/tdb/cli.py`:
  - flags in `build_parser`;
  - `_check_trace_args` called from `parse_args` right after the `--vi` early return;
  - dispatch in `main()` right after the `--vi` branch;
  - new `_run_trace(args)`.
- Test: `tests/unit/test_cli_trace_flags.py`, `tests/unit/test_trace_launch.py`, `tests/integration/test_trace_launch.py`

**Interfaces:**
- Consumes: `spec.load_spec`, `spec.unmatched_entries`, `contract.write_config`, `contract.build_env`, `capture.assemble`, `capture.write_capture`, `perfetto.write_trace`, and `tdb.languages.python_attach.pid_exists`.
- Produces in `launch.py`:
  - `class LaunchError(Exception)`;
  - `@dataclass class TraceRequest: mode: str; program: str | None; module: str | None; args: list[str]; python: str | None; cwd: str | None; name: str; out_base: Path; spec: TraceSpec | None = None; discover_out: Path | None = None; max_events: int = DEFAULT_MAX_EVENTS`;
  - `default_name(program: str | None, module: str | None) -> str`;
  - `default_out_base(name: str, now: datetime) -> Path`;
  - `resolve_command(req: TraceRequest) -> list[str]`;
  - `probe_version(interpreter: str) -> tuple[int, int]`;
  - `run(req: TraceRequest) -> int`, returning the exit code. Discover mode calls a hook Task 10 fills in. For now, `run` raises `NotImplementedError` for `mode == "discover"`.
- Produces in `cli.py`: the dests `trace`, `trace_name`, `trace_out`, `trace_merge` (`nargs="+"`), `trace_discover`, `trace_max_events` (`type=int`, `default=None`) and `trace_module` (`-m`, `metavar="MODULE"`). After validation:
  - `args.trace_spec: TraceSpec | None`;
  - `args.trace_args: list[str]`. With `-m` it is `[program, *args]` when a program positional is present, else `args`. Otherwise it is `args.args`.

CLI rules:
- `--trace`, `--trace-merge` and `--trace-discover` are mutually exclusive: `"--trace, --trace-merge and --trace-discover are mutually exclusive"`.
- `-m` is allowed only with `--trace` or `--trace-discover`: `"-m is supported only with --trace or --trace-discover"`.
- `--trace-name` and `--trace-max-events` need `--trace` or `--trace-discover`. `--trace-out` needs `--trace` or `--trace-merge`. The message is `"{flag} requires --trace"` (or `"... requires --trace or --trace-merge"` for `--trace-out`).
- `--trace`/`--trace-discover` need exactly one of a program and `-m`: `"--trace needs a program or -m MODULE"`.
- Conflicts give `"--trace cannot be combined with {flag}"` (with `--trace-discover`/`--trace-merge` in place of `--trace` when those are used). A flag conflicts when its attribute is truthy:
  - `--run`, `--examine-log`, `-e/--eval`, `-a/--attach` (attach_pid not None), `-r/--remote-attach`;
  - `-k/--breakpoint`, `-t/--to-line`;
  - `--record`, `--replay`, `--replay-tui`;
  - `--headless`, `--server`, `--mcp`, `--terminal`, `--post-mortem`, `--test`;
  - `--no-stop-on-entry`, `--no-just-my-code`, `--no-subprocess`;
  - `--adapter`, `--source`, `--keybindings`, `--local-root`, `--remote-root`, `--insecure-no-auth`.
- `--python`/`--pv` with a program that does not end in `.py`: `"--python/--pv apply only to a .py program or -m"`.
- `--lang X`: resolve with `registry.resolve(X)`; on failure, `parser.error` with its message. If `profile.capabilities.tracer is None`: `"--trace is not yet supported for {profile.display_name}"`.
- A `.py` program must exist (`"File not found: {program}"`). Store it as an absolute path.
- A `SpecError` gives `parser.error("invalid trace spec {path}:\n  " + "\n  ".join(problems))`.

Launch rules (spec "Three ways to name what runs" and "Running, stopping, exit code"):
- **Interpreter:** `args.python` (made absolute against the cwd), else `sys.executable`.
- **Command per form:**
  - form 1 → `[interp, abs_program, *args]`;
  - form 2 → `[interp, "-m", module, *args]`;
  - form 3 → `[shutil.which(program) or abs path if it exists, *args]`, else `LaunchError("command not found: {program}")`.
- **Version check (forms 1 and 2):** `probe_version` runs `[interp, "-I", "-c", "import sys; print(*sys.version_info[:2])"]` with a 10 s timeout. Below `(3, 12)` → `LaunchError(f"--trace needs Python 3.12 or newer; {interp} is {M}.{m}")`.
- **Output paths:** `parts_dir = out_base.with_name(out_base.name + ".parts")`. If it already exists → `LaunchError(f"{parts_dir} already exists (left by an earlier run?); merge it with --trace-merge or remove it")`.
- **Before spawning:** create `parts_dir`, write `run.json` and `config.json`, and spawn with `subprocess.Popen(cmd, cwd=req.cwd, env=build_env(os.environ, ...))`, with no pipes and no new session.
- **Signals while the program runs:**
  - SIGINT → print `tdb: waiting for {name} to exit` once;
  - POSIX SIGTERM/SIGHUP → `proc.send_signal` with the same signal;
  - Windows SIGTERM → `proc.terminate()`.

  Restore the previous handlers afterwards.
- **After the main process exits:** for up to `CHILD_FLUSH_WAIT` s, poll every 0.05 s until no part file without an `exit` record belongs to a pid for which `pid_exists` is true.
- **Exit code:** `returncode` if ≥ 0, else `128 + (-returncode)`, with `main_signal = signal.Signals(-returncode).name`.
- **Outputs:** assemble, then write `BASE.jsonl` and `BASE.perfetto.json`, then print the summary. Remove `parts_dir` only after both writes succeed. A failed write prints `tdb: error: could not write {path}: {exc}; events kept in {parts_dir}` and returns 1.
- **Summary lines, in order:**
  1. `tdb: trace "{name}": {n} process(es), {n} thread(s), {n} task(s), {n:,} call(s)`. Use the singular form for a count of 1, e.g. `1 process` and `3 processes`.
  2. `tdb: wrote {jsonl}`, then `tdb: wrote {perfetto}  (open in https://ui.perfetto.dev)`.
  3. One line per skipped marker: `tdb: warning: skipped {argv[0]} (pid {pid}): {reason}`.
  4. One line per agent error: `tdb: warning: tracer error in pid {pid}: {error}`.
  5. One line per abrupt pid: `tdb: warning: pid {pid} ended abruptly; its last events may be missing`.
  6. One line per note: `tdb: warning: {note}`.
  7. One line per unmatched entry: `tdb: never called: {entry}`.
  8. If no part files at all: `tdb: warning: no process was traced (is it Python 3.12+, started without -I/-E, with PYTHONPATH intact?)`.
- `_run_trace` builds the `TraceRequest` from `args`:
  - `name = args.trace_name or default_name(...)`;
  - `out_base = Path(args.trace_out) if args.trace_out else default_out_base(name, datetime.now())`;
  - `max_events = args.trace_max_events or DEFAULT_MAX_EVENTS`; a value below 1 is a parser error: `"--trace-max-events must be a positive integer"`.
- `_run_trace` catches `LaunchError` → `tdb: error: {msg}` and exit 2.

- [ ] **Step 1: Write the failing unit tests.** `tests/unit/test_cli_trace_flags.py`:

```python
@pytest.fixture
def spec(tmp_path):
    (tmp_path / "prog.py").write_text("def f():\n    pass\n")
    p = tmp_path / "s.yaml"; p.write_text("prog.py:\n  - f\n"); return str(p)

def err(capsys, argv):
    with pytest.raises(SystemExit) as ei:
        parse_args(argv)
    assert ei.value.code == 2
    return capsys.readouterr().err

def test_trace_parses_program_and_args(spec, tmp_path):
    a = parse_args(["--trace", spec, "--trace-name", "orders", str(tmp_path / "prog.py"), "--", "--port", "8001"])
    assert a.trace_spec.entries[0].functions[0].pattern == "f" and a.trace_args == ["--port", "8001"] and a.trace_name == "orders"

def test_m_takes_positionals_as_module_args(spec):
    a = parse_args(["--trace", spec, "-m", "orders.main", "--", "--port", "8001"])
    assert (a.trace_module, a.program, a.trace_args) == ("orders.main", "--port", ["--port", "8001"])

@pytest.mark.parametrize("extra,flag", [(["--run"], "--run"), (["-e", "1", "x"], "-e/--eval"), (["-a", "123"], "-a/--attach"),
    (["-k", "3"], "-k/--breakpoint"), (["--headless"], "--headless"), (["--no-subprocess"], "--no-subprocess"), (["--source", "x"], "--source")])
def test_trace_rejects_conflicting_flags(spec, tmp_path, capsys, extra, flag):
    assert f"--trace cannot be combined with {flag}" in err(capsys, ["--trace", spec, *extra, str(tmp_path / "prog.py")])

def test_modes_mutually_exclusive(spec, tmp_path, capsys):
    assert "mutually exclusive" in err(capsys, ["--trace", spec, "--trace-discover", "o.yaml", str(tmp_path / "prog.py")])

def test_m_requires_trace_mode(capsys):
    assert "-m is supported only with --trace or --trace-discover" in err(capsys, ["-m", "x"])

def test_trace_only_flags_need_trace(tmp_path, capsys):
    assert "--trace-name requires --trace" in err(capsys, ["--trace-name", "x", str(tmp_path)])

def test_trace_needs_program_or_module(spec, capsys):
    assert "--trace needs a program or -m MODULE" in err(capsys, ["--trace", spec])

def test_python_rejected_with_command_form(spec, capsys):
    assert "--python/--pv apply only to a .py program or -m" in err(capsys, ["--trace", spec, "--python", "/x/py", "uvicorn"])

def test_lang_without_tracer_rejected(spec, tmp_path, capsys):
    assert "--trace is not yet supported for Perl" in err(capsys, ["--trace", spec, "--lang", "perl", "svc"])

def test_spec_errors_reported_before_launch(tmp_path, capsys):
    (tmp_path / "s.yaml").write_text("missing.py:\n  - f\n")
    assert "missing.py: no such file" in err(capsys, ["--trace", str(tmp_path / "s.yaml"), "x.py"])
```

`tests/unit/test_trace_launch.py`:

```python
def test_default_name():
    assert default_name("services/orders/main.py", None) == "main"
    assert default_name(None, "orders.main") == "orders.main"
    assert default_name("/venv/bin/uvicorn", None) == "uvicorn"

def test_default_out_base(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert default_out_base("my svc", datetime(2026, 11, 25, 10, 42, 12)) == tmp_path / "my_svc-20261125-104212"

def test_resolve_command_forms(tmp_path, monkeypatch):
    prog = tmp_path / "p.py"; prog.write_text("")
    req = lambda **kw: TraceRequest(mode="trace", program=None, module=None, args=["a"], python=None, cwd=None, name="n", out_base=tmp_path / "o", **kw)
    assert resolve_command(req(program=str(prog))) == [sys.executable, str(prog), "a"]
    assert resolve_command(req(module="pkg.main")) == [sys.executable, "-m", "pkg.main", "a"]
    bindir = tmp_path / "bin"; bindir.mkdir(); exe = bindir / "svc"; exe.write_text("#!/bin/sh\n"); exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    assert resolve_command(req(program="svc")) == [str(exe), "a"]
    with pytest.raises(LaunchError, match="command not found: nope"):
        resolve_command(req(program="nope"))
```

(Skip the `PATH` part on Windows.)

- [ ] **Step 2: Write the failing integration tests** in `tests/integration/test_trace_launch.py`. They use a helper `tdb(tmp_path, *argv, **popen_kw)` that runs `[sys.executable, "-P", "-m", "tdb", *argv]` with `cwd=tmp_path`; the `*_proc` variant returns a Popen with `start_new_session=True` and stdout piped.

```python
def test_trace_writes_capture_and_perfetto(tmp_path):
    r = tdb(tmp_path, "--trace", spec_for(tmp_path, "f"), "--trace-out", "out", "prog.py")
    cap = load_capture(tmp_path / "out.jsonl")
    assert r.returncode == 0 and cap.header["name"] == "prog" and [e["function"] for e in cap.events if e["type"] == "call"] == ["f"]
    assert "traceEvents" in json.loads((tmp_path / "out.perfetto.json").read_text())
    assert not (tmp_path / "out.parts").exists() and "tdb: wrote" in r.stderr

def test_exit_code_passthrough(tmp_path): ...       # program sys.exit(7) -> returncode 7, header exit_code 7
def test_never_called_reported(tmp_path): ...      # spec f and g, only f runs -> "tdb: never called: prog.py: g" in stderr
def test_old_interpreter_refused(tmp_path): ...     # --python python3.11 (skip if missing) -> returncode 2, "needs Python 3.12 or newer"
def test_parts_dir_exists_refused(tmp_path): ...    # pre-create out.parts -> returncode 2, "already exists"
def test_module_form(tmp_path): ...                 # pkg/__main__.py + pkg/work.py, "-m pkg" -> call recorded, default name "pkg"
@posix
def test_console_script_and_shell_wrapper(tmp_path): ...  # bin/svc with "#!{sys.executable}" and bin/wrap.sh that execs python prog.py -> both traced
@posix
def test_zero_traced_processes_warning(tmp_path): ...     # command "true" -> "tdb: warning: no process was traced"

@posix
def test_sigint_with_calls_in_flight(tmp_path):  # Review Focus 1
    # prog: asyncio.run(main()) where main starts 3 tasks running traced `handle` (awaits asyncio.sleep(30))
    # and a daemon thread running traced `spin` (time.sleep loop; a non-daemon thread would block exit);
    # prints "ready" (flush=True) once all are running. Python dies by SIGINT after an uncaught
    # KeyboardInterrupt, so tdb reports 128 + 2.
    p = tdb_proc(tmp_path, "--trace", spec_for(tmp_path, "handle", "spin", "main"), "--trace-out", "out", "prog.py")
    assert p.stdout.readline().strip() == "ready"
    os.killpg(p.pid, signal.SIGINT)
    assert p.wait(30) == 130
    trace = json.loads((tmp_path / "out.perfetto.json").read_text())
    assert_nested(trace)
    calls = [e for e in load_capture(tmp_path / "out.jsonl").events if e["type"] == "call" and e["function"] == "handle"]
    assert len(calls) == 3

@posix
def test_sigterm_to_tdb_is_forwarded(tmp_path): ...   # program handles SIGTERM by printing "got TERM" and exiting 0; os.kill(tdb pid, SIGTERM) -> returncode 0, outputs written
@posix
def test_sigkill_of_program_still_yields_outputs(tmp_path): ...  # program prints os.getpid() then sleeps; kill -9 it -> tdb returncode 137, process ended "signal:SIGKILL"
```

`spec_for(tmp_path, *names)` writes `prog.py`-keyed specs. The test program's content goes in each test.

- [ ] **Step 3: Run** `uv run pytest tests/unit/test_cli_trace_flags.py tests/unit/test_trace_launch.py tests/integration/test_trace_launch.py -q`. Expected: FAIL.
- [ ] **Step 4: Implement** the capability field, `launch.py`, the CLI flags with their help texts (spec "Commands" table), `_check_trace_args`, `_run_trace`, and the `main()` dispatch, to the rules above.
- [ ] **Step 5: Run** the same command, plus `uv run pytest tests/unit/test_cli*.py -q` (the existing CLI tests must still pass). Expected: all pass.
- [ ] **Step 6: Commit** with the message "Add tdb --trace launch mode".

---

### Task 9: `--trace-merge`

**Files:**
- Create: `src/tdb/trace/merge.py`
- Modify: `src/tdb/cli.py` (validation; dispatch `args.trace_merge` → `_run_trace_merge(args)`)
- Test: `tests/unit/test_trace_merge.py`

**Interfaces:**
- Consumes: `capture.load_input`, `perfetto.write_trace`.
- Produces: `merge_main(inputs: list[str], out_base: str | None, *, now: datetime | None = None) -> int`, returning 0, or 2 on unreadable input.

Rules:
- `--trace-merge` takes no program (`"--trace-merge takes no program"`) and conflicts with the same flags as `--trace` (plus `-m`).
- The output is `{out_base}.perfetto.json`; the default base is `merged-%Y%m%d-%H%M%S` in the cwd.
- An input whose `[start_ns, end_ns]` overlaps no other input's range gives `tdb: warning: {name} does not overlap any other capture in time`.
- An unreadable input gives `tdb: error: cannot read {path}: {exc}` and returns 2.
- On success, print `tdb: wrote {path}  (open in https://ui.perfetto.dev)`.

- [ ] **Step 1: Write the failing tests:** write two captures with `capture.write_capture`, built from hand-made parts via `assemble`. Then:
  - `test_merge_writes_both_services`: the output's `process_name` values start with `"orders · "` and `"payments · "`.
  - `test_default_output_name`: `now=datetime(2026, 11, 25, 10, 42, 12)` → `merged-20261125-104212.perfetto.json`.
  - `test_parts_dir_input`.
  - `test_non_overlapping_warning`: capsys stderr contains `"payments does not overlap any other capture in time"`.
  - `test_unreadable_input_returns_2`.
  - `test_cli_rejects_program`: `parse_args(["prog.py", "--trace-merge", "a.jsonl"])` → `"--trace-merge takes no program"`. With `nargs="+"`, every positional after the flag is an input, so the check is `args.program is not None`.
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_trace_merge.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `merge.py` and the CLI wiring.
- [ ] **Step 4: Run** the same command. Expected: all pass.
- [ ] **Step 5: Commit** with the message "Add tdb --trace-merge".

---

### Task 10: `--trace-discover`

**Files:**
- Modify: `src/tdb/trace/agent/_tdb_trace_agent.py` (discover mode)
- Create: `src/tdb/trace/discover.py`
- Modify: `src/tdb/trace/launch.py` (`run` handles `mode == "discover"`)
- Modify: `src/tdb/cli.py` (dispatch `args.trace_discover` → `_run_trace`)
- Test: `tests/unit/test_trace_discover.py`, `tests/integration/test_trace_discover.py`

**Interfaces:**
- Consumes: the part `count` records and `launch.run`.
- Produces: `write_discovered_spec(parts_dir: Path, out_path: Path, *, program_desc: str, started: datetime, duration_s: float, cap: int = DISCOVER_CAP) -> int`, returning the number of functions written.

Agent discover mode:
- global `PY_START` only;
- user code excludes files under any `sysconfig.get_paths()` value for `stdlib`, `platstdlib`, `purelib` and `platlib` (normalized), files starting with `<`, `agent_dir`, qualnames containing `<`, and code without CO_OPTIMIZED (module and class bodies);
- non-user code returns `DISABLE`;
- user code increments the count, and at `cap` returns `DISABLE` and marks it capped;
- each flush emits `count` records for code whose count changed.

Launch discover:
- the parts dir is `out_path.with_name(out_path.name + ".parts")`;
- on exit, call `write_discovered_spec` and print `tdb: wrote {out} ({n} functions)`;
- remove the parts dir;
- the exit code works as in trace mode.

Writer rules:
- per `(file, qual, line)`, take the max `n` within each part, then sum over the parts;
- a function is capped if any part capped it or the sum ≥ cap;
- file keys are `os.path.relpath(file, out_path.parent)` with `/` separators, or the absolute path when relpath fails or starts with `..` more than 3 levels up;
- a key is double-quoted (`json.dumps`) only if it contains characters outside `[A-Za-z0-9_./-]`;
- files are sorted by key; functions within a file by line;
- names are `qual.replace(".", "::")`;
- the line format is `f"  - {name.ljust(width)}  # {count}{', async' if kind in ('coroutine', 'async_generator') else ''}{'  (hot)' if capped else ''}"`, where `count` is `f"{n:,} calls"` (`1 call` when n == 1), or `f"≥{cap:,} calls"` when capped, and `width` is the longest name in that file;
- there is a blank line between files;
- the first two lines are `# tdb --trace-discover: {program_desc}, {started:%Y-%m-%d %H:%M}, {duration}` (duration as `3m12s`, or `45s` under a minute) and `# Remove what you do not need; hot functions make traces large.`, then a blank line.

- [ ] **Step 1: Write the failing tests.**
  - Unit: from hand-made count parts, assert the exact output text of the format above. Use two parts that both count `api.py` `Api.checkout`, where the sum gives `"1,204 calls, async"`, and one capped function, where the line ends `"≥10,000 calls  (hot)"`. A file key with a space is quoted. `spec.load_spec(out)` must succeed on the result once the source files exist.
  - Integration `test_discover_end_to_end`: a program calls `f` 3 times, `g` 20,000 times, `C().m()` once and `async def a()` once, and uses `json.dumps`. The output must contain these lines, with each name padded to the width of the longest name in the file:
    - `"  - f     # 3 calls"`
    - `"  - g     # ≥10,000 calls  (hot)"`
    - `"  - C::m  # 1 call"`
    - `"  - a     # 1 call, async"`

    It must contain no `json` file, no `<lambda>`, and no `C` entry (class bodies are not functions).
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_trace_discover.py tests/integration/test_trace_discover.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** discover mode, `discover.py`, and the launch and CLI wiring.
- [ ] **Step 4: Run** the same command plus `tests/unit/test_cli_trace_flags.py`. Expected: all pass.
- [ ] **Step 5: Commit** with the message "Add tdb --trace-discover".

---

### Task 11: Examples, docs, packaging, end-to-end

**Files:**
- Create: `examples/Python/trace/inventory_service.py`, `examples/Python/trace/orders_service.py`, `examples/Python/trace/send_orders.py`, `examples/Python/trace/checkout.yaml`
- Create: `docs/python/tracing.md`
- Modify: `mkdocs.yml` (the nav entry `- Function Tracing: python/tracing.md` after `Process Inspector`), `README.md` (a short "Function Tracing" section that links the doc page)
- Modify: `pyproject.toml` package-data: add `"trace/agent/*.py"`, `"examples/Python/trace/*.py"`, `"examples/Python/trace/*.yaml"`
- Test: `tests/integration/test_trace_examples.py`, `tests/integration/test_trace_packaging.py`, `tests/integration/test_trace_overhead.py`

**Interfaces:**
- Consumes: everything above.
- Example services (stdlib only; asyncio streams on 127.0.0.1):
  - `inventory_service.py --port P --max-requests N` prints `listening on {port}` (port 0 = ephemeral). It answers `CHECK sku qty` lines through `loop.run_in_executor` on a `ThreadPoolExecutor(thread_name_prefix="db")` doing `time.sleep(0.01)`.
  - `orders_service.py --port P --inventory-port Q --max-requests N` prints `listening on {port}`. It handles `ORDER sku qty` by asking inventory, runs a `threading.Thread(name="heartbeat", daemon=True)`, and replies `OK`/`NO`.
  - `send_orders.py --port P --count N` sends N orders concurrently with `asyncio.gather`.
  - Both services exit 0 after N requests.
  - `checkout.yaml` names each service's handler, client and executor functions.

- [ ] **Step 1: Write the failing tests:**
  - `test_two_services_merge`:
    1. Start inventory with `tdb --trace checkout.yaml --trace-name inventory --trace-out inv … -- --port 0 --max-requests 4`, and read its port from the program's stdout.
    2. Start orders the same way (`--trace-name orders`, `--trace-out ord`, `--inventory-port`).
    3. Run `send_orders.py --count 4`.
    4. Wait for both services, then run `tdb --trace-merge inv.jsonl ord.jsonl --trace-out all`.

    Assert:
    - both exit 0;
    - the merged trace has process names starting `inventory · ` and `orders · `;
    - `assert_nested(trace)` holds;
    - the inventory capture has call events on a lane whose thread name starts with `db`;
    - the orders capture has task lanes.
  - `test_wheel_contains_agent_and_examples`: copy `pyproject.toml`, `README.md`, `LICENSE`, `mkdocs.yml`, `src/`, `docs/` and `examples/` (with `symlinks=True`, ignoring `build`, `*.egg-info` and `__pycache__`) to `tmp_path`. Then run `uv build --wheel --out-dir dist` there (skip if `uv` is missing) and assert the wheel's names include `tdb/trace/agent/sitecustomize.py`, `tdb/trace/agent/_tdb_trace_agent.py` and `tdb/examples/Python/trace/checkout.yaml`.
  - `test_overhead_untraced_loop`: skipped unless `TDB_TRACE_BENCH=1`. It times 1,000,000 calls of an untraced function under the agent against plain Python, prints both times, and asserts the traced time is below 2× the plain time.
- [ ] **Step 2: Run** `uv run pytest tests/integration/test_trace_examples.py tests/integration/test_trace_packaging.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the examples and the package-data globs. Write `docs/python/tracing.md`, covering:
  - the spec format with examples;
  - the three launch forms;
  - the two-terminal walk-through using the examples, then merge;
  - reading the trace in ui.perfetto.dev (lanes, `(suspended)`, per-run generator slices, wall time in the slice details);
  - discovery;
  - output files and the capture schema;
  - limitations: Python 3.12+; no free-threaded builds; children started with `-I`/`-E`/`-S` or a replaced environment are invisible; the `tdb-trace-flush` thread is visible in `threading.enumerate()`; `os.fork()` called from `__main__` may now show Python's multi-threaded-fork DeprecationWarning; a hard crash loses at most about 0.5 s of events.

  Add the README section and the mkdocs nav entry.
- [ ] **Step 4: Run** the same command, then the full suite with `memcap-pytest`. Expected: all pass. Compare any failure outside `tests/**/test_trace*` against a `main` worktree before calling it pre-existing.
- [ ] **Step 5: Commit** with the message "Add tracing examples, docs and packaging".

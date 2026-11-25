"""The tracer for ``tdb --trace``, loaded into the traced program.

``sitecustomize.py`` calls ``start(config)`` at interpreter start-up.  The
tracer enables ``sys.monitoring`` ``PY_START`` globally; code that is not
a spec'd function returns ``DISABLE`` from its first ``PY_START`` and never
calls back again.  Spec'd functions get local ``PY_RETURN``/``PY_YIELD``/
``PY_RESUME`` events.  Records go to an in-memory buffer that a daemon
thread writes to ``PARTS/<pid>.jsonl`` every ``flush_interval`` seconds;
``final_flush`` writes the rest at ``atexit`` or ``os._exit``.

Never break the program:
- stdlib only, and never ``import tdb``;
- every monitoring callback catches ``Exception`` (an exception escaping
  a callback is raised in the user's code); only the program's own
  BaseException-only exceptions, from its signal handlers, pass through
  (see "monitoring callbacks" below);
- the part file is written with ``os.write`` on a raw descriptor, never a
  buffered file object, whose lock a ``fork`` could inherit held;
- no strong references to frames (one would delay generator finalization);
- the agent's own files and its flusher thread are never traced.

The part-file record formats are in the plan's "Shared formats" section.
Per-part state (buffer, live calls, call ids, written definitions) is
kept apart from per-process matching caches, so a forked child can start
a new part while keeping what it inherited about code objects.
"""

import atexit
import fnmatch
import itertools
import json
import os
import sys
import threading
import time

_mon = sys.monitoring
_EV = _mon.events
DISABLE = _mon.DISABLE

TOOL_NAME = "tdb-trace"
# The unnamed ids.  Never DEBUGGER_ID (0), COVERAGE_ID (1), PROFILER_ID (2:
# taking it makes cProfile in the program fail) or OPTIMIZER_ID (5).
_TOOL_IDS = (3, 4)

_CO_OPTIMIZED = 0x1  # a function body (not module, class or exec code)
_CO_GENERATOR = 0x20
_CO_COROUTINE = 0x80
_CO_ASYNC_GENERATOR = 0x200

_GLOBAL_EVENTS = _EV.PY_START | _EV.PY_UNWIND | _EV.PY_THROW
_LOCAL_EVENTS = _EV.PY_RETURN | _EV.PY_YIELD | _EV.PY_RESUME

_getframe = sys._getframe
_get_ident = threading.get_ident
_get_native_id = threading.get_native_id
_perf_ns = time.perf_counter_ns
# Same output as json.dumps(obj, separators=(",", ":")), without building
# an encoder per call.
_dumps = json.JSONEncoder(separators=(",", ":")).encode

# --- set by start() ----------------------------------------------------
_tool = None  # claimed sys.monitoring tool id
_fd = None  # part file descriptor (O_APPEND)
_part_path = None
_stopped = True  # callbacks return at once while True
_final_done = False
_flusher_ident = None  # threading ident of the flusher; never traced
_flush_lock = threading.Lock()
_interval = 0.25
_targets = ()
_agent_dirs = frozenset()

# --- per-process matching caches (still valid in a forked child) -------
_file_funcs = {}  # co_filename -> (norm file, ((pattern, wild, entry), ...)) or None
_decisions = {}  # (co_filename, co_qualname, co_firstlineno) -> _Fn or None
_armed = {}  # id(code) -> (code, _Fn): code with local events on
_fn_ids = itertools.count(1)

# --- per-part state (a new part file starts these afresh) --------------
_buf = []  # records not yet written
_live = {}  # id(frame) -> (call id, code, _Fn) for recorded calls
_call_ids = itertools.count(1)
_fn_written = set()  # _Fn ids whose "fn" record is in this part
_thread_names = {}  # native thread id -> name last written
_proc_name = "MainProcess"

# --- counters (stats()) ------------------------------------------------
_n_starts = 0  # PY_START callbacks
_n_events = 0  # call/ret/raise/susp/res records


class _Fn:
    """A spec'd function: its ``fn`` record fields and its spec entry."""

    __slots__ = ("id", "file", "qual", "line", "kind", "entry")

    def __init__(self, file, qual, line, kind, entry):
        self.id = next(_fn_ids)
        self.file = file
        self.qual = qual
        self.line = line
        self.kind = kind
        self.entry = entry


class _Target:
    __slots__ = ("files", "suffixes", "functions")

    def __init__(self, target):
        self.files = frozenset(target["files"])
        self.suffixes = tuple(
            "/" + os.path.normcase(s).replace("\\", "/") for s in target["suffixes"]
        )
        self.functions = tuple(
            (f["pattern"], any(c in f["pattern"] for c in "*?["), f)
            for f in target["functions"]
        )


def _norm_path(path):
    return os.path.normcase(os.path.realpath(path))


def _kind(flags):
    if flags & _CO_COROUTINE:
        return "coroutine"
    if flags & _CO_ASYNC_GENERATOR:
        return "async_generator"
    if flags & _CO_GENERATOR:
        return "generator"
    return "function"


# --- matching -----------------------------------------------------------


def _funcs_for_file(filename):
    """Spec entries that apply to code from ``filename``, or None."""
    nfile = _norm_path(filename)
    if os.path.dirname(nfile) in _agent_dirs:
        return None
    slashed = nfile.replace("\\", "/")
    funcs = []
    for t in _targets:
        if nfile in t.files or slashed.endswith(t.suffixes):
            funcs.extend(t.functions)
    return (nfile, tuple(funcs)) if funcs else None


def _match(code):
    """The _Fn for a code object the spec names, or None."""
    found = _file_funcs.get(code.co_filename, False)
    if found is False:
        found = _file_funcs[code.co_filename] = _funcs_for_file(code.co_filename)
    if found is None:
        return None
    nfile, funcs = found
    qual = code.co_qualname.replace(".<locals>", "")
    for pattern, wild, entry in funcs:
        # Wildcards never pick up lambdas, comprehensions and the like.
        if wild and "<" in qual:
            continue
        if fnmatch.fnmatchcase(qual, pattern):
            return _Fn(nfile, qual, code.co_firstlineno, _kind(code.co_flags), entry)
    return None


def _arm(code):
    """Decide whether ``code`` is traced; if so, turn on its local events."""
    if not code.co_flags & _CO_OPTIMIZED:  # module, class or exec body
        return None
    if code.co_filename.startswith("<"):
        # <string>, <frozen os>, <attrs generated init ...>: no spec file
        # can name these, and caching them could grow without bound.
        return None
    key = (code.co_filename, code.co_qualname, code.co_firstlineno)
    if key in _decisions:
        fn = _decisions[key]
    else:
        fn = _decisions[key] = _match(code)
    if fn is not None:
        _mon.set_local_events(_tool, code, _LOCAL_EVENTS)
        _armed[id(code)] = (code, fn)
    return fn


# --- context ------------------------------------------------------------


def _thread_tid():
    """Native id of the current thread; writes a ``th`` record on a new name."""
    tid = _get_native_id()
    # Never threading.current_thread(): it makes dummy Thread objects.
    th = threading._active.get(_get_ident())
    name = th.name if th is not None else f"thread-{tid}"
    if _thread_names.get(tid) != name:
        _thread_names[tid] = name
        _buf.append({"k": "th", "tid": tid, "name": name})
    return tid


def _current_proc_name():
    mp = sys.modules.get("multiprocessing")  # never imported by the agent
    if mp is None:
        return "MainProcess"
    try:
        return mp.current_process().name
    except Exception:  # e.g. multiprocessing still being imported
        return None


def _check_proc_name():
    global _proc_name
    name = _current_proc_name()
    if name is not None and name != _proc_name:
        _proc_name = name
        _buf.append({"k": "proc", "t": _perf_ns(), "name": name})


# --- monitoring callbacks -----------------------------------------------
#
# Each callback catches Exception, so a failure of the agent never reaches
# the program.  BaseException-only exceptions (KeyboardInterrupt,
# SystemExit, ...) pass through on purpose: they are the program's own,
# raised by a signal handler (or PyThreadState_SetAsyncExc) that Python ran
# while the callback was executing.  Swallowing them would lose a Ctrl-C
# or a SIGTERM -> sys.exit() handler; letting them through raises them at
# the traced function's entry, return or unwind, as an untraced program
# would see them at some nearby instruction.  State is updated so that an
# interruption at any point leaves at worst an unfinished call.


def _on_start(code, offset):
    global _n_starts, _n_events
    try:
        if _stopped:
            return None
        _n_starts += 1
        hit = _armed.get(id(code))
        if hit is not None and hit[0] is code:
            fn = hit[1]
        else:
            fn = _arm(code)
            if fn is None:
                return DISABLE
        if _get_ident() == _flusher_ident:
            return None
        t = _perf_ns()
        tid = _thread_tid()
        if fn.id not in _fn_written:
            _fn_written.add(fn.id)
            _buf.append(
                {
                    "k": "fn",
                    "id": fn.id,
                    "file": fn.file,
                    "qual": fn.qual,
                    "line": fn.line,
                    "kind": fn.kind,
                }
            )
        c = next(_call_ids)
        _buf.append(
            {"k": "call", "t": t, "c": c, "fn": fn.id, "tid": tid, "task": None}
        )
        _n_events += 1
        _live[id(_getframe(1))] = (c, code, fn)
    except Exception:
        pass
    return None


def _on_return(code, offset, retval):
    global _n_events
    try:
        if _stopped:
            return None
        t = _perf_ns()
        live = _live.pop(id(_getframe(1)), None)
        if live is None or live[1] is not code:
            return None
        _buf.append(
            {"k": "ret", "t": t, "c": live[0], "tid": _thread_tid(), "task": None}
        )
        _n_events += 1
    except Exception:
        pass
    return None


def _on_yield(code, offset, retval):
    # Task 3 records "susp" here.
    return None


def _on_resume(code, offset):
    # Task 3 records "res" here.
    return None


def _on_throw(code, offset, exc):
    # Global event: must stay cheap for untraced code; never DISABLE.
    # Task 3 records "res" here for a live traced frame.
    return None


def _on_unwind(code, offset, exc):
    # Global event: must stay cheap for untraced code; never DISABLE.
    try:
        if _stopped:
            return None
        hit = _armed.get(id(code))
        if hit is None or hit[0] is not code:
            return None
        # The frame is gone after this; drop its call so it cannot leak.
        # Task 3 records "raise" here.
        _live.pop(id(_getframe(1)), None)
    except Exception:
        pass
    return None


_CALLBACKS = (
    (_EV.PY_START, _on_start),
    (_EV.PY_RETURN, _on_return),
    (_EV.PY_YIELD, _on_yield),
    (_EV.PY_RESUME, _on_resume),
    (_EV.PY_THROW, _on_throw),
    (_EV.PY_UNWIND, _on_unwind),
)


# --- writing --------------------------------------------------------------


def _open_part(parts_dir):
    """Create ``<pid>.jsonl`` (or ``<pid>-N.jsonl``) exclusively; return (fd, path)."""
    pid = os.getpid()
    flags = (
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND | getattr(os, "O_BINARY", 0)
    )
    for n in range(1, 1000):
        path = os.path.join(parts_dir, f"{pid}.jsonl" if n == 1 else f"{pid}-{n}.jsonl")
        try:
            return os.open(path, flags, 0o666), path
        except FileExistsError:
            continue
    raise FileExistsError(f"no free part file name for pid {pid} in {parts_dir}")


def _write_buffer():
    """Write and drop the buffered records.  The caller holds _flush_lock."""
    n = len(_buf)
    if not n:
        return
    batch = _buf[:n]  # other threads only append, so the first n are ours
    del _buf[:n]
    data = memoryview(("\n".join(map(_dumps, batch)) + "\n").encode("utf-8"))
    while data:
        data = data[os.write(_fd, data) :]


def _flush():
    """One periodic flush.  The caller holds _flush_lock."""
    _check_proc_name()
    _write_buffer()


def _flusher_loop():
    global _flusher_ident
    _flusher_ident = _get_ident()
    # time.sleep, not Event.wait: each wake-up then runs only code that
    # start() already ran, so no new PY_START callbacks come from here.
    while not _stopped:
        time.sleep(_interval)
        try:
            with _flush_lock:
                if not _stopped:  # final_flush wrote the last records
                    _flush()
        except BaseException:
            pass


_real_os_exit = os._exit


def _exit(status):
    """``os._exit`` while tracing: write the trace's last records first."""
    final_flush("_exit")
    _real_os_exit(status)


# --- public ---------------------------------------------------------------


def pick_tool_id():
    """The first free tool id of 3 and 4, or None."""
    for tool in _TOOL_IDS:
        if _mon.get_tool(tool) is None:
            return tool
    return None


def stats():
    return {"py_start_callbacks": _n_starts, "recorded_events": _n_events}


def start(config):
    """Start tracing this process.  Raises if it cannot (no free tool id,
    bad config, parts directory not writable); sitecustomize.py turns
    that into an agent-error marker."""
    global _tool, _fd, _part_path, _stopped, _flusher_ident, _interval
    global _targets, _agent_dirs, _proc_name
    if _tool is not None or _fd is not None:
        raise RuntimeError("the tracer is already started in this process")
    if config.get("version") != 1:
        raise ValueError(f"unsupported trace config version {config.get('version')!r}")
    mode = config["mode"]
    parts_dir = config["parts"]
    targets = tuple(_Target(t) for t in config["targets"])
    interval = float(config["flush_interval"])
    agent_dirs = {_norm_path(os.path.dirname(os.path.abspath(__file__)))}
    if config.get("agent_dir"):
        agent_dirs.add(_norm_path(config["agent_dir"]))

    tool = pick_tool_id()
    if tool is None:
        raise RuntimeError("sys.monitoring tool ids 3 and 4 are both in use")
    _mon.use_tool_id(tool, TOOL_NAME)
    _tool = tool
    try:
        _targets, _interval, _agent_dirs = targets, interval, frozenset(agent_dirs)
        _fd, _part_path = _open_part(parts_dir)
        _proc_name = _current_proc_name() or "MainProcess"
        wall = time.time_ns()
        pc = _perf_ns()
        _buf.append(
            {
                "k": "hdr",
                "pid": os.getpid(),
                "ppid": os.getppid(),
                "proc": _proc_name,
                "argv": list(sys.argv),
                "py": "%d.%d.%d" % tuple(sys.version_info[:3]),
                "wall": wall,
                "pc": pc,
                "mode": mode,
            }
        )
        for event, callback in _CALLBACKS:
            _mon.register_callback(tool, event, callback)
        _stopped = False
        _mon.set_events(tool, _GLOBAL_EVENTS)
        # Write the header now, through the same code the flusher runs.
        with _flush_lock:
            _flush()
        flusher = threading.Thread(
            target=_flusher_loop, name="tdb-trace-flush", daemon=True
        )
        flusher.start()
        _flusher_ident = flusher.ident
        atexit.register(final_flush, "atexit")
        os._exit = _exit
    except BaseException:
        _teardown()
        if _fd is not None:
            try:
                os.close(_fd)
                os.unlink(_part_path)
            except OSError:
                pass
            _fd = None
        raise


def _teardown():
    """Stop all callbacks and give the tool id back."""
    global _stopped, _tool
    _stopped = True
    tool, _tool = _tool, None
    if tool is None:
        return
    for event, _ in _CALLBACKS:
        try:
            _mon.register_callback(tool, event, None)
        except BaseException:
            pass
    try:
        _mon.set_events(tool, 0)
    except BaseException:
        pass
    for code, _ in list(_armed.values()):
        try:
            _mon.set_local_events(tool, code, 0)
        except BaseException:
            pass
    try:
        _mon.free_tool_id(tool)
    except BaseException:
        pass


def final_flush(how):
    """Write an ``exit`` record and the rest of the buffer, then stop.

    ``how`` is "atexit" or "_exit".  Idempotent, and never raises.
    """
    global _final_done, _stopped
    try:
        if _final_done or _fd is None:
            return
        _final_done = True
        _stopped = True
        # Bounded wait: a signal handler calling os._exit can land while
        # this thread itself holds the lock.
        locked = _flush_lock.acquire(timeout=1.0)
        try:
            _check_proc_name()  # pool workers are named after start-up
            _buf.append({"k": "exit", "t": _perf_ns(), "how": how})
            _write_buffer()
        finally:
            if locked:
                _flush_lock.release()
    except BaseException:
        pass
    try:
        _teardown()
    except BaseException:
        pass

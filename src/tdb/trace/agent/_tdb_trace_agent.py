"""The tracer for ``tdb --trace``, loaded into the traced program.

``sitecustomize.py`` calls ``start(config)`` at interpreter start-up.  The
tracer enables ``sys.monitoring`` ``PY_START`` globally; code that is not
a spec'd function returns ``DISABLE`` from its first ``PY_START`` and never
calls back again.  Spec'd functions get local ``PY_RETURN``/``PY_YIELD``/
``PY_RESUME`` events; ``PY_THROW``/``PY_UNWIND`` are global (they cannot be
disabled per code object) and return at once for untraced code.  Records
go to an in-memory buffer that a hidden flusher thread writes to
``PARTS/<pid>.jsonl`` every ``flush_interval`` seconds; ``final_flush``
writes the rest at ``atexit`` or ``os._exit``.  Wrappers around
``sys.excepthook`` and ``threading.excepthook``, which ``sitecustomize.py``
installs with ``install_crash_hooks()`` after ``start()``, record uncaught
exceptions.  A forked child starts a part file of its own (see "fork");
other children load the hook themselves.  In discover mode (``tdb
--trace-discover``) the tracer only counts the calls of the program's own
functions (see "discovery").

Never break the program:
- stdlib only, and never ``import tdb``;
- every monitoring callback catches ``Exception`` (an exception escaping
  a callback is raised in the user's code); only the program's own
  exceptions, from its signal handlers, pass through (see "monitoring
  callbacks" below);
- the program's code that the agent runs (``capture`` and ``if``
  expressions, ``__repr__``, ``__str__``) never raises into the program
  either, and runs under a re-entrancy guard (see "options");
- the part file is written with ``os.write`` on a raw descriptor, never a
  buffered file object, whose lock a ``fork`` could inherit held;
- no strong references to frames (one would delay generator finalization);
- the agent's own files and its flusher thread are never traced, and the
  flusher is a bare ``_thread`` thread, invisible to ``threading`` and
  stopped while the process forks;
- no thread ever waits, beyond a short bound, for another: the flusher can
  run the program's code (garbage-collection finalizers run in whichever
  thread allocates), and that code may wait for a lock the waiter holds.
  So there are no agent locks during the run at all (see "writing").

The part-file record formats are in the plan's "Shared formats" section.
Per-part state (buffer, live calls, call ids, written definitions) is
kept apart from per-process matching caches, so a forked child can start
a new part while keeping what it inherited about code objects.
"""

import _thread
import atexit
import builtins
import errno
import fnmatch
import itertools
import json
import os
import reprlib
import sys
import threading
import time
import weakref

# Loaded already (by fnmatch, through re), so free to import.
from functools import partial as _partial
from types import FunctionType as _FunctionType
from types import MethodType as _MethodType

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

# Writing (see "writing" below for who writes when).
_CHUNK = 10_000  # records per os.write
# Backpressure: a record that takes the backlog to _HIGH_WATER (about
# 15 MB) makes its thread wait, lock-free and for at most _PRESSURE_WAIT
# seconds, until the flusher has brought it under _LOW_WATER.  This bounds
# memory and what a hard crash can lose while the flusher keeps up.
_HIGH_WATER = 100_000
_LOW_WATER = _HIGH_WATER // 2
_PRESSURE_WAIT = 0.05
# final_flush waits this long for the flusher to stop, then takes over.
_STOP_WAIT = 2.0
# ... plus this long for an os.write the flusher already issued to land.
_LANDING_WAIT = 3.0
# The same two in a forked child, short enough (with the writing) for tdb,
# which waits 2.0 s (CHILD_FLUSH_WAIT) for children once the program has
# exited.  The takeover is exact after any wait; the landing wait matters
# only on storage where a write hangs.
_CHILD_STOP_WAIT = 0.25
_CHILD_LANDING_WAIT = 1.5
# A fork waits this long for the flusher to write what is buffered and
# stop, then goes ahead without stopping it ...
_FORK_STOP_WAIT = 0.5
# ... and, once it stopped, this long for its thread to leave the process.
_GONE_WAIT = 0.1
# Starting a flusher waits this long for its thread to run.
_START_WAIT = 0.1

_getframe = sys._getframe
_get_ident = threading.get_ident
_get_native_id = threading.get_native_id
_perf_ns = time.perf_counter_ns
# Bound now, before the program runs: what it does to these modules later
# (gevent's monkey-patching, mock.patch, pyfakefs) must not reach the
# flusher, the final flush or the fork hooks.
_sleep = time.sleep
_time_ns = time.time_ns
_os_open = os.open
_os_write = os.write
_os_close = os.close
_unlink = os.unlink
_fstat = os.fstat
_os_stat = os.stat
_getpid = os.getpid
_getppid = os.getppid
_path_join = os.path.join
_allocate_lock = _thread.allocate_lock
_start_new_thread = _thread.start_new_thread
# Same output as json.dumps(obj, separators=(",", ":")), without building
# an encoder per call.  Records are buffered as their JSON text.
_dumps = json.JSONEncoder(separators=(",", ":")).encode
# The hot fixed-shape records, formatted directly: the same text _dumps
# gives for their int and null fields, at a sixth of the cost.  A record
# holding any string (a name, a captured value) must go through _dumps.
# "task" takes the task id or the string "null" (see _task_id).
_CALL = '{"k":"call","t":%d,"c":%d,"fn":%d,"tid":%d,"task":%s}'
_RET = '{"k":"ret","t":%d,"c":%d,"tid":%d,"task":%s}'
_SUSP = '{"k":"susp","t":%d,"c":%d,"tid":%d,"task":%s}'
_RES = '{"k":"res","t":%d,"c":%d,"tid":%d,"task":%s}'
# A crash record keeps at most this many lines of the traceback.
_TB_LINES = 50
# The file name of the code compiled from capture and if expressions.
_EXPR_FILE = "<tdb-trace>"
# Code whose frames in a traceback are the agent's, not the program's.
_OWN_FILES = frozenset((_getframe().f_code.co_filename, _EXPR_FILE))
_SKIP = object()  # _enter(): the call is not recorded

# --- set by start() ----------------------------------------------------
_tool = None  # claimed sys.monitoring tool id
_fd = None  # part file descriptor (O_APPEND)
_part_path = None
_part_id = None  # (st_dev, st_ino) of the part file: is _fd still ours?
_part_pid = None  # the process the part is for: a forked child never writes it
_parts_dir = None
_mode = None  # "trace" or "discover", for the part header
_stopped = True  # callbacks return at once while True
_final_done = False
_broken = False  # a part-file write failed; nothing more is written
_interval = 0.25
_repr_limit = 200  # characters kept of a value or exception text
_repr = None  # the _Repr that formats values
_max_events = 5_000_000  # the event cap (call/ret/raise/susp/res records)
_targets = ()
_agent_dirs = frozenset()
_signal_nums = None  # signal numbers, once _from_signal_handler needs them
# Discover mode (see "discovery"):
_discover_cap = 10_000  # calls after which a function is no longer counted
_lib_dirs = ()  # library directories (normalized, ending in os.sep)
_root_paths = frozenset()  # user roots (normalized): the program's file, ...
_root_dirs = ()  # ... and the same, ending in os.sep: its directory

# --- the flusher ---------------------------------------------------------
# Whether there is a flusher: not None while one runs (its thread ident) or
# is being started (its start token); see _start_flusher.  The flusher's
# thread is never traced.
_flusher_ident = None
_flusher_tid = None  # its native thread id (set by the flusher)
_stop_wait, _landing_wait = _STOP_WAIT, _LANDING_WAIT  # final_flush's waits
# Held while the flusher may sleep: its timed acquire is the sleep, and a
# release wakes it at once (backpressure, final_flush, a fork).
_wake = _allocate_lock()
# Held while a flusher runs; it releases this when it ends.
_flusher_exited = _allocate_lock()
# Forks' requests that the flusher stop (see "fork"): a forking thread
# appends one; the flusher writes what is buffered, then takes one and
# ends; a forking thread that stops waiting takes one back.  Both take with
# "if _halt: del _halt[0]", which has no call in it, so no other thread and
# no signal handler runs in between: one request is taken once.
_halt = []
# _flushed when a fork last stopped waiting for the flusher: while it has
# not moved since, a fork does not wait for it again.
_fork_stall = -1
_fence = False  # set by final_flush: the flusher must not write again
# The chunk the flusher is writing, for final_flush to take over from:
# (its first record object, part size before the write, bytes, records).
_inflight = None
_in_syscall = False  # the flusher has called os.write for _inflight
_flushed = 0  # records the flusher has written (its progress)
_stall_mark = -1  # _flushed when a backpressure wait last saw no progress
_stall_noted = False

# --- per-process matching caches (still valid in a forked child) -------
_file_funcs = {}  # co_filename -> (norm file, (_Target.functions items)) or None
_decisions = {}  # (co_filename, co_qualname, co_firstlineno) -> _Fn or None
_armed = {}  # id(code) -> (code, _Fn): code with local events on
_fn_ids = itertools.count(1)
_user_files = {}  # co_filename -> norm file, or None: not user code (discover)

# --- per-part state (a new part file starts these afresh) --------------
_buf = []  # JSON text of the records not yet written, oldest first
_live = {}  # id(frame) -> (call id, code, _Fn) for recorded calls
_call_ids = itertools.count(1)
_fn_written = set()  # _Fn ids whose "fn" record is in this part
_thread_names = {}  # native thread id -> name last written
# asyncio task -> [task id, name last written].  Weak keys: the tracer
# never keeps a task (or, through it, its coroutine's frames) alive.
_task_ids = weakref.WeakKeyDictionary()
_task_counter = itertools.count(1)
_proc_name = "MainProcess"
_n_dropped = 0  # calls not recorded because of the event cap
_cap_noted = False
# Discover mode's counts (see "discovery").
_counts = {}  # id(code) -> (code, _Count)
_count_keys = {}  # (norm file, qual, line) -> _Count
_changed = set()  # the _Counts counted since the flusher last wrote them

# Idents of the threads running the program's code for a callback (the
# re-entrancy guard; see "options").
_inside = set()

# --- the hooks start() wrapped (crash records) --------------------------
_prev_excepthook = None
_prev_thread_excepthook = None

# --- counters (stats()) ------------------------------------------------
_n_starts = 0  # PY_START callbacks
_n_events = 0  # call/ret/raise/susp/res records


class _Fn:
    """A spec'd function: its ``fn`` record and its spec entry's options:
    ``opts`` (an _Opts, or None when the entry has no capture, if or
    max_hits) and ``ret`` (record the return value)."""

    __slots__ = ("id", "file", "qual", "line", "kind", "opts", "ret", "record")

    def __init__(self, file, qual, line, kind, opts, ret):
        self.id = next(_fn_ids)
        self.file = file
        self.qual = qual
        self.line = line
        self.kind = kind
        self.opts = opts
        self.ret = ret
        self.record = _dumps(
            {
                "k": "fn",
                "id": self.id,
                "file": file,
                "qual": qual,
                "line": line,
                "kind": kind,
            }
        )


def _compile(text):
    """An expression's code, or the error text if it does not compile.  tdb
    compiles every expression before launch, but with its own Python; the
    program's may have another grammar."""
    try:
        return compile(text, _EXPR_FILE, "eval", dont_inherit=True)
    except Exception as exc:  # SyntaxError, ValueError (a null byte), ...
        return _error_text(exc)


class _Opts:
    """A spec entry's capture, if and max_hits, compiled once at start.
    ``hits`` counts the calls recorded for the entry, of every function it
    matches."""

    __slots__ = ("capture", "cond", "max_hits", "hits")

    def __init__(self, entry):
        self.capture = tuple((text, _compile(text)) for text in entry["capture"])
        self.cond = None if entry["if"] is None else _compile(entry["if"])
        self.max_hits = entry["max_hits"]
        self.hits = 0
        if self.cond.__class__ is str:
            # An if that does not compile is one that always raises: no
            # call is recorded, and nothing needs to run to know that.
            self.cond, self.max_hits = None, 0


class _Target:
    __slots__ = ("files", "suffixes", "functions")

    def __init__(self, target):
        self.files = frozenset(target["files"])
        self.suffixes = tuple(
            "/" + os.path.normcase(s).replace("\\", "/") for s in target["suffixes"]
        )
        # (pattern, has wildcards, _Opts or None, record the return value)
        self.functions = tuple(
            (
                f["pattern"],
                any(c in f["pattern"] for c in "*?["),
                _Opts(f)
                if f["capture"] or f["if"] is not None or f["max_hits"] is not None
                else None,
                bool(f["return"]),
            )
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
    """The _Fn for a code object the spec names, or None.

    One entry decides, so a function that several entries match is
    recorded once, with that entry's options: a pattern without wildcards
    beats one with them; otherwise the first in spec order wins."""
    found = _file_funcs.get(code.co_filename, False)
    if found is False:
        found = _file_funcs[code.co_filename] = _funcs_for_file(code.co_filename)
    if found is None:
        return None
    nfile, funcs = found
    qual = code.co_qualname.replace(".<locals>", "")
    best = None
    for item in funcs:
        if not item[1]:
            if qual == item[0]:  # fnmatch without wildcards: equality
                best = item
                break
        # Wildcards never pick up lambdas, comprehensions and the like.
        elif best is None and "<" not in qual and fnmatch.fnmatchcase(qual, item[0]):
            best = item
    if best is None:
        return None
    kind = _kind(code.co_flags)
    return _Fn(nfile, qual, code.co_firstlineno, kind, best[2], best[3])


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
        _buf.append(_dumps({"k": "th", "tid": tid, "name": name}))
    return tid


def _task_id(tid):
    """Id of the running asyncio task, or None; writes a ``task`` record for
    a new task or a new name.  Never imports asyncio: a program that has not
    imported it runs no task."""
    aio = sys.modules.get("asyncio")
    if aio is None:
        return None
    try:
        loop = aio._get_running_loop()
        if loop is None:
            return None
        task = aio.current_task(loop)
        if task is None:
            return None
        entry = _task_ids.get(task)
        if entry is None:
            entry = _task_ids[task] = [next(_task_counter), None]
        name = task.get_name()
        if entry[1] != name:
            record = _dumps({"k": "task", "id": entry[0], "name": name, "tid": tid})
            _buf.append(record)
            entry[1] = name
        return entry[0]
    except Exception as exc:  # asyncio half imported, an odd task class, ...
        if _from_signal_handler(exc):
            raise
        return None


def _exc_text(exc):
    """``"ValueError: bad"`` (just the type name for an empty message), cut
    to repr_limit.  ``str()`` is the program's code: it may fail."""
    name = type(exc).__name__
    try:
        msg = str(exc)
    except BaseException as err:
        if _signalled(err):
            raise
        msg = ""
    return (f"{name}: {msg}" if msg else name)[:_repr_limit]


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
        _buf.append(_dumps({"k": "proc", "t": _perf_ns(), "name": name}))


# --- signal handlers' exceptions -------------------------------------------
#
# Python runs signal handlers inside monitoring callbacks too (at any
# eval-breaker check), so an exception a handler raises (an alarm-style
# timeout's TimeoutError, say) can surface in a callback's ``except``.  It
# is the program's and must reach it; an exception that does not come
# from a handler is the agent's, or a user expression's, and must not.


def _handler_code(handler):
    """The code a signal handler runs, through functools.partial and bound
    methods; None for SIG_DFL, SIG_IGN and builtins."""
    for _ in range(10):
        if isinstance(handler, _partial):
            handler = handler.func
        elif isinstance(handler, _MethodType):
            handler = handler.__func__
        else:
            break
    return handler.__code__ if isinstance(handler, _FunctionType) else None


def _from_signal_handler(exc):
    """Whether ``exc`` came from one of the program's Python signal
    handlers: its traceback runs through the code of a handler installed
    now.  Only an exception path pays for this: a walk of the traceback,
    and a look at the handlers only when it holds code not the agent's
    (a user expression's own error never does)."""
    global _signal_nums
    try:
        codes = set()
        tb = exc.__traceback__
        while tb is not None:
            code = tb.tb_frame.f_code
            if code.co_filename not in _OWN_FILES:
                codes.add(code)
            tb = tb.tb_next
        if not codes:
            return False
        # _signal, the C module, is loaded at start-up, and handlers are
        # installed through it; signal.getsignal() would turn each answer
        # into an enum, raising and catching an exception per function.
        sig = sys.modules.get("_signal")
        if sig is None:
            return False
        if _signal_nums is None:
            _signal_nums = tuple(sig.valid_signals())
        for num in _signal_nums:
            if _handler_code(sig.getsignal(num)) in codes:
                return True
    except Exception:
        pass
    return False


def _signalled(exc):
    """Whether an exception the program's code raised while the agent ran
    it (an expression, a ``__repr__``, a ``__str__``) must reach the
    program: a Ctrl-C (the default SIGINT handler is C code, with no code
    object to find), or anything a Python signal handler raised.  Any
    other exception, BaseException-only ones included (``sys.exit()``,
    ``CancelledError`` in a capture), is recorded as an error instead."""
    return isinstance(exc, KeyboardInterrupt) or _from_signal_handler(exc)


# --- options --------------------------------------------------------------
#
# A spec entry's capture and if expressions are compiled once (_Opts) and
# evaluated on entry with the frame's globals and locals; return values
# are recorded on PY_RETURN.  Values are reprlib-limited reprs, cut to
# repr_limit.  Their errors become "<error: Type: msg>" and are never
# raised, except a signal handler's (_signalled).
#
# The re-entrancy guard: _inside holds the idents of the threads running
# the program's code for a callback, and a callback that finds its thread
# there returns at once.  Python delivers no events while a callback runs
# (a capture's helper() is never seen), but sys.call_tracing() turns them
# back on.  Never DISABLE there: that would turn a traced function's
# PY_START off for good.  While no thread is inside, the check is one
# truthiness test.


class _Repr(reprlib.Repr):
    """reprlib's limits, but a failing ``__repr__`` raises (to be recorded
    as an error) instead of being shown as a made-up
    ``<Bad instance at 0x...>``."""

    def repr_instance(self, x, level):
        s = builtins.repr(x)
        if len(s) > self.maxother:  # cut in the middle, as reprlib does
            i = max(0, (self.maxother - 3) // 2)
            j = max(0, self.maxother - 3 - i)
            s = s[:i] + self.fillvalue + s[len(s) - j :]
        return s


def _make_repr(limit):
    r = _Repr()
    r.maxstring = r.maxother = r.maxlong = limit
    return r


def _error_text(exc):
    """``"<error: ValueError: bad>"``, cut to repr_limit (keeping the ">")."""
    return f"<error: {_exc_text(exc)}"[: _repr_limit - 1] + ">"


def _value(obj):
    """The recorded form of a value: its limited repr, or an error."""
    try:
        return _repr.repr(obj)[:_repr_limit]
    except BaseException as exc:
        if _signalled(exc):
            raise
        return _error_text(exc)


def _guarded(fn, arg):
    """``fn(arg)``, which runs the program's code, with this thread in
    _inside.  The add is inside the try: a signal handler's exception
    raised right after it must not leave the thread there for good."""
    ident = _get_ident()
    try:
        _inside.add(ident)
        return fn(arg)
    finally:
        _inside.discard(ident)


def _cap_drop():
    """A new call past the event cap: count it, and note the first."""
    global _n_dropped, _cap_noted
    _n_dropped += 1
    if not _cap_noted:
        _cap_noted = True
        msg = f"event cap of {_max_events:,} reached; later calls not recorded"
        _buf.append(_dumps({"k": "note", "t": _perf_ns(), "msg": msg}))


def _enter(opts, frame):
    """Apply an entry's options to a call starting in ``frame``: _SKIP if
    the call is not recorded, else its captured values (or None).

    In order: max_hits (past it no program code runs), the if, the event
    cap (so only calls the if lets through count as dropped), max_hits
    again, now taking the hit (the check and the count run with no call
    between, so threads cannot both take the last one), the captures."""
    max_hits = opts.max_hits
    if max_hits is not None and opts.hits >= max_hits:
        return _SKIP
    ident = _get_ident()
    names = None
    try:
        _inside.add(ident)
        if opts.cond is not None or opts.capture:
            env = frame.f_globals
            names = frame.f_locals  # 3.12: a snapshot; 3.13+: a proxy
        if opts.cond is not None:
            try:
                if not eval(opts.cond, env, names):
                    return _SKIP
            except BaseException as exc:
                if _signalled(exc):
                    raise
                return _SKIP
        if _n_events >= _max_events:
            _cap_drop()
            return _SKIP
        if max_hits is not None:
            if opts.hits >= max_hits:
                return _SKIP
            opts.hits += 1
        if not opts.capture:
            return None
        values = {}
        for text, code in opts.capture:
            if code.__class__ is str:  # it did not compile: the error
                values[text] = code
                continue
            try:
                values[text] = _value(eval(code, env, names))
            except BaseException as exc:
                if _signalled(exc):
                    raise
                values[text] = _error_text(exc)
        return values
    finally:
        try:
            if names.__class__ is dict:
                # Python 3.12: f_locals is a snapshot dict that the frame
                # keeps, holding every local's value at entry until the call
                # ends (a generator's whole life); that would delay the
                # program's finalizers.  Empty it: f_locals and locals()
                # fill it again when read.  (3.13+: a proxy, holding nothing.)
                names.clear()
        finally:
            _inside.discard(ident)


# --- monitoring callbacks -----------------------------------------------
#
# Each callback catches Exception, so a failure of the agent never reaches
# the program.  The program's own exceptions pass through on purpose:
# those raised by a signal handler (or PyThreadState_SetAsyncExc) that
# Python ran while the callback was executing.  BaseException-only ones
# (KeyboardInterrupt, SystemExit, ...) are never caught; an Exception
# from a Python signal handler is re-raised (_from_signal_handler).
# Swallowing them would lose a Ctrl-C, a SIGTERM -> sys.exit() handler or
# an alarm's timeout; letting them through raises them at the traced
# function's entry, return or unwind, as an untraced program would see
# them at some nearby instruction.  State is updated so that an
# interruption at any point leaves at worst an unfinished call.
#
# Callbacks never write and never take a lock.  A callback whose record
# reaches _HIGH_WATER calls _backpressure(), outside any traced call's
# time span: before taking the time of a call, after appending a return.
#
# The event cap: once _n_events reaches _max_events, no new call is
# recorded (_cap_drop); the ends and suspensions of recorded calls still
# are, since every other callback only acts on calls in _live.


def _on_start(code, offset):
    global _n_starts, _n_events
    try:
        if _stopped:
            return None
        if _inside and _get_ident() in _inside:
            return None  # nested (see "options"); never DISABLE here
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
        frame = _getframe(1)
        opts = fn.opts
        values = None
        if opts is not None:
            values = _enter(opts, frame)
            if values is _SKIP:
                return None
        elif _n_events >= _max_events:
            _cap_drop()
            return None
        if len(_buf) >= _HIGH_WATER:
            _backpressure()
        tid = _thread_tid()
        task = _task_id(tid)
        if fn.id not in _fn_written:
            _fn_written.add(fn.id)
            _buf.append(fn.record)
        c = next(_call_ids)
        t = _perf_ns()
        if values is None:
            _buf.append(_CALL % (t, c, fn.id, tid, "null" if task is None else task))
        else:
            record = {"k": "call", "t": t, "c": c, "fn": fn.id, "tid": tid}
            record["task"], record["v"] = task, values
            _buf.append(_dumps(record))
        _n_events += 1
        _live[id(frame)] = (c, code, fn)
    except Exception as exc:
        if _from_signal_handler(exc):
            raise
    return None


def _on_return(code, offset, retval):
    global _n_events
    try:
        if _stopped:
            return None
        if _inside and _get_ident() in _inside:
            return None
        t = _perf_ns()
        frame_id = id(_getframe(1))
        live = _live.get(frame_id)
        if live is None:
            return None
        if live[1] is not code:  # stale: the id of a frame that is gone
            del _live[frame_id]
            return None
        # The call stays live until its record is made: a signal handler's
        # exception raised before then propagates from here, the frame
        # unwinds with it (Python fires PY_UNWIND after a PY_RETURN
        # callback raises), and _on_unwind ends the call with it.
        tid = _thread_tid()
        task = _task_id(tid)
        if live[2].ret:
            record = {"k": "ret", "t": t, "c": live[0], "tid": tid, "task": task}
            record["v"] = _guarded(_value, retval)
            record = _dumps(record)
        else:
            record = _RET % (t, live[0], tid, "null" if task is None else task)
        # No signal handler can run from here to the end of the append (no
        # call before it): the call ends exactly once.
        _n_events += 1
        del _live[frame_id]
        _buf.append(record)
        if len(_buf) >= _HIGH_WATER:
            _backpressure()
    except Exception as exc:
        if _from_signal_handler(exc):
            raise
        _live.pop(id(_getframe(1)), None)  # it returns: never leave it live
    return None


def _on_yield(code, offset, retval):
    # Ends a run: a yield, or an await that suspends (in an await chain
    # each frame yields, innermost first).
    global _n_events
    try:
        if _stopped:
            return None
        if _inside and _get_ident() in _inside:
            return None
        t = _perf_ns()
        live = _live.get(id(_getframe(1)))
        if live is None or live[1] is not code:
            return None
        tid = _thread_tid()
        task = _task_id(tid)
        _buf.append(_SUSP % (t, live[0], tid, "null" if task is None else task))
        _n_events += 1
        if len(_buf) >= _HIGH_WATER:
            _backpressure()
    except Exception as exc:
        if _from_signal_handler(exc):
            raise
    return None


def _resumed(code, frame_id):
    """Record ``res`` if the frame ``frame_id``, running ``code``, is a live
    traced call (PY_RESUME, and PY_THROW: ``throw()``, a task's
    cancellation, ``close()``)."""
    global _n_events
    live = _live.get(frame_id)
    if live is None or live[1] is not code:
        return
    if len(_buf) >= _HIGH_WATER:
        _backpressure()
    tid = _thread_tid()
    task = _task_id(tid)
    t = _perf_ns()
    _buf.append(_RES % (t, live[0], tid, "null" if task is None else task))
    _n_events += 1


def _on_resume(code, offset):
    try:
        if _stopped or (_inside and _get_ident() in _inside):
            return None
        _resumed(code, id(_getframe(1)))
    except Exception as exc:
        if _from_signal_handler(exc):
            raise
    return None


def _on_throw(code, offset, exc):
    # Global event: must stay cheap for untraced code; never DISABLE.
    try:
        if _stopped:
            return None
        hit = _armed.get(id(code))
        if hit is None or hit[0] is not code:
            return None
        if _inside and _get_ident() in _inside:
            return None
        _resumed(code, id(_getframe(1)))
    except Exception as err:
        if _from_signal_handler(err):
            raise
    return None


def _on_unwind(code, offset, exc):
    # Global event: must stay cheap for untraced code; never DISABLE.
    global _n_events
    try:
        if _stopped:
            return None
        hit = _armed.get(id(code))
        if hit is None or hit[0] is not code:
            return None
        if _inside and _get_ident() in _inside:
            return None
        t = _perf_ns()
        # The frame is gone after this; drop its call first, so it cannot
        # leak even if what follows fails.
        live = _live.pop(id(_getframe(1)), None)
        if live is None or live[1] is not code:
            return None
        text = _guarded(_exc_text, exc)  # str() is the program's code
        tid = _thread_tid()
        task = _task_id(tid)
        _buf.append(
            _dumps(
                {
                    "k": "raise",
                    "t": t,
                    "c": live[0],
                    "tid": tid,
                    "task": task,
                    "exc": text,
                }
            )
        )
        _n_events += 1
        if len(_buf) >= _HIGH_WATER:
            _backpressure()
    except Exception as err:
        if _from_signal_handler(err):
            raise
    return None


_CALLBACKS = (
    (_EV.PY_START, _on_start),
    (_EV.PY_RETURN, _on_return),
    (_EV.PY_YIELD, _on_yield),
    (_EV.PY_RESUME, _on_resume),
    (_EV.PY_THROW, _on_throw),
    (_EV.PY_UNWIND, _on_unwind),
)


# --- discovery ----------------------------------------------------------------
#
# Discover mode counts the calls of every function in user code, for tdb
# to write a spec from.  Only PY_START is on, globally, with _on_count as
# its callback.  User code is a function body (module and class bodies
# are not functions, nor is 3.14's compiler-made __annotate__) whose
# qualname, without ".<locals>", has no "<" (no lambda, no generator
# expression), from a file that is not "<...>" (exec and frozen code), not
# the agent's and not library code (_user_file): under a library directory
# (the standard library and site-packages, _library_dirs), unless it is
# under one of the user roots tdb names (the program's file and directory)
# that is deeper still.
#
# Overhead stays bounded: other code returns DISABLE at its first call, and
# a function returns DISABLE once it has been counted _discover_cap times
# (it is then "capped").  Counts are kept per (file, qualname, first line), so
# several code objects of one function (a module imported twice) count
# together.  A counting callback only adds to its _Count and to _changed;
# the flusher appends a cumulative "count" record for each _Count in
# _changed at each flush, and final_flush one for each _Count not written
# since it last changed.  Counts are per part: a forked child counts from
# zero (_new_part), so tdb sums the parts.  Code a parent DISABLEd stays so
# in its forked children: their parts have no counts for it.


class _Count:
    """One function's count in this part.  ``flushed`` is the count of the
    last record the flusher made for it."""

    __slots__ = ("head", "n", "capped", "flushed")

    def __init__(self, file, qual, line, kind):
        self.n = self.flushed = 0
        self.capped = False
        # The record's fixed fields, as _dumps gives them, without the "}".
        self.head = _dumps(
            {"k": "count", "file": file, "qual": qual, "line": line, "kind": kind}
        )[:-1]

    def record(self, n):
        capped = "true" if self.capped else "false"
        return '%s,"n":%d,"capped":%s}' % (self.head, n, capped)


def _library_dirs():
    """The directories of library code, normalized and ending in os.sep:
    sysconfig's stdlib, platstdlib, purelib and platlib, and site's
    site-packages directories, which can be elsewhere (Debian's and
    Ubuntu's Pythons keep packages installed with apt in
    /usr/lib/python3/dist-packages, which sysconfig does not name).
    Editable installs keep their sources outside: they are user code."""
    found = []
    try:
        import sysconfig  # lazily: only discover mode needs it

        paths = sysconfig.get_paths()
        found.extend(
            paths.get(k) for k in ("stdlib", "platstdlib", "purelib", "platlib")
        )
    except Exception:
        pass
    site = sys.modules.get("site")  # imported already: it imported this
    for name in ("getsitepackages", "getusersitepackages"):
        try:
            dirs = getattr(site, name)()
            dirs = [dirs] if isinstance(dirs, str) else dirs
            # Only directories of installed packages: on Windows site also
            # names the bare prefix (in a venv, the venv's own directory).
            found.extend([d for d in dirs if _basename(d) in _PACKAGE_DIRS])
        except Exception:  # an old virtualenv's site.py lacks getsitepackages
            pass
    out = set()
    for path in found:
        if path:
            out.add(_as_dir(_norm_path(path)))
    return tuple(sorted(out))


_PACKAGE_DIRS = ("site-packages", "dist-packages")


def _basename(path):
    return os.path.basename(os.path.normcase(path).rstrip("/\\"))


def _as_dir(path):
    """A normalized directory, ending in os.sep."""
    return path if path.endswith(os.sep) else path + os.sep


def _deepest(nfile, dirs):
    """The length of the longest of ``dirs`` (ending in os.sep) holding
    ``nfile``, or 0."""
    best = 0
    for d in dirs:
        if len(d) > best and nfile.startswith(d):
            best = len(d)
    return best


def _user_file(filename):
    """The normalized path of a file of user code, or None.

    Library code is code under a library directory, unless a user root
    holds it more closely: the program's own file, or its directory when
    that is deeper than the library directory (a program inside an
    installed package), so the program counts as user code wherever it
    is installed.  A library directory inside a user root (a project's
    .venv) is still library code, and so, at equal depth, are a program's
    neighbours in site-packages itself."""
    nfile = _norm_path(filename)
    if os.path.dirname(nfile) in _agent_dirs:
        return None
    lib = _deepest(nfile, _lib_dirs)
    if not lib:
        return nfile
    root = len(nfile) + 1 if nfile in _root_paths else _deepest(nfile, _root_dirs)
    return nfile if root > lib else None


def _new_count(code):
    """The _Count for ``code``, seen for the first time, or None if it is
    not user code."""
    if not code.co_flags & _CO_OPTIMIZED:  # a module or class body, exec code
        return None
    if code.co_name == "__annotate__":  # 3.14: made for annotations, no def
        return None
    filename = code.co_filename
    if filename.startswith("<"):
        return None
    qual = code.co_qualname.replace(".<locals>", "")
    if "<" in qual:  # <lambda>, <genexpr>, ...
        return None
    nfile = _user_files.get(filename, False)
    if nfile is False:
        nfile = _user_files[filename] = _user_file(filename)
    if nfile is None:
        return None
    key = (nfile, qual, code.co_firstlineno)
    count = _count_keys.get(key)
    if count is None:
        kind = _kind(code.co_flags)
        count = _count_keys[key] = _Count(nfile, qual, code.co_firstlineno, kind)
    _counts[id(code)] = (code, count)
    return count


def _on_count(code, offset):
    """PY_START in discover mode: count a call of user code."""
    global _n_starts
    try:
        if _stopped:
            return None
        if _inside and _get_ident() in _inside:
            return None  # nested (see "options"); never DISABLE here
        _n_starts += 1
        hit = _counts.get(id(code))
        if hit is not None and hit[0] is code:
            count = hit[1]
        else:
            count = _new_count(code)
            if count is None:
                return DISABLE  # not user code: never seen again
        if count.capped:  # another code object of a capped function
            return DISABLE
        count.n += 1
        _changed.add(count)
        if count.n >= _discover_cap:
            count.capped = True
            return DISABLE
    except Exception as exc:
        if _from_signal_handler(exc):
            raise
    return None


def _flush_counts():
    """The flusher's: append a record for each count changed since the
    last flush.  Each leaves _changed before its count is read, so a call
    counted meanwhile puts it back for the next flush."""
    for count in list(_changed):
        _changed.discard(count)
        n = count.n
        _buf.append(count.record(n))
        count.flushed = n


def _final_counts():
    """final_flush's: append a record for each count not written since it
    last changed (a flusher stopped half-way through _flush_counts may have
    taken some out of _changed).  Safe to repeat: tdb takes a part's
    largest count."""
    _buf.extend([c.record(c.n) for c in list(_count_keys.values()) if c.n != c.flushed])


# --- crashes ----------------------------------------------------------------
#
# install_crash_hooks() wraps sys.excepthook and threading.excepthook.
# sitecustomize.py calls it after importing the next sitecustomize, so a
# distro hook installed there that never calls the one before it
# (Debian/Ubuntu's apport) becomes ours to call, not a replacement.  Each
# wrapper records the crash, then calls the hook that was there before, so
# the program's output and exit status stay as they were.  Recording
# catches Exception only (as the callbacks do): a BaseException from a
# signal handler propagates, and Python then reports it and the original
# exception itself.  A program that installs its own hook later without
# calling the one before replaces the wrapper; its crashes go unrecorded.

_hooks_installed = False


def _where(tb):
    """``"/abs/file.py:41"`` of the innermost traceback frame, or None."""
    if tb is None:
        return None
    while tb.tb_next is not None:
        tb = tb.tb_next
    filename = tb.tb_frame.f_code.co_filename
    if not filename.startswith("<"):  # <string>, <stdin>: not a path
        filename = _norm_path(filename)
    return f"{filename}:{tb.tb_lineno}"


def _bare_traceback(exc, tb):
    """A traceback's lines, without source lines, built without importing
    anything or running code from a string: for KeyboardInterrupt.

    After an uncaught KeyboardInterrupt Python dies by SIGINT (shells stop
    a script's loop on that).  Python 3.12 drops that and exits 1 once any
    code runs from a string (exec, eval), and a first import of traceback
    does that: its dependencies build namedtuples."""
    lines = []
    if tb is not None:
        lines.append("Traceback (most recent call last):")
    while tb is not None:
        code = tb.tb_frame.f_code
        lines.append(
            f'  File "{code.co_filename}", line {tb.tb_lineno}, in {code.co_name}'
        )
        tb = tb.tb_next
    lines.append(_exc_text(exc))
    return lines


def _tb_text(exc_type, exc, tb):
    """The traceback as printed, at most _TB_LINES lines: its first line,
    then (if cut) a marker and the last lines, where the frames nearest the
    error and the exception itself are."""
    if issubclass(exc_type, KeyboardInterrupt):
        lines = _bare_traceback(exc, tb)
    else:
        import traceback  # lazily: most programs never crash

        lines = "".join(traceback.format_exception(exc_type, exc, tb)).splitlines()
    if len(lines) > _TB_LINES:
        keep = _TB_LINES - 2
        cut = len(lines) - 1 - keep
        lines = [lines[0], f"  [... {cut} lines omitted ...]", *lines[-keep:]]
    return "\n".join(lines)


def _record_crash(exc_type, exc, tb):
    # SystemExit is a way out, not a crash (threads exit with it silently).
    if _stopped or exc_type is None or issubclass(exc_type, SystemExit):
        return
    t = _perf_ns()
    record = {
        "k": "crash",
        "t": t,
        "tid": _thread_tid(),
        "exc": _exc_text(exc),
        "where": _where(tb),
        "tb": _tb_text(exc_type, exc, tb),
    }
    _buf.append(_dumps(record))


def _excepthook(exc_type, exc, tb):
    """``sys.excepthook`` while tracing: an uncaught exception."""
    try:
        _record_crash(exc_type, exc, tb)
    except Exception:
        pass
    return _prev_excepthook(exc_type, exc, tb)


def _thread_excepthook(args):
    """``threading.excepthook`` while tracing; runs in the thread that died."""
    try:
        _record_crash(args.exc_type, args.exc_value, args.exc_traceback)
    except Exception:
        pass
    return _prev_thread_excepthook(args)


def install_crash_hooks():
    """Wrap the current ``sys.excepthook`` and ``threading.excepthook``.

    Called by sitecustomize.py once the next sitecustomize has run.  Once
    per process, and only while tracing (discovery records no crashes)."""
    global _prev_excepthook, _prev_thread_excepthook, _hooks_installed
    if _hooks_installed or _tool is None or _mode == "discover":
        return
    _hooks_installed = True
    _prev_excepthook = sys.excepthook
    _prev_thread_excepthook = threading.excepthook
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook


# --- writing --------------------------------------------------------------
#
# One writer at a time, and no locks:
# - start() writes the header before the flusher exists;
# - during the run only the flusher writes.  Recording threads only append
#   to _buf, and the flusher only deletes from its front, after writing
#   those records (list slicing, append and del are each atomic under the
#   GIL), so no record is ever outside _buf until it is on disk;
# - final_flush fences the flusher (it checks _fence just before each
#   os.write), waits a bounded time for it to stop, and then writes the
#   rest itself.  If the flusher is stuck (in a finalizer it ran, say), the
#   in-flight bookkeeping and the part's size tell final_flush exactly
#   which bytes of the chunk are on disk, so nothing is written twice.
# The main thread, where signal handlers run, never writes during the run,
# so an exception from a handler can never interrupt a write half-way.


def _open_part(parts_dir):
    """Create ``<pid>.jsonl`` (or ``<pid>-N.jsonl``) exclusively; return (fd, path)."""
    pid = _getpid()
    flags = (
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND | getattr(os, "O_BINARY", 0)
    )
    for n in range(1, 1000):
        path = _path_join(parts_dir, f"{pid}.jsonl" if n == 1 else f"{pid}-{n}.jsonl")
        try:
            return _os_open(path, flags, 0o666), path
        except FileExistsError:
            continue
    raise FileExistsError(f"no free part file name for pid {pid} in {parts_dir}")


def _header():
    """The ``hdr`` record of a new part."""
    wall = _time_ns()
    pc = _perf_ns()
    return _dumps(
        {
            "k": "hdr",
            "pid": _getpid(),
            "ppid": _getppid(),
            "proc": _proc_name,
            "argv": list(sys.argv),
            "py": "%d.%d.%d" % tuple(sys.version_info[:3]),
            "wall": wall,
            "pc": pc,
            "mode": _mode,
        }
    )


def _part_size():
    """The part file's size; an OSError if _fd is no longer the part file."""
    st = _fstat(_fd)
    if (st.st_dev, st.st_ino) != _part_id:
        # The program closed our descriptor (as daemonizing code does) and
        # the number may now be one of its own files: never write there.
        raise OSError(errno.EBADF, f"descriptor {_fd} no longer refers to {_part_path}")
    return st.st_size


def _write_backlog(n):
    """Write the ``n`` oldest records, a chunk at a time, and drop them from
    _buf.  Only one writer may run this.  Returns False if fenced."""
    global _inflight, _in_syscall, _flushed
    while n > 0:
        batch = _buf[: min(n, _CHUNK)]
        if not batch:
            break
        data = ("\n".join(batch) + "\n").encode("ascii")
        view = memoryview(data)
        _inflight = (batch[0], _part_size(), data, len(batch))
        # No call, and so no chance for other code to run, between each
        # fence check and its os.write.
        if _fence:
            return False
        _in_syscall = True
        written = _os_write(_fd, view)
        _in_syscall = False
        while written < len(data):  # a short write: rare
            if _fence:
                return False
            _in_syscall = True
            written += _os_write(_fd, view[written:])
            _in_syscall = False
        del _buf[: len(batch)]
        _inflight = None
        _flushed += len(batch)
        n -= len(batch)
    return True


def _write_out(data):
    """final_flush's own writes, once no flusher can write.  With a single
    writer the part's size says exactly what is on disk, so an exception
    from a signal handler (a Ctrl-C as the program exits, say), whether it
    lands before or after an os.write, never stops the write half-way and
    never repeats a byte: it carries on from the size."""
    if not data:
        return
    start = _part_size()
    for _ in range(3):
        try:
            while True:
                done = _part_size() - start
                if done >= len(data):
                    return
                _os_write(_fd, memoryview(data)[done:])
        except OSError as exc:
            if exc.errno is not None:  # a real I/O error
                raise
        except BaseException:
            pass


def _shielded(fn):
    """Call fn, again (up to three tries) if an exception from a signal
    handler interrupts it; fn must be safe to repeat.  I/O errors raise."""
    for attempt in range(3):
        try:
            return fn()
        except OSError as exc:
            if exc.errno is not None or attempt == 2:
                raise
        except BaseException:
            if attempt == 2:
                raise


def _give_up(error):
    """Stop tracing for good after a write failure, instead of retrying it
    on every flush while the buffer grows; leave an agent-error marker."""
    global _stopped, _broken, _fence
    _stopped = _broken = _fence = True
    del _buf[:]
    try:
        pid = _getpid()
        record = {
            "marker": "agent-error",
            "pid": pid,
            "argv": list(sys.argv),
            "error": error,
        }
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        for n in range(1, 100):
            name = f"agent-error-{pid}" + (f"-{n}" if n > 1 else "") + ".json"
            try:
                fd = _os_open(_path_join(_parts_dir, name), flags, 0o666)
            except FileExistsError:
                continue
            try:
                _os_write(fd, _dumps(record).encode("ascii"))
            finally:
                _os_close(fd)
            return
    except Exception:
        pass


def _wake_flusher():
    try:
        _wake.release()
    except RuntimeError:  # not held: the flusher is awake already
        pass


def _backpressure():
    """Give the flusher a short, lock-free wait to catch up.

    A flusher that makes no progress meanwhile (stuck in a finalizer that
    waits for a lock this thread holds, say) is not waited for again until
    it moves: until then the backlog grows, and a note says so.
    """
    global _stall_mark, _stall_noted
    if _get_ident() == _flusher_ident:
        # The flusher itself, running a finalizer that recorded an end: it
        # would wait for itself.
        return
    mark = _flushed
    if mark == _stall_mark:
        return
    _wake_flusher()
    end = _perf_ns() + int(_PRESSURE_WAIT * 1e9)
    while len(_buf) >= _LOW_WATER and not _stopped:
        if _perf_ns() >= end:
            if _flushed == mark:
                _stall_mark = mark
                if not _stall_noted:
                    _stall_noted = True
                    msg = (
                        "the trace writer fell behind (blocked for at least "
                        f"{_PRESSURE_WAIT * 1000:.0f} ms); events are buffered "
                        "in memory without a limit until it catches up"
                    )
                    _buf.append(_dumps({"k": "note", "t": _perf_ns(), "msg": msg}))
            return
        _sleep(0.001)


def _flusher_loop(started, exited, token):
    global _flusher_ident, _flusher_tid
    me, tid = _get_ident(), _get_native_id()
    if _flusher_ident is not token:
        return  # its starter gave up on it (see _start_flusher)
    _flusher_ident, _flusher_tid = me, tid  # no call since the test
    try:
        started.release()
        while not _fence:
            _wake.acquire(timeout=_interval)  # the sleep; a release wakes it
            if _fence:
                break
            halt = bool(_halt)  # a fork asked: write what is buffered, then end
            try:
                _check_proc_name()
                if _changed:  # discover mode
                    _flush_counts()
                _write_backlog(len(_buf))
            except OSError as exc:
                if exc.errno is not None:  # a real I/O error
                    _give_up(f"cannot write the part file: {exc}")
            except BaseException:
                pass  # the records stay in _buf for the next flush
            if halt and _halt:
                # Take the request and say there is no flusher, with no call
                # in between; it writes nothing more.  (An empty _halt: the
                # fork took its request back and went ahead.)
                del _halt[0]
                _flusher_ident = None
                break
    finally:
        if _flusher_ident is me:  # ended otherwise: final_flush, a write error
            _flusher_ident = None
        exited.release()


def _start_flusher():
    """Start a flusher, unless one runs or is starting, and wait, a short
    bound, until it runs (so its own first code is seen during start-up,
    never at a time that depends on scheduling).  Bounded: started after a
    fork, the thread that forked may hold a lock that code the new thread
    runs (a finalizer) waits for.

    Never two flushers: the starter takes the empty slot (_flusher_ident)
    for a token, and the thread replaces the token with its ident.  Both
    test and set with no call in between, and so does a starter that is
    interrupted (a signal handler's exception) and gives the slot back
    while it still holds its token: a thread it did start then ends at once.
    """
    global _flusher_ident, _flusher_exited
    token = []  # a new object, made without a call
    if _flusher_ident is not None:
        return
    _flusher_ident = token
    exited = None
    try:
        del _halt[:]  # left by an interrupted fork: for no flusher now
        if not _wake.locked():
            _wake.acquire()
        exited = _allocate_lock()
        exited.acquire()
        started = _allocate_lock()
        started.acquire()
        _flusher_exited = exited
        # A bare _thread thread: threading.enumerate() and active_count() in
        # the program stay as they would be without the tracer.
        _start_new_thread(_flusher_loop, (started, exited, token))
        started.acquire(timeout=_START_WAIT)
    except BaseException:
        if _flusher_ident is token:  # no thread took the slot; none will
            _flusher_ident = None
            if _flusher_exited is exited:
                exited.release()  # none to wait for
        raise


def _take_over():
    """After the flusher stopped, or was fenced and not waited for longer:
    return (the bytes of its in-flight chunk not yet on disk, the records
    still to write after them)."""
    end = _perf_ns() + int(_landing_wait * 1e9)
    while True:
        inflight = _inflight
        snapshot = _buf[:]  # atomic; a fenced flusher can still drop a chunk
        if inflight is None:
            return b"", snapshot
        first, before, data, count = inflight
        if _in_syscall and _part_size() < before + len(data) and _perf_ns() < end:
            # It called os.write and has not come back (or is stuck in code
            # it ran right after): give that write a bounded time to land.
            _sleep(0.01)
            continue
        if not snapshot or snapshot[0] is not first:  # written and dropped
            return b"", snapshot
        done = min(max(_part_size() - before, 0), len(data))
        return data[done:], snapshot[count:]


_real_os_exit = os._exit


def _exit(status):
    """``os._exit`` while tracing: write the trace's last records first.
    The real os._exit runs whatever happens meanwhile, such as an exception
    from a signal handler raised as final_flush returns."""
    try:
        final_flush("_exit")
    finally:
        _real_os_exit(status)


# --- fork -----------------------------------------------------------------
#
# A forked child has one thread (the one that forked), a copy of the
# parent's memory and the parent's descriptors.  Python runs these hooks
# for os.fork() and os.forkpty(), and for a subprocess with a preexec_fn:
# - before the fork, the flusher writes what is buffered and ends, and the
#   forking thread waits (a short bound) until its thread is gone.  So the
#   process forks with no thread of the tracer's: none can hold a lock the
#   child would inherit held, and Python 3.12+ does not warn about forking
#   a multi-threaded process.  A flusher that does not end in time (stuck
#   in a finalizer that waits for a lock the forking thread holds, say) is
#   left to carry on, and the fork goes ahead (and, until the flusher
#   moves again, later forks do not wait for it);
# - in the parent, a flusher is started if none runs (_ensure_flusher);
# - in the child, a hook in C (_CHILD_GATE) first stops recording and all
#   writing; then everything kept per process starts afresh: new locks, a
#   part file of its own, published once its header is written, call and
#   task ids from 1, the fn/th/task records written again, the event cap
#   and max_hits counted from zero, shorter exit waits, and a new flusher
#   (_ensure_flusher again after it).
#   The parent's unwritten records and live calls are the parent's to
#   write: they are dropped, and the parent's part descriptor is closed (a
#   raw one, so nothing is flushed through it).
#
# A signal handler's exception can be raised at almost any point of these
# hooks, and cannot reach the program (Python prints and drops it, as for
# any at-fork hook).  So every step leaves the state consistent if it is
# interrupted, and the hooks re-raise only such exceptions:
# - _flusher_ident says whether there is a flusher; it changes only in
#   test-and-set steps with no call inside (see _start_flusher, and the
#   flusher taking a request), so it is never wrong;
# - a request is taken back in a finally, by a step with no call inside;
# - _ensure_flusher starts a flusher whenever there is none (once more if
#   interrupted inside), and is registered twice, one after the other: a
#   signal that arrived during the fork() call is handled at the first
#   Python code after it, the first one's very start, before any try; the
#   second then starts the flusher;
# - until the child's hook has finished, the gate keeps the child from
#   recording and from writing anything to the parent's part; and the
#   child's part is published only with its header in it.  (final_flush
#   also writes only in the process its part is for, whatever hook a signal
#   handler calls os._exit from.)


def _wait_gone(tid):
    """Wait, at most _GONE_WAIT, until thread ``tid`` has left the process.

    Python 3.12+ counts a Linux process's threads in /proc/self/stat to
    decide whether os.fork() warns, and an ended thread is still counted
    for some microseconds after its last Python code ran.  Linux only: on
    macOS Python counts Mach threads, which there is no cheap way to wait
    for here, so a fork there may still show the warning (a documented
    limitation)."""
    if _TASKS is None or tid is None:
        return
    path = _TASKS + str(tid)
    end = _perf_ns() + int(_GONE_WAIT * 1e9)
    while True:
        try:
            _os_stat(path)
        except OSError:
            return  # gone
        if _perf_ns() >= end:
            return
        _sleep(0.00005)


_TASKS = "/proc/self/task/" if sys.platform.startswith("linux") else None


def _before_fork():
    """os.fork() is about to run: ask the flusher to write what is buffered
    and end, and wait a short bound (see "fork")."""
    global _fork_stall
    if _fence or _flusher_ident is None or _flushed == _fork_stall:
        return  # none to stop, or one that did not stop last time, unmoved since
    exited, tid = _flusher_exited, _flusher_tid
    try:
        _halt.append(True)
        _wake_flusher()
        if exited.acquire(timeout=_FORK_STOP_WAIT):
            exited.release()  # for anyone else waiting (another fork, final_flush)
    except Exception as exc:
        if _from_signal_handler(exc):
            raise
    finally:
        if _halt:  # not taken: take it back, so it carries on through the fork
            del _halt[0]
    if _flusher_ident is None:
        _fork_stall = -1
        _wait_gone(tid)
    else:
        _fork_stall = _flushed


def _restart_flusher():
    """_start_flusher, from an at-fork hook: once more if interrupted.  A
    signal handler's exception is then raised again (Python drops it); an
    error of the tracer's own (no new thread) is left for the next fork or
    final_flush, which writes everything."""
    try:
        _start_flusher()
    except BaseException as exc:
        try:
            _start_flusher()
        except BaseException:
            pass
        if _signalled(exc):
            raise


def _ensure_flusher():
    """After a fork, in the parent (twice) and in the child (after its
    set-up): start a flusher if tracing goes on and none runs, because the
    fork stopped it or an interrupted hook left none (see "fork")."""
    if not _fence and _flusher_ident is None:
        _restart_flusher()


def _close_if_ours(fd):
    """Close the parent's part descriptor, unless the program has closed it
    and the number is now one of its own files."""
    try:
        st = _fstat(fd)
        if (st.st_dev, st.st_ino) == _part_id:
            _os_close(fd)
    except OSError:
        pass


def _forked_to_exec():
    """Whether the fork _after_fork_in_child runs for is subprocess's, to run
    a preexec_fn: that child then executes the new program, which, if it is
    a traced Python program, starts its own part."""
    popen = getattr(sys.modules.get("subprocess"), "Popen", None)
    code = getattr(getattr(popen, "_execute_child", None), "__code__", None)
    if code is None:
        return False
    try:
        return _getframe(2).f_code is code  # the code that forked
    except ValueError:  # no Python code running: a fork from C
        return False


def _new_part():
    """In a forked child (its only thread): per-part state afresh, then a
    part file of its own, published only once its header is in it."""
    global _call_ids, _task_ids, _task_counter, _n_events, _n_dropped, _cap_noted
    global _final_done, _broken, _fence, _proc_name, _fd, _part_path, _part_id
    global _part_pid
    del _buf[:]
    _live.clear()
    _fn_written.clear()
    _thread_names.clear()
    _task_ids = weakref.WeakKeyDictionary()
    _call_ids, _task_counter = itertools.count(1), itertools.count(1)
    _n_events = _n_dropped = 0
    _cap_noted = False
    for t in _targets:
        for item in t.functions:
            if item[2] is not None:
                item[2].hits = 0
    # The parent's counts are the parent's to write (see "discovery").
    _counts.clear()
    _count_keys.clear()
    _changed.clear()
    _proc_name = _current_proc_name() or "MainProcess"
    data = (_header() + "\n").encode("ascii")
    fd, path = _open_part(_parts_dir)
    try:
        view, done = memoryview(data), 0
        while done < len(data):
            done += _os_write(fd, view[done:])
        st, pid = _fstat(fd), _getpid()
    except BaseException:
        try:  # no part file without its header
            _os_close(fd)
            _unlink(path)
        except OSError:
            pass
        raise
    # Published with no call in between, so all together or not at all.
    _fd, _part_path, _part_id, _part_pid = fd, path, (st.st_dev, st.st_ino), pid
    _final_done = _broken = _fence = False


def _after_fork_in_child():
    """os.fork() returned in the child: give it a part of its own (see
    "fork").  _CHILD_GATE has run: nothing records or writes until this
    has finished."""
    global _wake, _flusher_exited, _halt, _fork_stall, _flusher_ident
    global _flusher_tid, _inflight, _in_syscall, _flushed, _stall_mark
    global _stall_noted, _fd, _stopped, _stop_wait, _landing_wait
    inherited, _fd = _fd, None
    # New locks and flusher controls, never the parent's: a parent thread
    # that is not here may have held one at the fork.  No flusher yet, so
    # final_flush has none to wait for.
    _wake, _flusher_exited = _allocate_lock(), _allocate_lock()
    _halt, _fork_stall = [], -1
    _flusher_ident = _flusher_tid = _inflight = None
    _in_syscall, _flushed, _stall_mark, _stall_noted = False, 0, -1, False
    _stop_wait, _landing_wait = _CHILD_STOP_WAIT, _CHILD_LANDING_WAIT
    # Threads inside a callback in the parent do not exist here, and a new
    # thread may get one's ident.
    _inside.clear()
    if inherited is None or _tool is None:
        return  # not tracing
    try:
        _close_if_ours(inherited)
        if _forked_to_exec():
            return
        _new_part()
    except Exception as exc:
        if _from_signal_handler(exc):
            raise
        _give_up(f"cannot start the part file of a forked child: {exc}")
        return
    _stopped = False
    _restart_flusher()


# In C, so no Python code runs inside it and no signal handler can stop it
# half-way: the tracer's first hook in the child (registered before
# _after_fork_in_child), it keeps the child from recording, from writing
# (final_flush returns at once; no flusher runs) and from starting a
# flusher, until _after_fork_in_child has set the child up.
_CHILD_GATE = _partial(
    globals().update, {"_stopped": True, "_final_done": True, "_fence": True}
)


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
    global _tool, _fd, _part_path, _part_id, _parts_dir, _stopped, _interval
    global _targets, _agent_dirs, _proc_name, _repr_limit, _repr, _max_events
    global _mode, _part_pid, _discover_cap, _lib_dirs, _root_paths, _root_dirs
    if _tool is not None or _fd is not None:
        raise RuntimeError("the tracer is already started in this process")
    if config.get("version") != 1:
        raise ValueError(f"unsupported trace config version {config.get('version')!r}")
    mode = config["mode"]
    if mode not in ("trace", "discover"):
        raise ValueError(f"unsupported trace mode {mode!r}")
    discover = mode == "discover"
    parts_dir = config["parts"]
    _repr_limit = int(config["repr_limit"])  # first: it cuts compile errors
    max_events = int(config["max_events"])
    targets = tuple(_Target(t) for t in config["targets"])
    interval = float(config["flush_interval"])
    agent_dirs = {_norm_path(os.path.dirname(os.path.abspath(__file__)))}
    if config.get("agent_dir"):
        agent_dirs.add(_norm_path(config["agent_dir"]))
    if discover:
        cap = int(config["discover_cap"])
        lib_dirs = _library_dirs()  # before any callback: it imports sysconfig
        roots = frozenset(_norm_path(r) for r in config["user_roots"])

    tool = pick_tool_id()
    if tool is None:
        raise RuntimeError("sys.monitoring tool ids 3 and 4 are both in use")
    _mon.use_tool_id(tool, TOOL_NAME)
    _tool = tool
    try:
        _targets, _interval, _agent_dirs = targets, interval, frozenset(agent_dirs)
        _repr, _max_events, _mode = _make_repr(_repr_limit), max_events, mode
        if discover:
            _discover_cap, _lib_dirs = cap, lib_dirs
            _root_paths, _root_dirs = roots, tuple(_as_dir(r) for r in roots)
        _fd, _part_path = _open_part(parts_dir)
        st = _fstat(_fd)
        _part_id, _parts_dir, _part_pid = (st.st_dev, st.st_ino), parts_dir, _getpid()
        _proc_name = _current_proc_name() or "MainProcess"
        _buf.append(_header())
        if discover:  # PY_START only (see "discovery")
            _mon.register_callback(tool, _EV.PY_START, _on_count)
        else:
            for event, callback in _CALLBACKS:
                _mon.register_callback(tool, event, callback)
        _stopped = False
        _mon.set_events(tool, _EV.PY_START if discover else _GLOBAL_EVENTS)
        # Write the header now, before the flusher exists, through the code
        # the flusher runs, so its wake-ups run no code start-up has not.
        _check_proc_name()
        _write_backlog(len(_buf))
        _start_flusher()
        atexit.register(final_flush, "atexit")
        if hasattr(os, "register_at_fork"):
            # In registration order after a fork: _ensure_flusher twice in
            # the parent; the gate, the set-up, _ensure_flusher in the child.
            os.register_at_fork(
                before=_before_fork,
                after_in_parent=_ensure_flusher,
                after_in_child=_CHILD_GATE,
            )
            os.register_at_fork(
                after_in_parent=_ensure_flusher, after_in_child=_after_fork_in_child
            )
            os.register_at_fork(after_in_child=_ensure_flusher)
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
    """Write the rest of the buffer and an ``exit`` record, then stop.

    ``how`` is "atexit" or "_exit".  Idempotent, and never raises.  Waits
    at most _stop_wait (+ _landing_wait) seconds for the flusher: 2 (+ 3),
    and in a forked child 0.25 (+ 1.5).  An
    exception from a signal handler does not cut the writing short (see
    _write_out).  A part whose writing failed gets nothing more, and so no
    ``exit`` record.
    """
    global _final_done, _stopped, _fence
    try:
        if _final_done or _fd is None or _getpid() != _part_pid:
            # (A forked child whose own part is not set up yet, as when a
            # signal handler calls os._exit in an earlier at-fork hook: the
            # part and the buffer are the parent's.)
            return
        _final_done = True
        _stopped = True  # no new records
        _fence = True  # the flusher writes nothing more
    except BaseException:
        return
    try:
        _wake_flusher()
        _flusher_exited.acquire(timeout=_stop_wait)
    except BaseException:
        pass  # interrupted: taking over before it stops is safe too
    try:
        if not _broken:
            try:
                _check_proc_name()  # pool workers are named after start-up
            except BaseException:
                pass
            try:
                if _count_keys:  # discover mode
                    _shielded(_final_counts)
            except BaseException:
                pass
            head, rest = _shielded(_take_over)
            _write_out(head)
            for i in range(0, len(rest), _CHUNK):
                _write_out(("\n".join(rest[i : i + _CHUNK]) + "\n").encode("ascii"))
            tail = ""
            if _n_dropped:
                msg = f"{_n_dropped:,} calls not recorded (event cap)"
                tail = _dumps({"k": "note", "t": _perf_ns(), "msg": msg}) + "\n"
            # Last, and written directly: nothing can follow it.
            exit_record = {"k": "exit", "t": _perf_ns(), "how": how}
            _write_out((tail + _dumps(exit_record) + "\n").encode("ascii"))
    except OSError as exc:
        if exc.errno is not None:
            _give_up(f"cannot write the part file: {exc}")
    except BaseException:
        pass
    try:
        _teardown()
    except BaseException:
        pass

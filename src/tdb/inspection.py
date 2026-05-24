"""Debuggee-side inspection helpers (UI-free).

Holds the Python expressions evaluated *in the debuggee* via DAP
`evaluate` to enumerate asyncio tasks and child processes, plus the
data classes and JSON parsers that turn the results back into
typed Python.

Lives outside `tdb.widgets` so headless-mode code (the JSON-RPC
server) can import it without dragging in textual/rich. Both the
modal screens (`tdb.widgets.async_tasks_modal`,
`tdb.widgets.processes_modal`) and the RPC server import their
shared bits from here.
"""

from __future__ import annotations

import ast
import json
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


# --- asyncio tasks --------------------------------------------------


@dataclass
class AsyncTaskInfo:
    """Parsed info about a single asyncio task.

    `awaiting` is a short label for the asyncio primitive the task is
    parked on (e.g. `"Lock.acquire"`, `"Queue.get"`, `"asyncio.sleep"`),
    derived in the debuggee from the innermost stack frame. None when
    the task isn't suspended on a recognizable primitive.

    `awaiting_obj_id` is `id()` of that primitive (or of `_fut_waiter`
    for free-function awaits like `asyncio.sleep`). It is only
    meaningful within one snapshot (one round-trip) — used by the
    wait-graph builder to match a blocked task against its holder.
    None when there is no recognizable primitive.

    `cancelling` is `task.cancelling()` (3.11+) — count of pending
    cancellation requests. `cancel_message` is the message passed to
    `cancel()`, when set.
    """
    name: str
    state: str
    coro: str
    stack: list[str]
    variables: dict[str, str] = field(default_factory=dict)
    cancelling: int = 0
    cancel_message: str | None = None
    awaiting: str | None = None
    awaiting_obj_id: int | None = None
    holders: list[str] = field(default_factory=list)


# Python snippet evaluated in the debuggee to collect asyncio task info.
# Uses eval(compile(...)) with a multi-statement body via a namespace dict.
# Handles None coroutines, missing cr_frame, and repr() failures.
#
# The `_classify_awaiting` helper looks at the innermost frame of
# `task.get_stack()` and matches against the asyncio stdlib paths:
# `locks.py`, `queues.py`, `streams.py`, `tasks.py`. For locks/queues/
# streams it pulls `type(self).__name__` from `f_locals` so we can tell
# `Event.wait` from `Condition.wait` (both live in `locks.py`). For
# `tasks.py` (sleep/wait/gather/wait_for) there is no `self`. When no
# frame is recognizable, fall back to the type name of `_fut_waiter`.
TASK_COLLECT_EXPR = """\
(lambda _ns: (exec(r'''
import asyncio, json, re
def _natkey(_s):
    return [int(_p) if _p.isdigit() else _p for _p in re.split(r"(\\d+)", _s)]
def _walk_await(_t):
    # Walk cr_await chain from outermost coroutine to innermost.
    # task.get_stack() does NOT recurse into awaited coroutines, so to
    # see the asyncio primitive a task is parked on we walk it ourselves.
    _coro = _t.get_coro()
    _frames = []
    _seen = set()
    while _coro is not None and id(_coro) not in _seen:
        _seen.add(id(_coro))
        _f = getattr(_coro, "cr_frame", None) or getattr(_coro, "gi_frame", None)
        if _f is None:
            break
        _frames.append(_f)
        _coro = getattr(_coro, "cr_await", None) or getattr(_coro, "gi_yieldfrom", None)
    return _frames
def _classify_awaiting(_t):
    # Returns (label, obj_id). obj_id is id() of the primitive the task
    # is parked on (Lock/Queue/Event/Condition/Semaphore self) or, for
    # free-function awaits in asyncio/tasks.py, id() of _fut_waiter
    # (the Future the task is parked on). Used by the wait-graph builder
    # to match this blocked task against the holder's locals.
    for _f in reversed(_walk_await(_t)):
        _fn = _f.f_code.co_filename
        _cn = _f.f_code.co_name
        if "asyncio" not in _fn:
            continue
        _self = _f.f_locals.get("self")
        _cls = type(_self).__name__ if _self is not None else None
        if (_fn.endswith("locks.py") or _fn.endswith("queues.py") or _fn.endswith("streams.py")) and _cls:
            return (_cls + "." + _cn, id(_self))
        if _fn.endswith("tasks.py"):
            _label = _cn.lstrip("_")
            if _label:
                _fw = getattr(_t, "_fut_waiter", None)
                return ("asyncio." + _label, id(_fw) if _fw is not None else None)
    _fw = getattr(_t, "_fut_waiter", None)
    if _fw is not None:
        return (type(_fw).__name__, id(_fw))
    return (None, None)
def _read_cancel_message(_t):
    for _src in (_t, getattr(_t, "_fut_waiter", None)):
        if _src is None:
            continue
        for _attr in ("_cancel_message", "_cancel_message_must_cancel"):
            _val = getattr(_src, _attr, None)
            if _val is not None:
                return _val
    return None
def _find_holders(_target_t, _target_id, _per_task):
    # Returns sorted list of task names that have an object whose id()
    # matches _target_id in any non-asyncio frame of their cr_await
    # chain. Excludes _target_t itself and any other task that is also
    # blocked on this same primitive (those are co-waiters, not holders).
    # Heuristic — see project memory: a task that received the primitive
    # as a parameter but never acquired it can still be reported. Step 3
    # graph builder applies per-primitive policy on top.
    # Cost is O(N x F x L) per call (N tasks, F frames each, L locals);
    # the outer two-pass loop makes the total O(N^2 x F x L). For programs
    # with hundreds of tasks the inner _walk_await + f_locals iteration
    # is still microseconds-per-task, but a 5000-task program would burn
    # a noticeable amount of CPU on every collection. Caller already
    # guards with len(_per_task) > 500 -> [] above, so this body assumes
    # we're under the threshold.
    if _target_id is None:
        return []
    _holders = set()
    for _other_t, _, _other_awaiting_id in _per_task:
        if _other_t is _target_t:
            continue
        if _other_awaiting_id == _target_id:
            continue
        try:
            _frames = _walk_await(_other_t)
        except Exception:
            continue
        for _f in _frames:
            try:
                if "asyncio" in _f.f_code.co_filename:
                    continue
                _vals = list(_f.f_locals.values())
            except Exception:
                continue
            _hit = False
            for _v in _vals:
                try:
                    if id(_v) == _target_id:
                        _hit = True
                        break
                except Exception:
                    continue
            if _hit:
                _holders.add(_other_t.get_name())
                break
    return sorted(_holders)
_per_task = []
for _t in sorted(asyncio.all_tasks(), key=lambda t: _natkey(t.get_name())):
    try:
        _awaiting, _awaiting_obj_id = _classify_awaiting(_t)
    except Exception:
        _awaiting, _awaiting_obj_id = None, None
    _per_task.append((_t, _awaiting, _awaiting_obj_id))
_result = []
for _t, _awaiting, _awaiting_obj_id in _per_task:
    _coro = _t.get_coro()
    _stack = _t.get_stack() or []
    try:
        _cancelling = _t.cancelling() if hasattr(_t, "cancelling") else 0
    except Exception:
        _cancelling = 0
    _cancel_msg = _read_cancel_message(_t)
    if _cancel_msg is not None and not isinstance(_cancel_msg, str):
        try:
            _cancel_msg = repr(_cancel_msg)
        except Exception:
            _cancel_msg = "<unrepr-able>"
    try:
        if len(_per_task) > 500:
            _holders = []
        else:
            _holders = _find_holders(_t, _awaiting_obj_id, _per_task)
    except Exception:
        _holders = []
    _result.append({
        "name": _t.get_name(),
        "state": "cancelled" if _t.cancelled() else ("done" if _t.done() else "pending"),
        "coro": repr(_coro) if _coro is not None else "(finished)",
        "stack": [
            f"{_f.f_code.co_name} at {_f.f_code.co_filename}:{_f.f_lineno}"
            for _f in _stack
        ],
        "cancelling": _cancelling,
        "cancel_message": _cancel_msg,
        "awaiting": _awaiting,
        "awaiting_obj_id": _awaiting_obj_id,
        "holders": _holders,
    })
''', _ns), _ns.get('json', __import__('json')).dumps(_ns['_result']))[-1])({})
"""

# Expression template to get a task's cr_frame.f_locals as a DAP-inspectable
# object. The placeholder {task_name} is replaced with the task name.
TASK_LOCALS_EXPR = """\
[_t for _t in __import__('asyncio').all_tasks() if _t.get_name() == {task_name!r}][0].get_coro().cr_frame.f_locals\
"""


def _decode_json_repr(raw: str) -> object:
    """DAP `evaluate` wraps strings in their Python repr; unwrap then parse JSON.

    Handles three observed shapes:
      - `'"...json string..."'` (repr of a JSON string)
      - `'...json...'` directly (some adapters elide the wrap)
      - bare list-shaped Python literal (when JSON happens to be valid Python too)
    """
    text = raw.strip()
    try:
        parsed = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return json.loads(text)
    if isinstance(parsed, str):
        return json.loads(parsed)
    if isinstance(parsed, list):
        return parsed
    return json.loads(text)


def build_wait_graph(tasks: list[AsyncTaskInfo]) -> dict[str, list[str]]:
    """Forward wait-graph: `{task_name: [holders]}`.

    A holder edge means "waiter is blocked on a primitive that holder
    has acquired" (Lock/Semaphore/Condition) or, more loosely, "holder
    has the primitive in its frame locals" (Event/Queue). A task with
    no holders maps to an empty list.

    Pure transformation over already-collected `AsyncTaskInfo` —
    debuggee did the heavy lifting in `_find_holders`. Caller can invert
    for a "holder → blocked-tasks" view if a different traversal helps.
    """
    return {t.name: list(t.holders) for t in tasks}


def find_cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    """Find deadlock cycles via Tarjan's SCC.

    Returns each strongly-connected component of size ≥ 2 (or a
    singleton with a self-edge) as a sorted list of node names. A
    non-empty result means at least one deadlock is present. Singleton
    SCCs without self-edges are normal (non-cyclic) and not returned.

    Recursive — fine for wait graphs of <~500 tasks given Python's
    default recursion limit. On a wait chain deeper than the recursion
    limit the inner DFS raises RecursionError; we log and return [],
    so callers see "no cycles" rather than a crashed handler. Swap to
    iterative if this ever becomes load-bearing.
    """
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    counter = [0]
    sccs: list[list[str]] = []

    def strongconnect(v: str) -> None:
        index[v] = counter[0]
        low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in graph.get(v, []):
            if w not in index:
                strongconnect(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            sccs.append(comp)

    nodes: set[str] = set(graph.keys())
    for holders in graph.values():
        nodes.update(holders)
    try:
        for v in sorted(nodes):
            if v not in index:
                strongconnect(v)
    except RecursionError:
        log.warning(
            "find_cycles: recursion limit hit on graph with %d nodes; "
            "returning [] (no deadlock detection for this snapshot)",
            len(nodes),
        )
        return []

    cycles: list[list[str]] = []
    for comp in sccs:
        if len(comp) > 1:
            cycles.append(sorted(comp))
        elif comp[0] in graph.get(comp[0], []):
            cycles.append([comp[0]])
    return sorted(cycles)


@dataclass
class WaitTreeNode:
    """Pure-data wait-graph tree node, ready for tree-style rendering.

    `kind` is a tag the renderer uses to pick styling (e.g. red for
    cycles, dim for orphans). `data` is the task name for nodes that
    correspond to a task — used by the UI to sync selection with the
    task table; None for section/primitive/decorative nodes.

    Lives in inspection.py (not the modal) so it stays UI-free and
    testable without pulling in Textual.
    """
    label: str
    kind: str
    data: str | None = None
    children: list[WaitTreeNode] = field(default_factory=list)


def build_wait_tree(tasks: list[AsyncTaskInfo]) -> list[WaitTreeNode]:
    """Top-level sections for wait-graph display.

    Sections (empty ones omitted), in order:
      1. "Deadlock cycles" — one row per detected cycle.
      2. "Blocked tasks" — each blocked task expands into a primitive
         node, then its holder tasks (which may further expand).
      3. "Running / unblocked" — flat list of names.

    Cycle handling: when descending into holders, a task that is
    already on the current ancestry path is rendered as a `cycle_ref`
    leaf (kind='cycle_ref') and not expanded further — keeps the tree
    finite and signals the deadlock visually.
    """
    by_name = {t.name: t for t in tasks}
    cycles = find_cycles(build_wait_graph(tasks))
    blocked = [t for t in tasks if t.awaiting is not None]
    unblocked = [t for t in tasks if t.awaiting is None]

    sections: list[WaitTreeNode] = []

    if cycles:
        cycle_section = WaitTreeNode(
            label=f"Deadlock cycles ({len(cycles)})",
            kind="section_cycles",
        )
        for cycle in cycles:
            if len(cycle) == 1:
                cycle_section.children.append(WaitTreeNode(
                    label=f"{cycle[0]} (self-cycle)",
                    kind="cycle",
                    data=cycle[0],
                ))
            else:
                cycle_section.children.append(WaitTreeNode(
                    label=" <-> ".join(cycle),
                    kind="cycle",
                    data=cycle[0],
                ))
        sections.append(cycle_section)

    if blocked:
        blocked_section = WaitTreeNode(
            label=f"Blocked tasks ({len(blocked)})",
            kind="section_blocked",
        )
        try:
            for t in blocked:
                blocked_section.children.append(
                    _build_task_subtree(t, by_name, ())
                )
        except RecursionError:
            # _build_task_subtree recurses through the holder chain.
            # If the chain is deeper than the recursion limit, give up
            # on the detail tree but still show the section header so
            # the user knows tasks were blocked.
            log.warning(
                "build_wait_tree: recursion limit hit while expanding "
                "blocked-task subtree; rendering flat list instead",
            )
            blocked_section.children = [
                WaitTreeNode(
                    label=f"{t.name} (subtree truncated — chain too deep)",
                    kind="task",
                    data=t.name,
                )
                for t in blocked
            ]
        sections.append(blocked_section)

    if unblocked:
        running_section = WaitTreeNode(
            label=f"Running / unblocked ({len(unblocked)})",
            kind="section_running",
        )
        for t in unblocked:
            running_section.children.append(WaitTreeNode(
                label=t.name,
                kind="task_unblocked",
                data=t.name,
            ))
        sections.append(running_section)

    return sections


def _build_task_subtree(
    task: AsyncTaskInfo,
    by_name: dict[str, AsyncTaskInfo],
    path: tuple[str, ...],
) -> WaitTreeNode:
    if task.name in path:
        return WaitTreeNode(
            label=f"{task.name} (cycle ref)",
            kind="cycle_ref",
            data=task.name,
        )

    node = WaitTreeNode(label=task.name, kind="task", data=task.name)
    if task.awaiting is None:
        return node

    primitive = WaitTreeNode(
        label=f"awaiting: {task.awaiting}",
        kind="primitive",
    )
    node.children.append(primitive)

    if not task.holders:
        primitive.children.append(WaitTreeNode(
            label="(no holder identified)",
            kind="no_holder",
        ))
        return node

    new_path = path + (task.name,)
    for holder_name in task.holders:
        holder = by_name.get(holder_name)
        if holder is None:
            primitive.children.append(WaitTreeNode(
                label=f"{holder_name} (not in snapshot)",
                kind="orphan",
                data=holder_name,
            ))
        else:
            primitive.children.append(
                _build_task_subtree(holder, by_name, new_path)
            )
    return node


def parse_task_json(raw: str) -> list[AsyncTaskInfo]:
    """Parse the JSON output from TASK_COLLECT_EXPR into AsyncTaskInfo list."""
    try:
        data = _decode_json_repr(raw)
        return [
            AsyncTaskInfo(
                name=d["name"],
                state=d["state"],
                coro=d["coro"],
                stack=d.get("stack", []),
                variables=d.get("variables", {}),
                cancelling=d.get("cancelling", 0) or 0,
                cancel_message=d.get("cancel_message"),
                awaiting=d.get("awaiting"),
                awaiting_obj_id=d.get("awaiting_obj_id"),
                holders=list(d.get("holders") or []),
            )
            for d in data
        ]
    except Exception:
        log.exception("Failed to parse async task info: %s", raw[:200])
        return []


# --- multiprocessing children --------------------------------------


@dataclass
class ProcessInfo:
    """Parsed info about a single child process."""
    name: str
    pid: int | None
    alive: bool
    exitcode: int | None
    daemon: bool
    target: str
    args: str
    kwargs: str
    start_method: str


# Python snippet evaluated in the debuggee to collect child process info.
PROCESS_COLLECT_EXPR = """\
(lambda _ns: (exec('''
import json, multiprocessing
_result = []
for _p in multiprocessing.active_children():
    try:
        _tgt = repr(getattr(_p, "_target", None))[:200]
    except Exception:
        _tgt = "<unknown>"
    try:
        _args = repr(getattr(_p, "_args", ()))[:200]
    except Exception:
        _args = "()"
    try:
        _kwargs = repr(getattr(_p, "_kwargs", {}))[:200]
    except Exception:
        _kwargs = "{}"
    _result.append({
        "name": _p.name,
        "pid": _p.pid,
        "alive": _p.is_alive(),
        "exitcode": _p.exitcode,
        "daemon": _p.daemon,
        "target": _tgt,
        "args": _args,
        "kwargs": _kwargs,
        "start_method": getattr(_p, "_start_method", "") or "",
    })
_result.sort(key=lambda x: x["name"])
''', _ns), _ns.get('json', __import__('json')).dumps(_ns['_result']))[-1])({})
"""


def parse_process_json(raw: str) -> list[ProcessInfo]:
    """Parse the JSON output from PROCESS_COLLECT_EXPR into ProcessInfo list."""
    try:
        data = _decode_json_repr(raw)
        return [
            ProcessInfo(
                name=d["name"],
                pid=d.get("pid"),
                alive=d.get("alive", False),
                exitcode=d.get("exitcode"),
                daemon=d.get("daemon", False),
                target=d.get("target", "None"),
                args=d.get("args", "()"),
                kwargs=d.get("kwargs", "{}"),
                start_method=d.get("start_method", ""),
            )
            for d in data
        ]
    except Exception:
        log.exception("Failed to parse process info: %s", raw[:200])
        return []

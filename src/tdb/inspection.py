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
    """Parsed info about a single asyncio task."""
    name: str
    state: str
    coro: str
    stack: list[str]
    variables: dict[str, str] = field(default_factory=dict)


# Python snippet evaluated in the debuggee to collect asyncio task info.
# Uses eval(compile(...)) with a multi-statement body via a namespace dict.
# Handles None coroutines, missing cr_frame, and repr() failures.
TASK_COLLECT_EXPR = """\
(lambda _ns: (exec('''
import asyncio, json, re
def _natkey(_s):
    return [int(_p) if _p.isdigit() else _p for _p in re.split(r"(\\d+)", _s)]
_result = []
for _t in sorted(asyncio.all_tasks(), key=lambda t: _natkey(t.get_name())):
    _coro = _t.get_coro()
    _result.append({
        "name": _t.get_name(),
        "state": "cancelled" if _t.cancelled() else ("done" if _t.done() else "pending"),
        "coro": repr(_coro) if _coro is not None else "(finished)",
        "stack": [
            f"{_f.f_code.co_name} at {_f.f_code.co_filename}:{_f.f_lineno}"
            for _f in (_t.get_stack() or [])
        ],
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

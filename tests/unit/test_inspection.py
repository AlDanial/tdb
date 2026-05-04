"""Unit tests for tdb.inspection (parse helpers and headless-friendliness)."""

from __future__ import annotations

import json
import sys

from tdb.inspection import (
    AsyncTaskInfo,
    PROCESS_COLLECT_EXPR,
    ProcessInfo,
    TASK_COLLECT_EXPR,
    TASK_LOCALS_EXPR,
    parse_process_json,
    parse_task_json,
)


# --- parse_task_json -----------------------------------------------------

def _wrap_repr(s: str) -> str:
    """DAP wraps an evaluate result as the Python repr of the string."""
    return repr(s)


def test_parse_task_json_from_repr_wrapped():
    """The common DAP path: result is repr(json_string)."""
    payload = json.dumps([{
        "name": "Task-1",
        "state": "pending",
        "coro": "<coroutine main()>",
        "stack": ["frame at /x.py:1"],
    }])
    raw = _wrap_repr(payload)
    tasks = parse_task_json(raw)
    assert len(tasks) == 1
    assert isinstance(tasks[0], AsyncTaskInfo)
    assert tasks[0].name == "Task-1"
    assert tasks[0].state == "pending"
    assert tasks[0].stack == ["frame at /x.py:1"]


def test_parse_task_json_handles_bare_json():
    """Some adapters return the JSON directly without repr-wrapping."""
    raw = json.dumps([{
        "name": "X", "state": "done", "coro": "()", "stack": [],
    }])
    tasks = parse_task_json(raw)
    assert tasks[0].name == "X"


def test_parse_task_json_returns_empty_on_garbage():
    assert parse_task_json("not json at all") == []


def test_parse_task_json_empty_list():
    assert parse_task_json(_wrap_repr("[]")) == []


def test_async_task_info_default_variables_is_empty_dict():
    info = AsyncTaskInfo(name="t", state="pending", coro="c", stack=[])
    assert info.variables == {}


def test_async_task_info_default_metadata_fields():
    """New cancellation/awaiting fields must default to safe values so a
    debuggee that doesn't report them (older Python, error path) still
    parses cleanly."""
    info = AsyncTaskInfo(name="t", state="pending", coro="c", stack=[])
    assert info.cancelling == 0
    assert info.cancel_message is None
    assert info.awaiting is None


def test_parse_task_json_reads_metadata_fields():
    payload = json.dumps([{
        "name": "T", "state": "pending", "coro": "c", "stack": [],
        "cancelling": 2, "cancel_message": "shutdown",
        "awaiting": "Lock.acquire",
    }])
    [t] = parse_task_json(_wrap_repr(payload))
    assert t.cancelling == 2
    assert t.cancel_message == "shutdown"
    assert t.awaiting == "Lock.acquire"


def test_parse_task_json_tolerates_null_cancelling():
    """Some debuggees may send a null when cancelling() raised; parser
    must coerce to 0, not propagate None."""
    payload = json.dumps([{
        "name": "T", "state": "pending", "coro": "c", "stack": [],
        "cancelling": None,
    }])
    [t] = parse_task_json(_wrap_repr(payload))
    assert t.cancelling == 0


# --- parse_process_json --------------------------------------------------

def test_parse_process_json_round_trip():
    payload = json.dumps([{
        "name": "ForkPoolWorker-1",
        "pid": 12345,
        "alive": True,
        "exitcode": None,
        "daemon": True,
        "target": "<function worker>",
        "args": "()",
        "kwargs": "{}",
        "start_method": "fork",
    }])
    procs = parse_process_json(_wrap_repr(payload))
    assert len(procs) == 1
    p = procs[0]
    assert isinstance(p, ProcessInfo)
    assert p.name == "ForkPoolWorker-1"
    assert p.pid == 12345
    assert p.alive is True
    assert p.daemon is True
    assert p.start_method == "fork"


def test_parse_process_json_handles_missing_optional_fields():
    payload = json.dumps([{"name": "min", "alive": False}])
    procs = parse_process_json(_wrap_repr(payload))
    assert procs[0].pid is None
    assert procs[0].exitcode is None
    assert procs[0].args == "()"
    assert procs[0].kwargs == "{}"


def test_parse_process_json_returns_empty_on_failure():
    assert parse_process_json("nope") == []


# --- expression sanity checks --------------------------------------------

def test_task_collect_expr_is_a_python_expression():
    """Compile but don't execute — proves syntax is valid before debugpy
    has a chance to swallow a SyntaxError silently."""
    compile(TASK_COLLECT_EXPR, "<TASK_COLLECT_EXPR>", "eval")


def test_process_collect_expr_is_a_python_expression():
    compile(PROCESS_COLLECT_EXPR, "<PROCESS_COLLECT_EXPR>", "eval")


def test_task_locals_expr_template_renders():
    rendered = TASK_LOCALS_EXPR.format(task_name="My-Task")
    assert "'My-Task'" in rendered
    compile(rendered, "<TASK_LOCALS_EXPR>", "eval")


# --- import hygiene ------------------------------------------------------

def test_inspection_module_has_no_textual_dependency():
    """Importing tdb.inspection must NOT pull textual or rich.

    This is the architectural invariant the module was created to enforce
    so headless mode stays lightweight.
    """
    # Drop any cached copy so the import is observable.
    for name in list(sys.modules):
        if name.startswith("tdb.inspection"):
            del sys.modules[name]
    # Snapshot whether textual was loaded BEFORE we import inspection,
    # so a previous test that loaded a widget doesn't taint this one.
    pre_textual = "textual" in sys.modules
    pre_rich = "rich" in sys.modules
    import tdb.inspection  # noqa: F401
    if not pre_textual:
        assert "textual" not in sys.modules, (
            "tdb.inspection must not import textual"
        )
    if not pre_rich:
        assert "rich" not in sys.modules, "tdb.inspection must not import rich"


def test_server_app_has_no_textual_dependency_in_a_fresh_interpreter():
    """`tdb.server.app` is the headless RPC entry — importing it must not
    drag textual into a fresh process.

    We spawn a subprocess so any textual already imported in this
    pytest run doesn't taint the check.
    """
    import subprocess
    code = (
        "import sys; "
        "import tdb.server.app; "
        "assert 'textual' not in sys.modules, sorted("
        "  m for m in sys.modules if m.startswith('textual'))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"server.app pulled textual:\n{result.stdout}\n{result.stderr}"
    )

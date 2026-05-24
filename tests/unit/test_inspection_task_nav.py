"""Tests for InspectionWorkflows.navigate_to_task — the parser that
turns AsyncTaskInfo.stack (list[str] of "func at /path/file.py:line"
entries) into synthetic StackFrame objects for the main Stack View.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from tdb.app_handlers.inspection import InspectionWorkflows, _TASK_FRAME_RE
from tdb.app_handlers.ui_panels import UIPanels
from tdb.inspection import AsyncTaskInfo
from tdb.session.state import DebugState


def _make_workflows_with_tasks(tasks: list[AsyncTaskInfo]) -> tuple[InspectionWorkflows, DebugState]:
    """Build a minimal stand-in for TdbApp wired up enough for
    navigate_to_task to read the tasks list and mutate controller.state.

    Uses a real DebugState (not a SimpleNamespace) so the workflow's
    `state.set_stack(...)` call resolves to the actual method.
    """
    state = DebugState()
    controller = SimpleNamespace(state=state)
    modal = SimpleNamespace(items=tasks)
    panels = UIPanels()
    panels.async_tasks = modal
    app = SimpleNamespace(controller=controller, panels=panels)
    return InspectionWorkflows(app), state


def _task(name: str, stack: list[str]) -> AsyncTaskInfo:
    return AsyncTaskInfo(name=name, state="pending", coro="coro()", stack=stack)


def test_task_frame_regex_parses_well_formed_entries():
    m = _TASK_FRAME_RE.match("main at /home/al/x.py:42")
    assert m is not None
    assert m.group(1) == "main"
    assert m.group(2) == "/home/al/x.py"
    assert m.group(3) == "42"


def test_task_frame_regex_handles_special_func_names():
    # Lambdas, dunders, qualified names should all parse.
    for func in ("<lambda>", "__init__", "MyClass.method"):
        m = _TASK_FRAME_RE.match(f"{func} at /tmp/a.py:1")
        assert m is not None and m.group(1) == func


def test_navigate_to_task_populates_synthetic_frames():
    tasks = [_task("Task-1", [
        "inner at /tmp/a.py:10",
        "outer at /tmp/b.py:20",
    ])]
    workflows, state = _make_workflows_with_tasks(tasks)

    assert workflows.navigate_to_task("Task-1") is True
    assert len(state.stack_frames) == 2
    top = state.stack_frames[0]
    assert top.name == "inner"
    assert top.source.path == "/tmp/a.py"
    assert top.source.name == "a.py"
    assert top.line == 10
    # Synthetic-ness now lives on the state flag (replacing the old
    # negative-id sentinel). Frame ids are natural ints unique within
    # the stack.
    assert state.displayed_frames_are_synthetic is True
    assert top.id != state.stack_frames[1].id
    assert state.current_frame_id == top.id
    # No live DAP scopes/variables for a suspended task.
    assert state.scopes == []
    assert state.variables == {}


def test_navigate_to_task_skips_malformed_entries():
    tasks = [_task("Task-1", [
        "garbage with no colon line",
        "good at /tmp/x.py:5",
        "",
    ])]
    workflows, state = _make_workflows_with_tasks(tasks)

    assert workflows.navigate_to_task("Task-1") is True
    assert len(state.stack_frames) == 1
    assert state.stack_frames[0].name == "good"
    assert state.stack_frames[0].line == 5


def test_navigate_to_task_returns_false_for_unknown_task():
    tasks = [_task("Task-1", ["main at /tmp/a.py:1"])]
    workflows, state = _make_workflows_with_tasks(tasks)

    assert workflows.navigate_to_task("Task-Missing") is False
    assert state.stack_frames == []


def test_navigate_to_task_returns_false_for_empty_stack():
    tasks = [_task("Task-1", [])]
    workflows, state = _make_workflows_with_tasks(tasks)

    assert workflows.navigate_to_task("Task-1") is False
    assert state.stack_frames == []


def test_navigate_to_task_returns_false_when_all_entries_malformed():
    tasks = [_task("Task-1", ["only garbage", "more garbage"])]
    workflows, state = _make_workflows_with_tasks(tasks)

    assert workflows.navigate_to_task("Task-1") is False
    assert state.stack_frames == []


def test_navigate_to_task_no_modal_returns_false():
    state = DebugState()
    controller = SimpleNamespace(state=state)
    # panels.async_tasks is None — modal not open.
    app = SimpleNamespace(controller=controller, panels=UIPanels())
    workflows = InspectionWorkflows(app)
    assert workflows.navigate_to_task("Task-1") is False

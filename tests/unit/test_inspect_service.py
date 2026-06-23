"""Unit tests for tdb.session.inspect_service.InspectService.

The service is the shared fetch-and-parse layer behind both the TUI's
InspectionWorkflows and the RPC server's task/thread/process actions.
Tests use a real DebugController (cheap to construct) with monkeypatched
evaluate / client methods, mirroring the style of test_rpc_handlers.py.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tdb.dap.types import Scope, Source, StackFrame, Thread, Variable
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController
from tdb.session.inspect_service import InspectService, SessionGateError
from tdb.session.state import SessionPhase


def _service(phase: SessionPhase = SessionPhase.STOPPED):
    controller = DebugController(ServerEventHandler())
    controller.state.transition_to(phase)
    return InspectService(lambda: controller), controller


def _task_payload(name: str = "Task-1", awaiting: str | None = None) -> str:
    return json.dumps(
        [
            {
                "name": name,
                "state": "pending",
                "coro": "main()",
                "stack": ["main at /app/main.py:10"],
                "awaiting": awaiting,
            }
        ]
    )


# --- Gates ---------------------------------------------------------------


@pytest.mark.parametrize(
    "phase,reason",
    [
        (SessionPhase.RUNNING, "running"),
        (SessionPhase.TERMINATED, "terminated"),
    ],
)
async def test_collect_tasks_gates(phase, reason):
    svc, _ = _service(phase)
    with pytest.raises(SessionGateError) as ei:
        await svc.collect_tasks()
    assert ei.value.reason == reason


async def test_list_threads_gates_when_running():
    svc, _ = _service(SessionPhase.RUNNING)
    with pytest.raises(SessionGateError):
        await svc.list_threads()


async def test_collect_processes_gates_when_terminated():
    svc, _ = _service(SessionPhase.TERMINATED)
    with pytest.raises(SessionGateError):
        await svc.collect_processes()


# --- Tasks ---------------------------------------------------------------


async def test_collect_tasks_parses_payload(monkeypatch):
    svc, ctrl = _service()

    async def fake_evaluate(expr: str) -> str:
        return _task_payload(awaiting="Lock.acquire")

    monkeypatch.setattr(ctrl, "evaluate", fake_evaluate)
    tasks = await svc.collect_tasks()
    assert len(tasks) == 1
    assert tasks[0].name == "Task-1"
    assert tasks[0].awaiting == "Lock.acquire"


async def test_collect_tasks_empty_on_unparseable_payload(monkeypatch):
    """When the in-debuggee evaluate failed, controller.evaluate returns
    the error string. `parse_task_json` swallows the parse failure and
    yields [] (logging the payload) — preserved here so both consumers
    keep their established 'no tasks found' rendering for that case."""
    svc, ctrl = _service()

    async def fake_evaluate(expr: str) -> str:
        return "NameError: name 'asyncio' is not defined"

    monkeypatch.setattr(ctrl, "evaluate", fake_evaluate)
    assert await svc.collect_tasks() == []


async def test_task_locals_returns_empty_on_failure(monkeypatch):
    """A dead frame / failed evaluate renders as 'no variables', not an
    exception — both consumers display an empty-variables state."""
    svc, ctrl = _service()

    async def boom(*a, **k):
        raise RuntimeError("no frame")

    monkeypatch.setattr(ctrl, "resolve_evaluate_frame_id", boom)
    assert await svc.task_locals("Task-1") == []


async def test_task_locals_uses_active_client(monkeypatch):
    """Locals must be evaluated against the *active* client (which may be
    a child process), not unconditionally against the parent."""
    svc, ctrl = _service()
    calls = SimpleNamespace(evaluated_on=None)

    class FakeClient:
        async def evaluate(self, expr, frame_id=None, context="repl"):
            calls.evaluated_on = self
            return "ok", 7

        async def variables(self, ref):
            assert ref == 7
            return [Variable(name="x", value="1", type="int")]

    fake = FakeClient()
    monkeypatch.setattr(type(ctrl), "active_client", property(lambda self: fake))

    async def fake_resolve(client):
        assert client is fake
        return 99

    monkeypatch.setattr(ctrl, "resolve_evaluate_frame_id", fake_resolve)
    variables = await svc.task_locals("Task-1")
    assert calls.evaluated_on is fake
    assert [v.name for v in variables] == ["x"]


# --- Threads ---------------------------------------------------------------


async def test_thread_stack_shape(monkeypatch):
    svc, ctrl = _service()
    frame = StackFrame(id=1, name="main", source=Source(path="/app/main.py"), line=3)
    scope = Scope(name="Locals", variables_reference=11)

    class FakeClient:
        async def stack_trace(self, tid):
            assert tid == 5
            return [frame]

        async def scopes(self, frame_id):
            assert frame_id == 1
            return [scope]

        async def variables(self, ref):
            return [Variable(name="n", value="42", type="int")]

    monkeypatch.setattr(ctrl, "client", FakeClient())
    frames, scopes, variables = await svc.thread_stack(5)
    assert frames == [frame]
    assert scopes == [scope]
    assert [v.name for v in variables[11]] == ["n"]


async def test_thread_stack_variables_best_effort(monkeypatch):
    """Scope/variable failures degrade to partial results — the stack is
    still useful without locals."""
    svc, ctrl = _service()
    frame = StackFrame(id=1, name="main", source=None, line=3)

    class FakeClient:
        async def stack_trace(self, tid):
            return [frame]

        async def scopes(self, frame_id):
            raise RuntimeError("scopes unavailable")

    monkeypatch.setattr(ctrl, "client", FakeClient())
    frames, scopes, variables = await svc.thread_stack(5)
    assert frames == [frame]
    assert scopes == []
    assert variables == {}


async def test_list_threads_passthrough(monkeypatch):
    svc, ctrl = _service()
    expected = [Thread(id=1, name="MainThread")]

    class FakeClient:
        async def threads(self):
            return expected

    monkeypatch.setattr(ctrl, "client", FakeClient())
    assert await svc.list_threads() == expected


# --- Processes ---------------------------------------------------------------


async def test_collect_processes_evaluates_on_parent(monkeypatch):
    """The collect expression must run on the parent — children don't
    know their siblings. This was the behavior the TUI had and the RPC
    server lacked before the shared service existed."""
    svc, ctrl = _service()
    payload = json.dumps(
        [
            {
                "name": "Worker-1",
                "pid": 4242,
                "alive": True,
                "exitcode": None,
                "daemon": False,
                "target": "work()",
                "args": "()",
                "kwargs": "{}",
                "start_method": "fork",
            }
        ]
    )
    called = SimpleNamespace(on_parent=False)

    async def fake_on_parent(expr: str) -> str:
        called.on_parent = True
        return payload

    monkeypatch.setattr(ctrl, "evaluate_on_parent", fake_on_parent)
    processes = await svc.collect_processes()
    assert called.on_parent
    assert processes[0].pid == 4242


async def test_collect_processes_falls_back_to_proc(monkeypatch):
    """When the parent evaluate fails, fall back to /proc entries built
    from tracked child PIDs. With no tracked children that's []."""
    svc, ctrl = _service()

    async def fake_on_parent(expr: str) -> str:
        raise RuntimeError("parent not stopped")

    monkeypatch.setattr(ctrl, "evaluate_on_parent", fake_on_parent)
    monkeypatch.setattr(ctrl, "get_child_pids", lambda: [])
    assert await svc.collect_processes() == []


async def test_process_stack_none_without_client(monkeypatch):
    svc, ctrl = _service()
    monkeypatch.setattr(ctrl, "get_child_client", lambda pid: None)
    assert await svc.process_stack(1234) is None


async def test_service_follows_controller_swap():
    """The provider indirection must track ControllerRef-style swaps."""
    first = DebugController(ServerEventHandler())
    first.state.transition_to(SessionPhase.TERMINATED)
    holder = SimpleNamespace(ctrl=first)
    svc = InspectService(lambda: holder.ctrl)
    with pytest.raises(SessionGateError):
        await svc.collect_tasks()

    second = DebugController(ServerEventHandler())
    second.state.transition_to(SessionPhase.RUNNING)
    holder.ctrl = second
    with pytest.raises(SessionGateError) as ei:
        await svc.collect_tasks()
    assert ei.value.reason == "running"

"""In-process unit tests for RpcHandlers.

The point of #3 was to make every RPC handler testable without spawning
a subprocess. These tests use a fake DAPClient + a real controller so
breakpoint state, dispatch, and validation paths can run in milliseconds.
"""

from __future__ import annotations

import pytest

from tdb.dap.types import SourceBreakpoint, Thread
from tdb.server.event_handler import ServerEventHandler
from tdb.server.handlers import ControllerRef, RpcHandlers, _parse_file_line
from tdb.session.controller import DebugController
from tdb.session.state import SessionPhase


# --- Fakes / helpers ----------------------------------------------------


class _FakeDAPClient:
    """Just enough surface to avoid AttributeError when the controller
    proxies the easy cases. Tests that exercise actual DAP calls install
    their own attribute stubs as needed."""

    def __init__(self) -> None:
        self.set_breakpoints_calls: list[tuple[str, list[SourceBreakpoint]]] = []
        self._threads: list[Thread] = []

    async def set_breakpoints(self, source_path, breakpoints):
        self.set_breakpoints_calls.append((source_path, list(breakpoints)))
        return []

    async def threads(self) -> list[Thread]:
        return list(self._threads)


@pytest.fixture
def handlers() -> RpcHandlers:
    """Fresh RpcHandlers wired to a controller with a stub DAPClient.

    The state machine is set to "ready and stopped" so dispatch-table
    routing and validation paths can run without a real DAP session.
    """
    eh = ServerEventHandler()
    controller = DebugController(eh)
    controller.client = _FakeDAPClient()
    # Pretend the session is up and currently paused so dispatch-table
    # routing and validation paths can run without a real DAP session.
    controller.state.transition_to(SessionPhase.STOPPED)
    return RpcHandlers(ControllerRef(controller), eh)


# --- _parse_file_line ---------------------------------------------------


def test_parse_file_line_resolves_path(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("\n")
    p, line = _parse_file_line(f"{f}:42")
    assert p == str(f.resolve())
    assert line == 42


def test_parse_file_line_handles_drive_letter_style(tmp_path):
    """rsplit(':', 1) should split on the LAST colon, leaving Windows-style
    'C:/path:line' intact."""
    p, line = _parse_file_line("C:/foo/bar.py:7")
    assert p.endswith("bar.py")
    assert line == 7


def test_parse_file_line_rejects_no_colon():
    with pytest.raises(ValueError):
        _parse_file_line("nocolon")


def test_parse_file_line_rejects_non_numeric_line():
    with pytest.raises(ValueError):
        _parse_file_line("/x.py:abc")


# --- Dispatch table -----------------------------------------------------


def test_dispatch_table_covers_every_action(handlers):
    table = handlers.dispatch_table()
    declared = set(RpcHandlers.ACTIONS)
    assert set(table) == declared
    # Every entry must be a coroutine function.
    import inspect

    for name, fn in table.items():
        assert inspect.iscoroutinefunction(fn), name


def test_action_help_lists_known_actions(handlers):
    rsp = pytest.run = None  # silence linters
    import asyncio

    rsp = asyncio.run(handlers.action_help([]))
    assert rsp.success is True
    assert "set_breakpoint" in rsp.value
    assert "continue" in rsp.value


# --- Breakpoint actions (no DAP needed once is_terminated guards trip) --


async def test_set_breakpoint_validation_rejects_missing_params(handlers):
    rsp = await handlers.action_set_breakpoint([])
    assert rsp.success is False
    assert "file:line" in rsp.value


async def test_set_breakpoint_validation_rejects_bad_format(handlers):
    rsp = await handlers.action_set_breakpoint(["nocolon"])
    assert rsp.success is False


async def test_set_breakpoint_validation_rejects_non_numeric_line(handlers, tmp_path):
    f = tmp_path / "x.py"
    f.write_text("\n")
    rsp = await handlers.action_set_breakpoint([f"{f}:abc"])
    assert rsp.success is False
    assert "line" in rsp.value.lower()


async def test_set_breakpoint_round_trips_through_controller(handlers, tmp_path):
    f = tmp_path / "x.py"
    f.write_text("a = 1\n")
    rsp = await handlers.action_set_breakpoint([f"{f}:1"])
    assert rsp.success is True

    # Set must also be visible via list_breakpoints
    listed = await handlers.action_list_breakpoints([])
    assert listed.success
    assert ":1" in listed.value


async def test_set_breakpoint_with_condition(handlers, tmp_path):
    f = tmp_path / "x.py"
    f.write_text("a = 1\n")
    await handlers.action_set_breakpoint([f"{f}:1", "a > 0", "3"])
    listed = await handlers.action_list_breakpoints([])
    assert "condition=a > 0" in listed.value
    assert "hit_condition=3" in listed.value


async def test_remove_breakpoint(handlers, tmp_path):
    f = tmp_path / "x.py"
    f.write_text("a = 1\n")
    await handlers.action_set_breakpoint([f"{f}:1"])
    rsp = await handlers.action_remove_breakpoint([f"{f}:1"])
    assert rsp.success
    listed = await handlers.action_list_breakpoints([])
    assert ":1" not in listed.value


async def test_list_breakpoints_when_empty(handlers):
    rsp = await handlers.action_list_breakpoints([])
    assert rsp.success
    assert "No breakpoints" in rsp.value


# --- Status + termination guards ----------------------------------------


async def test_status_when_running(handlers):
    handlers.controller.state.transition_to(SessionPhase.RUNNING)
    rsp = await handlers.action_status([])
    assert rsp.value == "running"


async def test_status_when_terminated(handlers):
    handlers.controller.state.transition_to(SessionPhase.TERMINATED)
    rsp = await handlers.action_status([])
    assert rsp.value == "terminated"


async def test_status_when_stopped(handlers):
    rsp = await handlers.action_status([])
    assert rsp.success is True
    assert "stopped" in rsp.value or "unknown" in rsp.value


async def test_pause_rejected_when_terminated(handlers):
    handlers.controller.state.transition_to(SessionPhase.TERMINATED)
    rsp = await handlers.action_pause([])
    assert rsp.success is False
    assert "terminated" in rsp.value.lower()


async def test_pause_returns_error_on_timeout(handlers):
    """When the controller's pause() returns False (no stopped event
    arrived in time), the RPC must surface a meaningful error rather
    than a misleading ok."""
    handlers.controller.state.transition_to(SessionPhase.RUNNING)
    handlers.controller.state.current_thread_id = 1

    async def _stub_pause(thread_id):
        return  # accept the request but no event will fire

    handlers.controller.client.pause = _stub_pause

    # Force a fast timeout via the controller default by overriding it.
    async def _quick_pause():
        return await type(handlers.controller).pause(
            handlers.controller,
            timeout=0.05,
        )

    handlers.controller.pause = _quick_pause

    rsp = await handlers.action_pause([])
    assert rsp.success is False
    assert "didn't land" in rsp.value.lower() or "timeout" in rsp.value.lower()


@pytest.mark.parametrize("action_name", ["next", "step_in", "step_out", "continue"])
async def test_step_actions_rejected_when_terminated(handlers, action_name):
    handlers.controller.state.transition_to(SessionPhase.TERMINATED)
    fn = handlers.dispatch_table()[action_name]
    rsp = await fn([])
    assert rsp.success is False
    assert "terminated" in rsp.value.lower()


@pytest.mark.parametrize("action_name", ["next", "step_in", "step_out", "continue"])
async def test_step_actions_rejected_when_running(handlers, action_name):
    handlers.controller.state.transition_to(SessionPhase.RUNNING)
    fn = handlers.dispatch_table()[action_name]
    rsp = await fn([])
    assert rsp.success is False
    assert "running" in rsp.value.lower()


async def test_evaluate_rejects_empty_params(handlers):
    rsp = await handlers.action_evaluate([])
    assert rsp.success is False


async def test_evaluate_rejected_when_running(handlers):
    handlers.controller.state.transition_to(SessionPhase.RUNNING)
    rsp = await handlers.action_evaluate(["x"])
    assert rsp.success is False
    assert "running" in rsp.value.lower()


async def test_inspect_rejects_empty_params(handlers):
    rsp = await handlers.action_inspect([])
    assert rsp.success is False


async def test_inspect_rejected_when_running(handlers):
    handlers.controller.state.transition_to(SessionPhase.RUNNING)
    rsp = await handlers.action_inspect(["x"])
    assert rsp.success is False


# --- get_source ---------------------------------------------------------


async def test_get_source_reads_file(handlers, tmp_path):
    f = tmp_path / "hello.py"
    f.write_text("print('hi')\n")
    rsp = await handlers.action_get_source([str(f)])
    assert rsp.success
    assert "print('hi')" in rsp.value


async def test_get_source_missing_param(handlers):
    rsp = await handlers.action_get_source([])
    assert rsp.success is False


async def test_get_source_missing_file(handlers, tmp_path):
    rsp = await handlers.action_get_source([str(tmp_path / "missing.py")])
    assert rsp.success is False


# --- Stack actions ------------------------------------------------------


async def test_get_stack_trace_when_no_frames(handlers):
    rsp = await handlers.action_get_stack_trace([])
    assert rsp.success is False
    assert "stack" in rsp.value.lower()


async def test_stack_up_at_top_returns_error(handlers):
    rsp = await handlers.action_stack_up([])
    assert rsp.success is False


async def test_stack_down_at_bottom_returns_error(handlers):
    rsp = await handlers.action_stack_down([])
    assert rsp.success is False


# --- Threads ------------------------------------------------------------


async def test_list_threads_rejected_when_terminated(handlers):
    handlers.controller.state.transition_to(SessionPhase.TERMINATED)
    rsp = await handlers.action_list_threads([])
    assert rsp.success is False


async def test_inspect_thread_invalid_id(handlers):
    rsp = await handlers.action_inspect_thread(["not-an-int"])
    assert rsp.success is False


async def test_inspect_thread_missing_param(handlers):
    rsp = await handlers.action_inspect_thread([])
    assert rsp.success is False


# --- Output drain -------------------------------------------------------


async def test_get_output_drains_buffer(handlers):
    handlers.event_handler.on_output("hello\n", "stdout")
    handlers.event_handler.on_output("world\n", "stderr")
    rsp = await handlers.action_get_output([])
    assert rsp.success
    assert "hello" in rsp.value and "world" in rsp.value
    # And buffer is now empty.
    rsp2 = await handlers.action_get_output([])
    assert rsp2.value == ""


# --- wait_graph ---------------------------------------------------------

import json as _json


def _stub_evaluate(handlers, payload):
    """Replace controller.evaluate with a coroutine returning `payload`
    serialized as JSON. The handler's parse_task_json accepts bare JSON."""

    async def _fake_evaluate(_expr: str) -> str:
        return _json.dumps(payload)

    handlers.controller.evaluate = _fake_evaluate


async def test_wait_graph_rejected_when_running(handlers):
    handlers.controller.state.transition_to(SessionPhase.RUNNING)
    rsp = await handlers.action_wait_graph([])
    assert rsp.success is False
    assert "running" in rsp.value.lower()


async def test_wait_graph_rejected_when_terminated(handlers):
    handlers.controller.state.transition_to(SessionPhase.TERMINATED)
    rsp = await handlers.action_wait_graph([])
    assert rsp.success is False
    assert "terminated" in rsp.value.lower()


async def test_wait_graph_no_tasks(handlers):
    _stub_evaluate(handlers, [])
    rsp = await handlers.action_wait_graph([])
    assert rsp.success
    assert "No asyncio tasks" in rsp.value


async def test_wait_graph_no_blocked_tasks(handlers):
    _stub_evaluate(
        handlers,
        [
            {"name": "A", "state": "pending", "coro": "c", "stack": []},
            {"name": "B", "state": "pending", "coro": "c", "stack": []},
        ],
    )
    rsp = await handlers.action_wait_graph([])
    assert rsp.success
    assert "2 task(s)" in rsp.value
    assert "0 blocked" in rsp.value
    assert "No blocked tasks" in rsp.value
    assert "Deadlock" not in rsp.value


async def test_wait_graph_renders_blocked_tasks_with_holders(handlers):
    _stub_evaluate(
        handlers,
        [
            {
                "name": "Holder",
                "state": "pending",
                "coro": "c",
                "stack": [],
            },
            {
                "name": "Waiter",
                "state": "pending",
                "coro": "c",
                "stack": [],
                "awaiting": "Lock.acquire",
                "awaiting_obj_id": 42,
                "holders": ["Holder"],
            },
        ],
    )
    rsp = await handlers.action_wait_graph([])
    assert rsp.success
    assert "Blocked tasks:" in rsp.value
    assert "Waiter" in rsp.value
    assert "Lock.acquire" in rsp.value
    assert "holders: Holder" in rsp.value
    # No deadlock — Holder doesn't wait on anything.
    assert "Deadlock" not in rsp.value


async def test_wait_graph_blocked_with_no_identified_holder(handlers):
    """A blocked task with empty holders list (e.g., Event.wait, or
    a Lock whose holder couldn't be identified) renders explicitly."""
    _stub_evaluate(
        handlers,
        [
            {
                "name": "T",
                "state": "pending",
                "coro": "c",
                "stack": [],
                "awaiting": "Event.wait",
                "awaiting_obj_id": 99,
                "holders": [],
            },
        ],
    )
    rsp = await handlers.action_wait_graph([])
    assert rsp.success
    assert "(no holder identified)" in rsp.value


async def test_wait_graph_reports_deadlock(handlers):
    """A→B and B→A is a 2-task deadlock."""
    _stub_evaluate(
        handlers,
        [
            {
                "name": "A",
                "state": "pending",
                "coro": "c",
                "stack": [],
                "awaiting": "Lock.acquire",
                "awaiting_obj_id": 1,
                "holders": ["B"],
            },
            {
                "name": "B",
                "state": "pending",
                "coro": "c",
                "stack": [],
                "awaiting": "Lock.acquire",
                "awaiting_obj_id": 2,
                "holders": ["A"],
            },
        ],
    )
    rsp = await handlers.action_wait_graph([])
    assert rsp.success
    assert "1 deadlock cycle(s)" in rsp.value
    assert "Deadlock cycles:" in rsp.value
    assert "A <-> B" in rsp.value


async def test_wait_graph_self_cycle_rendering(handlers):
    """Pathological self-cycle (task waits on a primitive it itself
    holds) — rendered with a (self-cycle) tag."""
    _stub_evaluate(
        handlers,
        [
            {
                "name": "Solo",
                "state": "pending",
                "coro": "c",
                "stack": [],
                "awaiting": "Lock.acquire",
                "awaiting_obj_id": 1,
                "holders": ["Solo"],
            },
        ],
    )
    rsp = await handlers.action_wait_graph([])
    assert rsp.success
    assert "Solo (self-cycle)" in rsp.value


async def test_wait_graph_in_dispatch_table(handlers):
    """Verify the action is wired into the dispatch table — guards
    against a future refactor accidentally dropping it."""
    assert "wait_graph" in handlers.dispatch_table()
    assert "wait_graph" in RpcHandlers.ACTIONS
    assert "wait_graph" in RpcHandlers.ACTION_HELP


# --- Mid-action gate races (phase changes between service calls) --------
#
# Inspector actions make more than one InspectService call (e.g.
# inspect_task: collect_tasks then task_locals; inspect_thread:
# list_threads then thread_stack). The service re-gates on every entry,
# so a `terminated` / `continued` DAP event landing between the calls
# raises SessionGateError mid-action. The handler must map that to the
# same wording as the up-front gate — not let it escape to the
# dispatcher's generic exception handler, and not mislabel it as a
# fetch failure.


async def test_inspect_task_gate_race_maps_to_gate_error(handlers, monkeypatch):
    from tdb.session import inspect_service as _isvc

    ctrl = handlers.controller
    payload = _json.dumps(
        [{"name": "Task-1", "state": "pending", "coro": "main()", "stack": []}]
    )

    async def fake_evaluate(expr: str) -> str:
        return payload

    monkeypatch.setattr(ctrl, "evaluate", fake_evaluate)

    # Flip the phase after collect_tasks succeeds but before task_locals
    # gates, by hooking resolve_evaluate_frame_id (first await inside
    # task_locals after its gate would be too late — so flip inside the
    # gate's read path instead: transition right before calling).
    original_gate = _isvc.InspectService._gate

    def racing_gate(self):
        original_gate(self)
        # After the first successful gate (collect_tasks), terminate the
        # session so the *next* gate (task_locals) trips.
        ctrl.state.transition_to(SessionPhase.TERMINATED)

    monkeypatch.setattr(_isvc.InspectService, "_gate", racing_gate)

    rsp = await handlers.action_inspect_task(["Task-1"])
    assert rsp.success is False
    assert rsp.value == "Program has terminated"


async def test_inspect_thread_gate_race_maps_to_gate_error(handlers, monkeypatch):
    from tdb.session import inspect_service as _isvc

    ctrl = handlers.controller
    ctrl.client._threads = [Thread(id=1, name="MainThread")]

    original_gate = _isvc.InspectService._gate

    def racing_gate(self):
        original_gate(self)
        # list_threads gates first and passes; thread_stack's gate then
        # sees RUNNING.
        ctrl.state.transition_to(SessionPhase.RUNNING)

    monkeypatch.setattr(_isvc.InspectService, "_gate", racing_gate)

    rsp = await handlers.action_inspect_thread([1])
    assert rsp.success is False
    assert rsp.value == "Cannot inspect threads while program is running"
    # Specifically: the race must NOT be mislabeled as a stack failure.
    assert "failed to fetch stack trace" not in rsp.value


# --- timeout_s / wait_for_stop / lock bypass (PR1 for MCP) --------------
#
# The MCP-server work needs three contracts:
#   1. Blocking actions accept a per-call timeout and surface a stable
#      "still running" sentinel instead of erroring on expiry.
#   2. `wait_for_stop` re-enters the wait without re-issuing a step (so an
#      agent that got "still running" can keep waiting cleanly).
#   3. `pause` bypasses the dispatcher lock so it can interrupt an
#      in-flight `continue` — agents have no UI side channel.


def test_parse_timeout_extracts_first_param():
    assert RpcHandlers._parse_timeout([0.5], None) == 0.5
    assert RpcHandlers._parse_timeout(["2.5"], None) == 2.5


def test_parse_timeout_falls_back_to_default_when_missing_or_bad():
    assert RpcHandlers._parse_timeout([], 7.0) == 7.0
    assert RpcHandlers._parse_timeout(["not-a-number"], 7.0) == 7.0
    assert RpcHandlers._parse_timeout([None], 7.0) == 7.0


@pytest.mark.parametrize("action_name", ["next", "step_in", "step_out", "continue"])
async def test_step_action_returns_still_running_on_timeout(handlers, action_name):
    """When the debuggee doesn't stop within `timeout_s`, the step
    action returns success with the documented sentinel so MCP / scripted
    callers can poll. Drives the real ServerEventHandler whose
    stopped_event never fires — and controller.continue_/step_* are
    no-ops here because current_thread_id is None on the fixture, so
    the action_fn() leg doesn't touch the fake DAP client."""
    fn = handlers.dispatch_table()[action_name]
    rsp = await fn([0.05])  # 50ms — small but well above scheduler noise
    assert rsp.success is True
    assert rsp.value == RpcHandlers.STILL_RUNNING_MSG


async def test_step_action_default_timeout_uses_rpc_step_wait(handlers, monkeypatch):
    """Omitting params[0] must NOT change the historical HTTP-API
    default of `RPC_STEP_WAIT`. We verify by recording what value the
    event handler's `wait_for_stop` was called with."""
    captured: list[float] = []

    async def _spy_wait(timeout: float = 30.0) -> bool:
        captured.append(timeout)
        return False  # surface the still-running path immediately

    handlers.event_handler.wait_for_stop = _spy_wait

    rsp = await handlers.action_continue([])
    assert rsp.success is True
    assert rsp.value == RpcHandlers.STILL_RUNNING_MSG
    from tdb._timeouts import RPC_STEP_WAIT

    assert captured == [RPC_STEP_WAIT]


async def test_wait_for_stop_in_dispatch_table(handlers):
    table = handlers.dispatch_table()
    assert "wait_for_stop" in table


async def test_wait_for_stop_rejected_when_terminated(handlers):
    handlers.controller.state.transition_to(SessionPhase.TERMINATED)
    rsp = await handlers.action_wait_for_stop([])
    assert rsp.success is False
    assert "terminated" in rsp.value.lower()


async def test_wait_for_stop_returns_still_running_on_timeout(handlers):
    """Pure wait — no step issued. Unlike `continue`, this action must
    NOT trip the `is_running` guard: an agent reaches this code path
    *because* the program is already running."""
    handlers.controller.state.transition_to(SessionPhase.RUNNING)
    rsp = await handlers.action_wait_for_stop([0.05])
    assert rsp.success is True
    assert rsp.value == RpcHandlers.STILL_RUNNING_MSG


async def test_wait_for_stop_does_not_call_any_step_action(handlers):
    """Regression guard: wait_for_stop must not issue next/step/continue
    on the underlying controller — its whole reason for existing is to
    re-enter the wait loop without re-issuing a step."""
    called: list[str] = []

    async def _record(name):
        async def _impl(*args, **kwargs):
            called.append(name)

        return _impl

    handlers.controller.continue_ = await _record("continue_")
    handlers.controller.step_over = await _record("step_over")
    handlers.controller.step_in = await _record("step_in")
    handlers.controller.step_out = await _record("step_out")

    handlers.controller.state.transition_to(SessionPhase.RUNNING)
    await handlers.action_wait_for_stop([0.05])
    assert called == []


async def test_dispatcher_pause_bypasses_session_lock(handlers):
    """End-to-end at the dispatcher: while `session_lock` is held (as it
    would be during an in-flight `continue`), a `pause` POST must still
    complete promptly. A non-bypassed action (`status`) must block on
    the lock — that's the control case proving the bypass is what made
    pause go through, not loose lock semantics."""
    import asyncio

    import httpx

    from tdb.server.app import create_app

    app = create_app(handlers)
    transport = httpx.ASGITransport(app=app)

    async with handlers.session_lock:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            # pause: must come back quickly despite the held lock.
            rsp = await asyncio.wait_for(
                client.post("/rpc", json={"action": "pause", "params": []}),
                timeout=1.0,
            )
            assert rsp.status_code == 200
            body = rsp.json()
            # action_pause early-returns ok when state is already STOPPED.
            assert body["success"] is True

            # status: should NOT bypass — confirms the dispatcher is
            # actually holding the lock for everything else.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    client.post("/rpc", json={"action": "status", "params": []}),
                    timeout=0.2,
                )


def test_no_lock_actions_constant_includes_pause():
    """The bypass list is a small, deliberate surface. Pin its contents
    so adding a new bypass becomes a visible decision in code review."""
    from tdb.server.app import _NO_LOCK_ACTIONS

    assert _NO_LOCK_ACTIONS == frozenset({"pause"})

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

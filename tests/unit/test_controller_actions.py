"""Unit tests for DebugController debug actions and inspection flows.

Complements the existing focused suites (pause, termination, configure
race, state authority) with coverage of stepping, continue/resume
fan-out, run-to-cursor, breakpoint CRUD gating, stack navigation,
evaluate paths, thread/process switching, stop-info fetching, and
output filtering. The DAP client is replaced by a recording fake so
each test drives the controller by hand.
"""

from __future__ import annotations

import asyncio

import pytest

from tdb.dap.client import DAPError
from tdb.dap.messages import Event, Response
from tdb.session.controller import DebugController
from tdb.session.event_bus import DebugEventHandler
from tdb.session.state import SessionPhase
from tdb.dap.types import (
    Capabilities,
    Scope,
    Source,
    SourceBreakpoint,
    StackFrame,
    Thread,
    Variable,
)
from tdb.languages.base import (
    AdapterQuirks,
    AdapterSpec,
    LanguageProfile,
    Presentation,
    ProfileCapabilities,
)


class _RecordingHandler(DebugEventHandler):
    def __init__(self) -> None:
        self.outputs: list[tuple[str, str]] = []

    def on_initialized(self): ...
    def on_stopped(self, *a, **k): ...
    def on_continued(self): ...
    def on_terminated(self): ...
    def on_exited(self, *a, **k): ...
    def on_external_terminal_started(self): ...

    def on_output(self, text, category):
        self.outputs.append((text, category))


class _FakeDAP:
    """Recording fake for DAPClient. Set `fail` to make methods raise;
    push onto `evaluate_effects` to script per-call evaluate results."""

    def __init__(self, name: str = "parent") -> None:
        self.name = name
        self.calls: list[tuple] = []
        self.threads_result = [Thread(id=1, name="MainThread")]
        self.frames_result = [
            StackFrame(id=11, name="inner", source=Source(path="/f.py"), line=4),
            StackFrame(id=12, name="outer", source=Source(path="/f.py"), line=9),
        ]
        self.scopes_result = [Scope(name="Locals", variables_reference=100)]
        self.variables_result = [Variable(name="x", value="7")]
        self.evaluate_effects: list = []  # exceptions or (result, ref) tuples
        self.source_result = "print('hi')\n"
        self.fail: set[str] = set()
        self.capabilities = Capabilities()
        self._event_handlers: dict[str, list] = {}

    def on_event(self, event_name, handler) -> None:
        self._event_handlers.setdefault(event_name, []).append(handler)

    def _hit(self, method: str, *args) -> None:
        self.calls.append((method,) + args)
        if method in self.fail:
            raise RuntimeError(f"{self.name}.{method} failed")

    def calls_to(self, method: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == method]

    async def threads(self):
        self._hit("threads")
        return self.threads_result

    async def stack_trace(self, thread_id, start_frame=0, levels=20):
        self._hit("stackTrace", thread_id)
        return self.frames_result

    async def scopes(self, frame_id):
        self._hit("scopes", frame_id)
        return self.scopes_result

    async def variables(self, ref, start=0, count=0):
        self._hit("variables", ref)
        return self.variables_result

    async def evaluate(self, expression, frame_id=None, context="repl"):
        self._hit("evaluate", expression, frame_id)
        if self.evaluate_effects:
            effect = self.evaluate_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            return effect
        return ("42", 0)

    async def source(self, source_reference, source=None):
        self._hit("source", source_reference)
        return self.source_result

    async def next(self, thread_id):
        self._hit("next", thread_id)

    async def step_in(self, thread_id):
        self._hit("stepIn", thread_id)

    async def step_out(self, thread_id):
        self._hit("stepOut", thread_id)

    async def pause(self, thread_id):
        self._hit("pause", thread_id)

    async def continue_nowait(self, thread_id):
        self._hit("continue", thread_id)

    async def set_breakpoints(self, source_path, breakpoints):
        self._hit("setBreakpoints", source_path, tuple(bp.line for bp in breakpoints))
        return []

    async def set_exception_breakpoints(self, filters):
        self._hit("setExceptionBreakpoints", tuple(filters))

    async def configuration_done(self):
        self._hit("configurationDone")

    async def disconnect(self, terminate=True):
        self._hit("disconnect", terminate)

    async def stop(self):
        self._hit("stop")


def _make(
    thread_id: int | None = 1,
    with_frames: bool = True,
    profile=None,
) -> tuple[DebugController, _FakeDAP, _RecordingHandler]:
    """Controller stopped at a breakpoint with a two-frame stack."""
    handler = _RecordingHandler()
    ctrl = DebugController(handler, profile=profile)
    fake = _FakeDAP()
    ctrl.client = fake
    ctrl._active_client = fake
    ctrl.state.enter_stop(thread_id=thread_id, reason="breakpoint")
    ctrl.state.current_thread_id = thread_id  # None stays None
    if with_frames:
        ctrl.state.set_stack(list(fake.frames_result))
    return ctrl, fake, handler


async def _drain_bg(ctrl: DebugController) -> None:
    """Let the controller's fire-and-forget tasks run to completion."""
    for _ in range(10):
        if not ctrl._bg_tasks:
            return
        await asyncio.sleep(0.01)


# --- Stepping -----------------------------------------------------------


@pytest.mark.parametrize(
    "action,dap_call",
    [("step_over", "next"), ("step_in", "stepIn"), ("step_out", "stepOut")],
)
async def test_steps_transition_running_and_issue_dap_step(action, dap_call):
    ctrl, fake, _ = _make()
    await getattr(ctrl, action)()
    assert fake.calls_to(dap_call) == [(dap_call, 1)]
    assert ctrl.state.phase is SessionPhase.RUNNING
    assert ctrl.state.stack_frames == []


@pytest.mark.parametrize("action", ["step_over", "step_in", "step_out", "continue_"])
async def test_actions_are_noops_without_thread(action):
    ctrl, fake, _ = _make(thread_id=None)
    await getattr(ctrl, action)()
    assert fake.calls == []
    assert ctrl.state.phase is SessionPhase.STOPPED


async def test_issue_statement_step_routes_by_kind():
    ctrl, fake, _ = _make()
    await ctrl._issue_statement_step("next")
    await ctrl._issue_statement_step("stepIn")
    assert fake.calls == [("next", 1), ("stepIn", 1)]


async def test_issue_statement_step_noop_without_thread():
    ctrl, fake, _ = _make(thread_id=None)
    await ctrl._issue_statement_step("next")
    assert fake.calls == []


def test_step_mode_property_round_trip():
    ctrl, _, _ = _make()
    assert ctrl.step_mode == "statement"
    ctrl.step_mode = "line"
    assert ctrl.step_mode == "line"
    ctrl.set_step_mode("statement")
    assert ctrl.step_mode == "statement"
    with pytest.raises(ValueError):
        ctrl.set_step_mode("word")


# --- Continue / resume fan-out ------------------------------------------


async def test_continue_resumes_active_client_and_children():
    ctrl, fake, _ = _make()
    child = _FakeDAP("child")
    ctrl._child_clients[100] = child
    await ctrl.continue_()
    await _drain_bg(ctrl)
    assert fake.calls_to("continue") == [("continue", 1)]
    assert ctrl.state.phase is SessionPhase.RUNNING
    # Child resumed best-effort in the background: threads then continue.
    assert child.calls_to("threads") and child.calls_to("continue")


async def test_continue_from_child_also_resumes_parent():
    ctrl, fake, _ = _make()
    child = _FakeDAP("child")
    ctrl._child_clients[100] = child
    ctrl._active_client = child
    await ctrl.continue_()
    await _drain_bg(ctrl)
    # Direct continue went to the child (it was active) ...
    assert child.calls_to("continue") == [("continue", 1)]
    # ... the parent was resumed via the background path ...
    assert fake.calls_to("threads") and fake.calls_to("continue")
    # ... and the active client reset to the parent.
    assert ctrl.active_client is fake


async def test_continue_swallows_direct_continue_failure():
    ctrl, fake, _ = _make()
    fake.fail.add("continue")
    await ctrl.continue_()  # must not raise
    assert ctrl.state.phase is SessionPhase.RUNNING


async def test_resume_client_gives_up_when_threads_unavailable():
    ctrl, _, _ = _make()
    silent = _FakeDAP("silent")
    silent.fail.add("threads")
    await ctrl._resume_client(silent)
    assert silent.calls_to("continue") == []
    empty = _FakeDAP("empty")
    empty.threads_result = []
    await ctrl._resume_client(empty)
    assert empty.calls_to("continue") == []


# --- Run to cursor --------------------------------------------------------


async def test_run_to_cursor_sets_temp_breakpoint_everywhere_and_continues():
    ctrl, fake, _ = _make()
    child = _FakeDAP("child")
    ctrl._child_clients[100] = child
    await ctrl.run_to_cursor("/f.py", 30)
    await _drain_bg(ctrl)
    assert fake.calls_to("setBreakpoints") == [("setBreakpoints", "/f.py", (30,))]
    assert child.calls_to("setBreakpoints") == [("setBreakpoints", "/f.py", (30,))]
    assert fake.calls_to("continue")  # continued after arming
    # Simulate the stop at the cursor, then cleanup removes the temp bp.
    ctrl.state.enter_stop(thread_id=1, reason="breakpoint")
    await ctrl.cleanup_run_to_cursor()
    assert fake.calls_to("setBreakpoints")[-1] == ("setBreakpoints", "/f.py", ())
    assert child.calls_to("setBreakpoints")[-1] == ("setBreakpoints", "/f.py", ())
    assert ctrl.state.breakpoints["/f.py"] == []


async def test_run_to_cursor_preserves_existing_breakpoint():
    ctrl, fake, _ = _make()
    ctrl.state.breakpoints["/f.py"] = [SourceBreakpoint(line=30)]
    await ctrl.run_to_cursor("/f.py", 30)
    ctrl.state.enter_stop(thread_id=1, reason="breakpoint")
    await ctrl.cleanup_run_to_cursor()
    # The user's own breakpoint must survive the round trip.
    assert [bp.line for bp in ctrl.state.breakpoints["/f.py"]] == [30]
    assert fake.calls_to("setBreakpoints") == []  # nothing added or removed


async def test_cleanup_run_to_cursor_noop_when_nothing_pending():
    ctrl, fake, _ = _make()
    await ctrl.cleanup_run_to_cursor()  # must not raise
    assert fake.calls == []


async def test_run_to_cursor_survives_breakpoint_send_failure():
    ctrl, fake, _ = _make()
    fake.fail.add("setBreakpoints")
    await ctrl.run_to_cursor("/f.py", 30)  # must not raise
    assert fake.calls_to("continue")


# --- Breakpoint CRUD gating ----------------------------------------------


async def test_toggle_breakpoint_adds_then_removes():
    ctrl, fake, _ = _make()
    await ctrl.toggle_breakpoint("/f.py", 5)
    assert [bp.line for bp in ctrl.state.breakpoints["/f.py"]] == [5]
    await ctrl.toggle_breakpoint("/f.py", 5)
    assert ctrl.state.breakpoints["/f.py"] == []
    assert [c[2] for c in fake.calls_to("setBreakpoints")] == [(5,), ()]


async def test_breakpoint_changes_not_sent_before_session_ready():
    handler = _RecordingHandler()
    ctrl = DebugController(handler)
    fake = _FakeDAP()
    ctrl.client = fake  # phase NOT_STARTED: is_ready is False
    await ctrl.toggle_breakpoint("/f.py", 5)
    await ctrl.add_breakpoint("/f.py", 6)
    assert [bp.line for bp in ctrl.state.breakpoints["/f.py"]] == [5, 6]
    assert fake.calls == []  # state only, nothing on the wire


async def test_add_breakpoint_updates_condition_in_place():
    ctrl, fake, _ = _make()
    await ctrl.add_breakpoint("/f.py", 5, condition="x > 1")
    await ctrl.add_breakpoint("/f.py", 5, condition="x > 2", hit_condition="3")
    (bp,) = ctrl.state.breakpoints["/f.py"]
    assert (bp.condition, bp.hit_condition) == ("x > 2", "3")


async def test_remove_breakpoint_noop_when_absent():
    ctrl, fake, _ = _make()
    await ctrl.remove_breakpoint("/f.py", 99)
    assert fake.calls == []


async def test_toggle_breakpoint_enabled_filters_disabled_from_wire():
    ctrl, fake, _ = _make()
    await ctrl.add_breakpoint("/f.py", 5)
    await ctrl.toggle_breakpoint_enabled("/f.py", 5)
    (bp,) = ctrl.state.breakpoints["/f.py"]
    assert bp.enabled is False
    assert fake.calls_to("setBreakpoints")[-1] == ("setBreakpoints", "/f.py", ())
    await ctrl.toggle_breakpoint_enabled("/f.py", 5)
    assert fake.calls_to("setBreakpoints")[-1] == ("setBreakpoints", "/f.py", (5,))


async def test_toggle_breakpoint_enabled_unknown_line_is_noop():
    ctrl, fake, _ = _make()
    await ctrl.toggle_breakpoint_enabled("/f.py", 99)
    assert fake.calls == []


async def test_set_breakpoint_condition_updates_and_resends():
    ctrl, fake, _ = _make()
    await ctrl.add_breakpoint("/f.py", 5)
    await ctrl.set_breakpoint_condition("/f.py", 5, "n == 10", "2")
    (bp,) = ctrl.state.breakpoints["/f.py"]
    assert (bp.condition, bp.hit_condition) == ("n == 10", "2")
    assert len(fake.calls_to("setBreakpoints")) == 2


async def test_disable_enable_all_breakpoints():
    ctrl, fake, _ = _make()
    await ctrl.add_breakpoint("/f.py", 5)
    await ctrl.disable_all_breakpoints()
    assert ctrl.state.breakpoints_disabled is True
    assert fake.calls_to("setBreakpoints")[-1] == ("setBreakpoints", "/f.py", ())
    await ctrl.enable_all_breakpoints()
    assert ctrl.state.breakpoints_disabled is False
    assert fake.calls_to("setBreakpoints")[-1] == ("setBreakpoints", "/f.py", (5,))


async def test_clear_all_breakpoints_wipes_state_and_wire():
    ctrl, fake, _ = _make()
    await ctrl.add_breakpoint("/f.py", 5)
    await ctrl.clear_all_breakpoints()
    assert ctrl.state.breakpoints == {}
    assert fake.calls_to("setBreakpoints")[-1] == ("setBreakpoints", "/f.py", ())


# --- Stack navigation ------------------------------------------------------


async def test_navigate_stack_up_and_down():
    ctrl, fake, _ = _make()  # frames [11 inner, 12 outer], current 11
    assert await ctrl.navigate_stack(up=True) is True
    assert ctrl.state.current_frame_id == 12
    assert fake.calls_to("scopes") == [("scopes", 12)]
    assert await ctrl.navigate_stack(up=True) is False  # top of stack
    assert await ctrl.navigate_stack(up=False) is True
    assert ctrl.state.current_frame_id == 11
    assert await ctrl.navigate_stack(up=False) is False  # bottom


async def test_navigate_stack_requires_frames_and_known_current():
    ctrl, _, _ = _make(with_frames=False)
    assert await ctrl.navigate_stack(up=True) is False
    ctrl2, _, _ = _make()
    ctrl2.state.current_frame_id = 999  # not in the stack
    assert await ctrl2.navigate_stack(up=True) is False


async def test_navigate_stack_skips_fetch_after_termination():
    ctrl, fake, _ = _make()
    ctrl.state.transition_to(SessionPhase.TERMINATED)
    assert await ctrl.navigate_stack(up=True) is True
    assert fake.calls_to("scopes") == []


# --- Evaluate paths ----------------------------------------------------------


async def test_evaluate_uses_current_frame():
    ctrl, fake, _ = _make()
    assert await ctrl.evaluate("x + 1") == "42"
    assert fake.calls_to("evaluate") == [("evaluate", "x + 1", 11)]


async def test_evaluate_returns_error_text_instead_of_raising():
    ctrl, fake, _ = _make()
    fake.evaluate_effects.append(RuntimeError("kaboom"))
    assert await ctrl.evaluate("x") == "kaboom"


async def test_synthetic_frames_fall_back_to_live_top_frame():
    ctrl, fake, _ = _make()
    ctrl.state.set_stack(list(fake.frames_result), synthetic=True)
    frame_id = await ctrl.resolve_evaluate_frame_id(fake)
    assert frame_id == 11  # top of the freshly fetched real stack
    assert fake.calls_to("stackTrace")  # went to the wire, not the fakes


async def test_synthetic_frames_without_thread_resolve_to_none():
    ctrl, fake, _ = _make()
    ctrl.state.set_stack(list(fake.frames_result), synthetic=True)
    ctrl.state.current_thread_id = None
    assert await ctrl.resolve_evaluate_frame_id(fake) is None
    fake.fail.add("stackTrace")
    ctrl.state.current_thread_id = 1
    assert await ctrl.resolve_evaluate_frame_id(fake) is None


async def test_evaluate_on_parent_from_child_uses_parent_stack():
    ctrl, fake, _ = _make()
    child = _FakeDAP("child")
    ctrl._child_clients[100] = child
    ctrl._active_client = child
    assert await ctrl.evaluate_on_parent("q") == "42"
    # Scope context came from the parent, not the active child.
    assert fake.calls_to("stackTrace") and fake.calls_to("evaluate")
    assert child.calls == []


async def test_evaluate_on_parent_retries_when_parent_not_stopped(monkeypatch):
    ctrl, fake, _ = _make()

    async def instant_sleep(_s):
        pass

    monkeypatch.setattr("tdb.session.controller.asyncio.sleep", instant_sleep)
    fake.evaluate_effects.append(DAPError("evaluate", "Unable to find thread 1"))
    fake.evaluate_effects.append(("7", 0))
    assert await ctrl.evaluate_on_parent("q") == "7"
    assert len(fake.calls_to("evaluate")) == 2


async def test_evaluate_on_parent_returns_other_dap_error_text():
    ctrl, fake, _ = _make()
    fake.evaluate_effects.append(DAPError("evaluate", "SyntaxError"))
    assert "SyntaxError" in await ctrl.evaluate_on_parent("q")
    assert len(fake.calls_to("evaluate")) == 1  # no retry for other errors


# --- Frame / thread / process selection --------------------------------------


async def test_select_frame_fetches_scopes_for_live_frames():
    ctrl, fake, _ = _make()
    await ctrl.select_frame(12)
    assert ctrl.state.current_frame_id == 12
    assert fake.calls_to("scopes") == [("scopes", 12)]
    assert ctrl.state.variables[100] == fake.variables_result


async def test_select_frame_post_mortem_uses_snapshot_scopes():
    ctrl, fake, _ = _make()
    ctrl.state.transition_to(SessionPhase.POST_MORTEM)
    snapshot_scopes = [Scope(name="Locals", variables_reference=500)]
    ctrl.state.frame_scopes = {12: snapshot_scopes}
    await ctrl.select_frame(12)
    assert ctrl.state.scopes == snapshot_scopes
    assert fake.calls == []  # no DAP traffic in post-mortem


async def test_select_frame_synthetic_skips_fetch():
    ctrl, fake, _ = _make()
    ctrl.state.set_stack(list(fake.frames_result), synthetic=True)
    await ctrl.select_frame(12)
    assert fake.calls == []


async def test_switch_active_thread_repoints_views_and_stepping():
    ctrl, fake, _ = _make()
    child = _FakeDAP("child")
    ctrl._active_client = child
    await ctrl.switch_active_thread(7)
    assert ctrl.active_client is fake  # back on the parent
    assert ctrl.state.current_thread_id == 7
    assert fake.calls_to("stackTrace") == [("stackTrace", 7)]
    assert ctrl.state.stack_frames == fake.frames_result
    assert fake.calls_to("scopes")  # top frame's scopes fetched


async def test_switch_active_thread_failure_leaves_no_raise():
    ctrl, fake, _ = _make()
    fake.fail.add("stackTrace")
    await ctrl.switch_active_thread(7)  # logged, not raised


async def test_switch_active_process_targets_child_main_thread():
    ctrl, fake, _ = _make()
    child = _FakeDAP("child")
    child.threads_result = [Thread(id=42, name="MainThread")]
    ctrl._child_clients[100] = child
    await ctrl.switch_active_process(100)
    assert ctrl.active_client is child
    assert ctrl.state.current_thread_id == 42
    assert child.calls_to("stackTrace") == [("stackTrace", 42)]


async def test_switch_active_process_unknown_pid_is_noop():
    ctrl, fake, _ = _make()
    await ctrl.switch_active_process(12345)
    assert ctrl.active_client is fake


async def test_switch_active_process_without_threads_keeps_thread_id():
    ctrl, fake, _ = _make()
    child = _FakeDAP("child")
    child.threads_result = []
    ctrl._child_clients[100] = child
    await ctrl.switch_active_process(100)
    assert ctrl.state.current_thread_id == 1  # unchanged


# --- Stop-info fetching --------------------------------------------------


async def test_fetch_stop_info_populates_threads_stack_and_variables():
    ctrl, fake, _ = _make(with_frames=False)
    await ctrl.fetch_stop_info()
    assert ctrl.state.threads == fake.threads_result
    assert ctrl.state.stack_frames == fake.frames_result
    assert ctrl.state.scopes == fake.scopes_result
    assert ctrl.state.variables[100] == fake.variables_result


async def test_fetch_stop_info_survives_threads_failure():
    ctrl, fake, _ = _make(with_frames=False)
    fake.fail.add("threads")
    await ctrl.fetch_stop_info()
    assert ctrl.state.stack_frames == fake.frames_result  # stack still fetched


async def test_fetch_stop_info_stack_failure_clears_frames():
    ctrl, fake, _ = _make()
    fake.fail.add("stackTrace")
    await ctrl.fetch_stop_info()
    assert ctrl.state.stack_frames == []
    assert fake.calls_to("scopes") == []


async def test_fetch_source_content_asks_active_client():
    ctrl, fake, _ = _make()
    src = Source(path="/remote/f.py")
    assert await ctrl.fetch_source_content(src) == fake.source_result
    ctrl.state.transition_to(SessionPhase.POST_MORTEM)
    assert await ctrl.fetch_source_content(src) == ""


# --- Output filtering and event side-effects ------------------------------


def test_output_telemetry_is_dropped():
    ctrl, _, handler = _make()
    ctrl._on_output(
        Event(seq=1, event="output", body={"output": "ptvsd", "category": "telemetry"})
    )
    ctrl._on_output(
        Event(seq=2, event="output", body={"output": "hello\n", "category": "stdout"})
    )
    assert handler.outputs == [("hello\n", "stdout")]


def test_output_suppressed_in_external_terminal_mode():
    ctrl, _, handler = _make()
    ctrl._terminal = "xterm"
    ctrl._on_output(
        Event(seq=1, event="output", body={"output": "to tty\n", "category": "stdout"})
    )
    ctrl._on_output(
        Event(
            seq=2,
            event="output",
            body={"output": "adapter says\n", "category": "console"},
        )
    )
    assert handler.outputs == [("adapter says\n", "console")]


async def test_pause_parent_is_best_effort():
    ctrl, fake, _ = _make()
    await ctrl._pause_parent()
    assert fake.calls_to("pause") == [("pause", 1)]
    fake.fail.add("threads")
    await ctrl._pause_parent()  # must not raise


# --- stop() and do_configure() --------------------------------------------


async def test_stop_terminates_owned_debuggee():
    ctrl, fake, _ = _make()
    await ctrl.stop()
    assert fake.calls_to("disconnect") == [("disconnect", True)]
    assert fake.calls_to("stop") == [("stop",)]
    assert ctrl.state.phase is SessionPhase.TERMINATED


async def test_stop_detaches_without_terminating_remote_debuggee():
    ctrl, fake, _ = _make()
    ctrl._is_remote_attach = True
    await ctrl.stop()
    assert fake.calls_to("disconnect") == [("disconnect", False)]


async def test_stop_survives_disconnect_failure():
    ctrl, fake, _ = _make()
    fake.fail.add("disconnect")
    await ctrl.stop()
    assert ctrl.state.phase is SessionPhase.TERMINATED


def _resolved_launch_future(success: bool = True) -> asyncio.Future:
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    fut.set_result(
        Response(
            seq=1,
            request_seq=1,
            command="launch",
            success=success,
            message=None if success else "bad interpreter",
        )
    )
    return fut


async def test_do_configure_sends_breakpoints_then_configuration_done():
    handler = _RecordingHandler()
    ctrl = DebugController(handler)
    fake = _FakeDAP()
    ctrl.client = fake
    ctrl.state.breakpoints["/f.py"] = [
        SourceBreakpoint(line=3),
        SourceBreakpoint(line=9, enabled=False),
    ]
    ctrl._launch_future = _resolved_launch_future()
    await ctrl.do_configure()
    assert fake.calls_to("setExceptionBreakpoints") == [
        ("setExceptionBreakpoints", ("userUnhandled",))
    ]
    # Disabled bp filtered out; configurationDone after breakpoints.
    assert fake.calls_to("setBreakpoints") == [("setBreakpoints", "/f.py", (3,))]
    order = [c[0] for c in fake.calls]
    assert order.index("setBreakpoints") < order.index("configurationDone")
    assert ctrl.state.phase is SessionPhase.RUNNING


async def test_do_configure_prearms_pause_for_remote_attach():
    handler = _RecordingHandler()
    ctrl = DebugController(handler)
    fake = _FakeDAP()
    ctrl.client = fake
    ctrl._is_remote_attach = True
    ctrl._launch_future = _resolved_launch_future()
    await ctrl.do_configure()
    order = [c[0] for c in fake.calls]
    # Pause must be queued BEFORE configurationDone or a short debuggee
    # races to completion (see do_configure's comment).
    assert order.index("pause") < order.index("configurationDone")


async def test_do_configure_skips_prearm_pause_when_already_stopped():
    handler = _RecordingHandler()
    ctrl = DebugController(handler)
    fake = _FakeDAP()
    ctrl.client = fake
    ctrl._is_remote_attach = True
    ctrl.state.enter_stop(thread_id=1, reason="breakpoint")  # tdb.breakpoint() hook
    ctrl._launch_future = _resolved_launch_future()
    await ctrl.do_configure()
    assert fake.calls_to("pause") == []
    assert ctrl.state.phase is SessionPhase.STOPPED  # not clobbered


async def test_do_configure_raises_on_failed_launch():
    handler = _RecordingHandler()
    ctrl = DebugController(handler)
    fake = _FakeDAP()
    ctrl.client = fake
    ctrl._launch_future = _resolved_launch_future(success=False)
    with pytest.raises(Exception, match="Launch failed: bad interpreter"):
        await ctrl.do_configure()


# --- LanguageProfile threading ---------------------------------------------


class _NullSpec(AdapterSpec):
    id = "null"

    def command(self):
        return ["true"]

    def launch_body(self, **kw):
        return {}

    def attach_body(self, **kw):
        return {}

    def pick_exception_filters(self, caps):
        return []


def _bare_profile(**cap_kwargs) -> LanguageProfile:
    return LanguageProfile(
        id="bare",
        display_name="Bare",
        adapter=_NullSpec(),
        presentation=Presentation(),
        capabilities=ProfileCapabilities(**cap_kwargs),
    )


def test_default_profile_is_python():
    ctrl, _dap, _handler = _make()
    assert ctrl.profile.id == "python"


async def test_do_configure_skips_exception_bps_when_no_filters():
    ctrl, dap, _handler = _make(profile=_bare_profile())
    ctrl._launch_future = _resolved_launch_future()
    await ctrl.do_configure()
    assert dap.calls_to("setExceptionBreakpoints") == []


async def test_do_configure_uses_profile_filters():
    ctrl, dap, _handler = _make()  # python profile
    ctrl._launch_future = _resolved_launch_future()
    await ctrl.do_configure()
    assert dap.calls_to("setExceptionBreakpoints")[0] == (
        "setExceptionBreakpoints",
        ("userUnhandled",),
    )


def test_children_not_registered_without_strategy():
    ctrl, dap, _handler = _make(profile=_bare_profile())
    ctrl._setup_event_handlers()
    assert "debugpyAttach" not in ctrl.client._event_handlers


def test_children_registered_for_python():
    ctrl, dap, _handler = _make()
    ctrl._setup_event_handlers()
    assert "debugpyAttach" in ctrl.client._event_handlers

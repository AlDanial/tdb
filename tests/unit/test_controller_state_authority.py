"""The controller is the single state authority.

Every DAP event handler on `DebugController` (`_on_stopped`,
`_on_continued`, `_on_terminated`, `_on_exited`) updates `state` fields
synchronously *before* delegating to the per-app event handler. This
test module pins that contract for all four events so future consumers
don't drift back into duplicating the assignments.

Sibling tests in test_controller_termination.py cover the original
fix that motivated this design (terminated/exited propagation). The
tests below extend the same proof to stopped/continued.
"""

from __future__ import annotations

from tdb.dap.client import DAPClient
from tdb.dap.messages import Event
from tdb.dap.types import StackFrame, Source
from tdb.session.controller import DebugController
from tdb.session.event_bus import DebugEventHandler
from tdb.session.state import SessionPhase


class _RecordingHandler(DebugEventHandler):
    def __init__(self):
        self.events: list[tuple[str, tuple, dict]] = []

    def on_initialized(self):
        self.events.append(("initialized", (), {}))

    def on_stopped(self, thread_id, reason, description=None, text=None):
        self.events.append(
            (
                "stopped",
                (thread_id, reason, description, text),
                {},
            )
        )

    def on_continued(self):
        self.events.append(("continued", (), {}))

    def on_terminated(self):
        self.events.append(("terminated", (), {}))

    def on_exited(self, exit_code):
        self.events.append(("exited", (exit_code,), {}))

    def on_output(self, *a, **k):
        self.events.append(("output", a, k))

    def on_external_terminal_started(self):
        self.events.append(("ext_term", (), {}))


def _stopped_event(
    *, thread_id=1, reason="breakpoint", description=None, text=None
) -> Event:
    body = {"reason": reason, "threadId": thread_id}
    if description is not None:
        body["description"] = description
    if text is not None:
        body["text"] = text
    return Event(seq=1, event="stopped", body=body)


def _continued_event() -> Event:
    return Event(seq=2, event="continued", body={})


# --- _on_stopped ---------------------------------------------------------


def test_on_stopped_sets_is_running_false():
    h = _RecordingHandler()
    ctrl = DebugController(h)
    ctrl.state.transition_to(SessionPhase.RUNNING)
    ctrl._on_stopped(_stopped_event())
    assert ctrl.state.is_running is False


def test_on_stopped_sets_stop_reason():
    h = _RecordingHandler()
    ctrl = DebugController(h)
    ctrl._on_stopped(_stopped_event(reason="step"))
    assert ctrl.state.stop_reason == "step"


def test_on_stopped_sets_current_thread_id_when_provided():
    h = _RecordingHandler()
    ctrl = DebugController(h)
    ctrl._on_stopped(_stopped_event(thread_id=7))
    assert ctrl.state.current_thread_id == 7


def test_on_stopped_preserves_thread_id_when_event_omits_it():
    """Pause events sometimes arrive without a thread_id; we must not
    clobber the previously-known thread."""
    h = _RecordingHandler()
    ctrl = DebugController(h)
    ctrl.state.current_thread_id = 99
    ctrl._on_stopped(Event(seq=1, event="stopped", body={"reason": "pause"}))
    assert ctrl.state.current_thread_id == 99


def test_on_stopped_defaults_reason_to_unknown():
    h = _RecordingHandler()
    ctrl = DebugController(h)
    ctrl._on_stopped(Event(seq=1, event="stopped", body={"threadId": 1}))
    assert ctrl.state.stop_reason == "unknown"


def test_on_stopped_resets_active_client_to_parent():
    """After a parent-side stop, the active client must point at the
    parent so subsequent step/evaluate go to the right session."""
    h = _RecordingHandler()
    ctrl = DebugController(h)
    other_client = DAPClient()
    ctrl._active_client = other_client  # pretend a child was active
    ctrl._on_stopped(_stopped_event())
    assert ctrl._active_client is ctrl.client


def test_on_stopped_propagates_to_event_handler():
    h = _RecordingHandler()
    ctrl = DebugController(h)
    ctrl._on_stopped(
        _stopped_event(
            thread_id=3,
            reason="exception",
            description="ValueError",
            text="bad input",
        )
    )
    assert h.events == [
        (
            "stopped",
            (3, "exception", "ValueError", "bad input"),
            {},
        )
    ]


def test_on_stopped_state_is_set_before_handler_dispatch():
    """Handlers downstream may immediately read state; the contract is
    that state mutation happens BEFORE the dispatch."""
    h = _RecordingHandler()
    ctrl = DebugController(h)

    observed: dict = {}

    class _Probe(DebugEventHandler):
        def on_initialized(self): ...
        def on_stopped(self, *a, **k):
            observed["is_running"] = ctrl.state.is_running
            observed["stop_reason"] = ctrl.state.stop_reason
            observed["thread_id"] = ctrl.state.current_thread_id

        def on_continued(self): ...
        def on_terminated(self): ...
        def on_exited(self, *a, **k): ...
        def on_output(self, *a, **k): ...
        def on_external_terminal_started(self): ...

    ctrl.event_handler = _Probe()
    ctrl.state.transition_to(SessionPhase.RUNNING)
    ctrl._on_stopped(_stopped_event(thread_id=5, reason="breakpoint"))
    assert observed == {
        "is_running": False,
        "stop_reason": "breakpoint",
        "thread_id": 5,
    }


# --- _on_continued -------------------------------------------------------


def test_on_continued_sets_is_running_true():
    h = _RecordingHandler()
    ctrl = DebugController(h)
    ctrl.state.transition_to(SessionPhase.STOPPED)
    ctrl._on_continued(_continued_event())
    assert ctrl.state.is_running is True


def test_on_continued_clears_frame_data():
    """Frame data is stale once the program resumes; the controller
    clears it so consumers don't display zombie frames."""
    h = _RecordingHandler()
    ctrl = DebugController(h)
    ctrl.state.stack_frames = [
        StackFrame(id=1, name="f", source=Source(path="/x.py"), line=1),
    ]
    ctrl.state.scopes = [object()]  # type: ignore[list-item]
    ctrl.state.variables[42] = []
    ctrl.state.current_frame_id = 1

    ctrl._on_continued(_continued_event())

    assert ctrl.state.stack_frames == []
    assert ctrl.state.scopes == []
    assert ctrl.state.variables == {}
    assert ctrl.state.current_frame_id is None


def test_on_continued_propagates_to_event_handler():
    h = _RecordingHandler()
    ctrl = DebugController(h)
    ctrl._on_continued(_continued_event())
    assert h.events == [("continued", (), {})]


# --- Step actions don't need a manual sync from the consumer ----------


async def test_step_then_stopped_event_leaves_state_consistent():
    """Reproduces the headless RPC flow that used to require
    `_sync_state_from_stop`: after wait_for_stop returns, state must
    already reflect the stop.
    """
    h = _RecordingHandler()
    ctrl = DebugController(h)
    ctrl.state.current_thread_id = 1

    # Simulate the action_fn side: controller.step_over flips is_running=True.
    # We can't actually call step_over without a connected client, so we
    # mimic its state mutation directly.
    ctrl.state.transition_to(SessionPhase.RUNNING)

    # Now imagine DAP fires `stopped` on the read loop.
    ctrl._on_stopped(_stopped_event(thread_id=1, reason="step"))

    # By the time the (hypothetical) wait_for_stop returns, state is
    # already consistent — no manual sync needed.
    assert ctrl.state.is_running is False
    assert ctrl.state.stop_reason == "step"
    assert ctrl.state.current_thread_id == 1


def test_no_sync_helper_remains_on_RpcHandlers():
    """_sync_state_from_stop was deleted as part of #4. Asserting on its
    absence keeps anyone from re-introducing the duplicate path."""
    from tdb.server.handlers import RpcHandlers

    assert not hasattr(RpcHandlers, "_sync_state_from_stop")

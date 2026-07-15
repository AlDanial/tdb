"""Unit tests for tdb.session.statement_stepper (StatementStepper).

The stepper is exercised through its public surface only: `begin` to
arm a statement step, then `maybe_continue` after each simulated stop.
Real source files (tmp_path) drive real AST step units; the DAP side is
replaced by recording callbacks, mirroring the controller's wiring.
"""

from __future__ import annotations

import pytest

from tdb.dap.types import Source, StackFrame
from tdb.session.state import DebugState, SessionPhase
from tdb.session.statement_stepper import StatementStepper
from tdb.source_analysis import compute_step_units

MULTILINE_SRC = """\
x = foo(
    1,
    2,
)
y = 2
"""
# Step units: the assignment spans lines 1-4, `y = 2` is (5, 5).


def _frame(path: str, line: int, fid: int = 1, name: str = "f") -> StackFrame:
    return StackFrame(id=fid, name=name, source=Source(path=path), line=line)


def _stopped_state(
    path: str,
    line: int,
    *,
    reason: str = "step",
    thread_id: int = 1,
) -> DebugState:
    state = DebugState()
    state.enter_stop(thread_id=thread_id, reason=reason)
    state.set_stack([_frame(path, line)])
    return state


def _restop(
    state: DebugState,
    path: str,
    line: int,
    *,
    reason: str = "step",
) -> None:
    """Simulate the next DAP stopped event: re-enter STOPPED with a
    fresh one-frame stack (maybe_continue cleared the previous one)."""
    state.enter_stop(thread_id=state.current_thread_id or 1, reason=reason)
    state.set_stack([_frame(path, line)])


class _Harness:
    """Stepper + recording callbacks, standing in for the controller."""

    def __init__(self, state: DebugState) -> None:
        self.state = state
        self.issued: list[str] = []
        self.ensure_calls = 0
        self.ensure_error: Exception | None = None
        self.ensure_frames: list[StackFrame] | None = None
        self.stepper = StatementStepper(
            state,
            self._issue,
            self._ensure,
            compute_units=compute_step_units,
        )

    async def _issue(self, kind: str) -> None:
        self.issued.append(kind)

    async def _ensure(self) -> None:
        self.ensure_calls += 1
        if self.ensure_error is not None:
            raise self.ensure_error
        if self.ensure_frames is not None:
            self.state.set_stack(self.ensure_frames)


@pytest.fixture
def prog(tmp_path) -> str:
    path = tmp_path / "prog.py"
    path.write_text(MULTILINE_SRC)
    return str(path)


# --- set_mode / cancel ------------------------------------------------


def test_set_mode_rejects_unknown_mode():
    h = _Harness(DebugState())
    with pytest.raises(ValueError):
        h.stepper.set_mode("word")


async def test_set_mode_clears_pending_step(prog):
    h = _Harness(_stopped_state(prog, 1))
    await h.stepper.begin("next")
    h.stepper.set_mode("line")
    _restop(h.state, prog, 2)
    assert await h.stepper.maybe_continue() is False
    assert h.issued == []


async def test_cancel_clears_pending_step(prog):
    h = _Harness(_stopped_state(prog, 1))
    await h.stepper.begin("next")
    h.stepper.cancel()
    _restop(h.state, prog, 2)
    assert await h.stepper.maybe_continue() is False
    assert h.issued == []


# --- begin ------------------------------------------------------------


async def test_line_mode_never_arms(prog):
    h = _Harness(_stopped_state(prog, 1))
    h.stepper.set_mode("line")
    await h.stepper.begin("next")
    _restop(h.state, prog, 2)
    assert await h.stepper.maybe_continue() is False


async def test_begin_loads_stack_when_empty(prog):
    state = DebugState()
    state.enter_stop(thread_id=1, reason="step")  # stopped, but no frames yet
    h = _Harness(state)
    h.ensure_frames = [_frame(prog, 1)]
    await h.stepper.begin("next")
    assert h.ensure_calls == 1
    _restop(h.state, prog, 2)
    assert await h.stepper.maybe_continue() is True
    assert h.issued == ["next"]


async def test_begin_skips_stack_load_when_frames_present(prog):
    h = _Harness(_stopped_state(prog, 1))
    await h.stepper.begin("next")
    assert h.ensure_calls == 0


async def test_begin_survives_stack_load_failure(prog):
    state = DebugState()
    state.enter_stop(thread_id=1, reason="step")
    h = _Harness(state)
    h.ensure_error = RuntimeError("boom")
    await h.stepper.begin("next")  # must not raise
    h.state.set_stack([_frame(prog, 2)])
    assert await h.stepper.maybe_continue() is False
    assert h.issued == []


async def test_begin_without_source_does_not_arm(prog):
    state = DebugState()
    state.enter_stop(thread_id=1, reason="step")
    state.set_stack([StackFrame(id=1, name="<builtin>", source=None, line=7)])
    h = _Harness(state)
    await h.stepper.begin("next")
    _restop(h.state, prog, 2)
    assert await h.stepper.maybe_continue() is False


async def test_begin_on_unparseable_file_does_not_arm(tmp_path):
    path = tmp_path / "broken.py"
    path.write_text("def broken(:\n    pass\n")
    h = _Harness(_stopped_state(str(path), 1))
    await h.stepper.begin("next")
    _restop(h.state, str(path), 2)
    assert await h.stepper.maybe_continue() is False


async def test_begin_on_unreadable_file_does_not_arm(tmp_path):
    missing = str(tmp_path / "gone.py")
    h = _Harness(_stopped_state(missing, 1))
    await h.stepper.begin("next")
    _restop(h.state, missing, 1)
    assert await h.stepper.maybe_continue() is False


# --- maybe_continue: the continue/stop decision -----------------------


async def test_no_pending_returns_false(prog):
    h = _Harness(_stopped_state(prog, 1))
    assert await h.stepper.maybe_continue() is False


async def test_continues_while_inside_multiline_statement(prog):
    h = _Harness(_stopped_state(prog, 1))
    await h.stepper.begin("next")
    _restop(h.state, prog, 2)
    assert await h.stepper.maybe_continue() is True
    assert h.issued == ["next"]
    # The stepper re-issued a step: session must look running again.
    assert h.state.phase is SessionPhase.RUNNING
    assert h.state.stack_frames == []


async def test_stops_after_leaving_statement(prog):
    h = _Harness(_stopped_state(prog, 1))
    await h.stepper.begin("next")
    _restop(h.state, prog, 5)  # y = 2 — next logical statement
    assert await h.stepper.maybe_continue() is False
    assert h.issued == []


async def test_single_line_statement_stops_immediately(prog):
    h = _Harness(_stopped_state(prog, 5))
    await h.stepper.begin("next")
    _restop(h.state, prog, 6)
    assert await h.stepper.maybe_continue() is False


async def test_stops_on_breakpoint_and_disarms(prog):
    h = _Harness(_stopped_state(prog, 1))
    await h.stepper.begin("next")
    _restop(h.state, prog, 2, reason="breakpoint")
    assert await h.stepper.maybe_continue() is False
    # Disarmed for good: a later step-reason stop must not resume.
    _restop(h.state, prog, 3, reason="step")
    assert await h.stepper.maybe_continue() is False
    assert h.issued == []


@pytest.mark.parametrize("reason", ["exception", "pause"])
async def test_stops_on_non_step_reasons(prog, reason):
    h = _Harness(_stopped_state(prog, 1))
    await h.stepper.begin("next")
    _restop(h.state, prog, 2, reason=reason)
    assert await h.stepper.maybe_continue() is False


async def test_none_reason_treated_as_step(prog):
    h = _Harness(_stopped_state(prog, 1))
    await h.stepper.begin("next")
    _restop(h.state, prog, 2)
    h.state.stop_reason = None
    assert await h.stepper.maybe_continue() is True


async def test_stops_when_file_changes(prog, tmp_path):
    other = tmp_path / "other.py"
    other.write_text("z = 1\n")
    h = _Harness(_stopped_state(prog, 1))
    await h.stepper.begin("next")
    _restop(h.state, str(other), 1)
    assert await h.stepper.maybe_continue() is False


async def test_stops_without_current_location(prog):
    h = _Harness(_stopped_state(prog, 1))
    await h.stepper.begin("next")
    h.state.enter_stop(thread_id=1, reason="step")
    h.state.set_stack([])  # stop with no resolvable frame
    assert await h.stepper.maybe_continue() is False


async def test_stops_without_thread_id(prog):
    h = _Harness(_stopped_state(prog, 1))
    await h.stepper.begin("next")
    _restop(h.state, prog, 2)
    h.state.current_thread_id = None
    assert await h.stepper.maybe_continue() is False
    assert h.issued == []


async def test_step_in_stops_on_deeper_frame(prog):
    h = _Harness(_stopped_state(prog, 1))
    await h.stepper.begin("stepIn")
    # Entered a call: two frames now, even though the top line (1) is
    # still inside the original statement's range.
    h.state.enter_stop(thread_id=1, reason="step")
    h.state.set_stack([_frame(prog, 1, fid=2, name="g"), _frame(prog, 1)])
    assert await h.stepper.maybe_continue() is False


async def test_step_in_continues_at_same_depth(prog):
    h = _Harness(_stopped_state(prog, 1))
    await h.stepper.begin("stepIn")
    _restop(h.state, prog, 2)
    assert await h.stepper.maybe_continue() is True
    assert h.issued == ["stepIn"]


async def test_iteration_safety_cap_disarms(prog):
    h = _Harness(_stopped_state(prog, 1))
    await h.stepper.begin("next")
    for i in range(h.stepper.MAX_ITERATIONS):
        _restop(h.state, prog, 2)
        assert await h.stepper.maybe_continue() is True, f"iteration {i}"
    _restop(h.state, prog, 2)
    assert await h.stepper.maybe_continue() is False  # cap exceeded
    assert len(h.issued) == h.stepper.MAX_ITERATIONS


# --- step-unit cache --------------------------------------------------


async def test_units_cached_across_file_deletion(prog, tmp_path):
    """Second begin() must not re-read the file: delete it between
    begins and confirm the range still resolves from cache."""
    import os

    h = _Harness(_stopped_state(prog, 1))
    await h.stepper.begin("next")
    h.stepper.cancel()
    os.unlink(prog)
    await h.stepper.begin("next")
    _restop(h.state, prog, 2)
    assert await h.stepper.maybe_continue() is True


# --- match statements (regression companion to source_analysis fix) ---


async def test_multiline_match_subject_is_one_statement(tmp_path):
    path = tmp_path / "matchy.py"
    path.write_text("match get(\n    x,\n):\n    case _:\n        pass\n")
    h = _Harness(_stopped_state(str(path), 1))
    await h.stepper.begin("next")
    _restop(h.state, str(path), 2)  # still inside the match header
    assert await h.stepper.maybe_continue() is True
    _restop(h.state, str(path), 4)  # case _: — its own unit
    assert await h.stepper.maybe_continue() is False


# --- compute_units=None (no source model for language) ----------------


def test_no_compute_units_forces_line_mode():
    h = _Harness(DebugState())  # default harness passes compute_units

    stepper = StatementStepper(
        h.state,
        issue_step=h._issue,
        ensure_stack_loaded=h._ensure,
        compute_units=None,
    )
    assert stepper.mode == "line"
    stepper.set_mode("statement")
    assert stepper.mode == "line"


async def test_line_mode_stepper_never_continues(prog):
    h = _Harness(_stopped_state(prog, 1))
    stepper = StatementStepper(
        h.state,
        issue_step=h._issue,
        ensure_stack_loaded=h._ensure,
        compute_units=None,
    )
    await stepper.begin("next")
    _restop(h.state, prog, 2)
    assert await stepper.maybe_continue() is False
    assert h.issued == []

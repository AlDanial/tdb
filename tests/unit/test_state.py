"""Unit tests for tdb.session.state.DebugState helpers."""

from __future__ import annotations

from tdb.dap.types import Source, StackFrame
from tdb.session.state import DebugState, SessionPhase


def _frame(fid, name="f", path="/a.py", line=1):
    return StackFrame(id=fid, name=name, source=Source(path=path), line=line)


def test_clear_frame_data_resets():
    s = DebugState()
    s.stack_frames = [_frame(1), _frame(2)]
    s.scopes = [object()]  # type: ignore[list-item]
    s.variables[42] = []
    s.current_frame_id = 1

    s.clear_frame_data()
    assert s.stack_frames == []
    assert s.scopes == []
    assert s.variables == {}
    assert s.current_frame_id is None


def test_get_current_source_path_matches_frame_id():
    s = DebugState()
    s.stack_frames = [
        _frame(1, path="/a.py"),
        _frame(2, path="/b.py"),
    ]
    s.current_frame_id = 2
    assert s.get_current_source_path() == "/b.py"


def test_get_current_source_path_returns_none_when_no_match():
    s = DebugState()
    s.stack_frames = [_frame(1)]
    s.current_frame_id = 99
    assert s.get_current_source_path() is None


def test_get_current_line():
    s = DebugState()
    s.stack_frames = [_frame(1, line=10), _frame(2, line=20)]
    s.current_frame_id = 1
    assert s.get_current_line() == 10


def test_get_stop_location_with_path():
    s = DebugState()
    s.stack_frames = [_frame(1, path="/a.py", line=12)]
    assert s.get_stop_location() == ("/a.py", 12)


def test_get_stop_location_without_path_falls_back_to_name():
    s = DebugState()
    # Frame without a source path (e.g., library code under justMyCode)
    s.stack_frames = [
        StackFrame(id=1, name="<builtin>", source=None, line=7),
    ]
    assert s.get_stop_location() == ("<builtin>", 7)


def test_get_stop_location_no_frames():
    s = DebugState()
    assert s.get_stop_location() == ("unknown", 0)


# --- enter_stop atomic transition --------------------------------------------


def test_enter_stop_sets_phase_reason_thread_and_clears_synthetic_flag():
    s = DebugState()
    s.displayed_frames_are_synthetic = True  # left over from a prior task nav
    s.enter_stop(thread_id=42, reason="breakpoint")
    assert s.phase == SessionPhase.STOPPED
    assert s.stop_reason == "breakpoint"
    assert s.current_thread_id == 42
    # A fresh stop replaces whatever was being displayed.
    assert s.displayed_frames_are_synthetic is False


def test_enter_stop_with_none_thread_preserves_existing():
    s = DebugState()
    s.current_thread_id = 7
    s.enter_stop(thread_id=None, reason="pause")
    assert s.current_thread_id == 7
    assert s.stop_reason == "pause"


# --- set_stack: live + synthetic ---------------------------------------------


def test_set_stack_live_defaults_current_frame_to_top():
    s = DebugState()
    frames = [_frame(10), _frame(11), _frame(12)]
    s.set_stack(frames)
    assert s.stack_frames is frames
    assert s.current_frame_id == 10
    assert s.displayed_frames_are_synthetic is False


def test_set_stack_live_honors_explicit_current_frame_id():
    s = DebugState()
    frames = [_frame(10), _frame(11)]
    s.set_stack(frames, current_frame_id=11)
    assert s.current_frame_id == 11


def test_set_stack_live_with_empty_frames_clears_current_frame_id():
    s = DebugState()
    s.current_frame_id = 99
    s.set_stack([])
    assert s.stack_frames == []
    assert s.current_frame_id is None
    assert s.displayed_frames_are_synthetic is False


def test_set_stack_synthetic_sets_flag_and_clears_scopes_and_variables():
    s = DebugState()
    # Pre-populate with stale live data.
    s.scopes = [object()]  # type: ignore[list-item]
    s.variables[5] = [object()]  # type: ignore[list-item]
    frames = [_frame(0), _frame(1)]
    s.set_stack(frames, synthetic=True)
    assert s.displayed_frames_are_synthetic is True
    assert s.current_frame_id == 0
    # Synthetic stacks have no live DAP scope context.
    assert s.scopes == []
    assert s.variables == {}


def test_set_stack_resets_synthetic_flag_when_live_replaces_synthetic():
    s = DebugState()
    s.set_stack([_frame(0)], synthetic=True)
    assert s.displayed_frames_are_synthetic is True
    # Now a real DAP stack_trace lands.
    s.set_stack([_frame(7), _frame(8)])
    assert s.displayed_frames_are_synthetic is False


# --- clear_frame_data also resets the synthetic flag -------------------------


def test_clear_frame_data_resets_synthetic_flag():
    s = DebugState()
    s.set_stack([_frame(0)], synthetic=True)
    s.clear_frame_data()
    assert s.displayed_frames_are_synthetic is False

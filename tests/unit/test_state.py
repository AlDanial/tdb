"""Unit tests for tdb.session.state.DebugState helpers."""

from __future__ import annotations

from tdb.dap.types import Source, StackFrame
from tdb.session.state import DebugState


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

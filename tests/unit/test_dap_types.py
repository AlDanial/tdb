"""Unit tests for tdb.dap.types dataclasses (round-trip + defaults)."""

from __future__ import annotations

from tdb.dap.types import (
    Breakpoint,
    Scope,
    Source,
    SourceBreakpoint,
    StackFrame,
    Thread,
)


def test_source_from_dict_full():
    src = Source.from_dict(
        {
            "path": "/foo/bar.py",
            "name": "bar.py",
            "sourceReference": 7,
        }
    )
    assert src.path == "/foo/bar.py"
    assert src.name == "bar.py"
    assert src.source_reference == 7


def test_source_from_dict_defaults():
    src = Source.from_dict({})
    assert src.path is None
    assert src.name is None
    assert src.source_reference == 0


def test_source_breakpoint_to_dict_minimal():
    bp = SourceBreakpoint(line=42)
    d = bp.to_dict()
    assert d == {"line": 42}
    # `enabled` is tdb-local and must NOT be on the wire
    assert "enabled" not in d


def test_source_breakpoint_to_dict_full():
    bp = SourceBreakpoint(
        line=42,
        condition="x > 0",
        hit_condition="3",
        log_message="hi",
    )
    d = bp.to_dict()
    assert d == {
        "line": 42,
        "condition": "x > 0",
        "hitCondition": "3",
        "logMessage": "hi",
    }


def test_source_breakpoint_enabled_default():
    bp = SourceBreakpoint(line=1)
    assert bp.enabled is True


def test_breakpoint_from_dict_with_source():
    bp = Breakpoint.from_dict(
        {
            "id": 3,
            "verified": True,
            "line": 10,
            "source": {"path": "/x.py"},
            "message": "ok",
        }
    )
    assert bp.id == 3
    assert bp.verified is True
    assert bp.line == 10
    assert bp.source is not None
    assert bp.source.path == "/x.py"
    assert bp.message == "ok"


def test_breakpoint_from_dict_no_source():
    bp = Breakpoint.from_dict({"id": 1, "verified": False, "line": 0})
    assert bp.source is None


def test_thread_from_dict():
    t = Thread.from_dict({"id": 1, "name": "MainThread"})
    assert t.id == 1
    assert t.name == "MainThread"


def test_stack_frame_from_dict_with_source():
    f = StackFrame.from_dict(
        {
            "id": 100,
            "name": "main",
            "source": {"path": "/x.py", "name": "x.py"},
            "line": 7,
            "column": 1,
        }
    )
    assert f.id == 100
    assert f.name == "main"
    assert f.source is not None
    assert f.source.path == "/x.py"
    assert f.line == 7
    assert f.column == 1


def test_stack_frame_from_dict_no_source():
    f = StackFrame.from_dict({"id": 1, "name": "<frame>"})
    assert f.source is None
    assert f.line == 0
    assert f.column == 0


def test_scope_dataclass_defaults():
    s = Scope(name="Locals", variables_reference=5)
    assert s.named_variables == 0
    assert s.indexed_variables == 0

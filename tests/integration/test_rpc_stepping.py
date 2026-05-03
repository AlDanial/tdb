"""Integration tests for stepping + evaluate + run-to-breakpoint flow."""

from __future__ import annotations

from .conftest import SAMPLE_PROGRAM


def test_continue_to_breakpoint_and_evaluate(headless_server):
    """Set a bp, continue, verify we stop there and can evaluate."""
    # Breakpoint on `total = a + b` inside add()
    headless_server.ok("set_breakpoint", f"{SAMPLE_PROGRAM}:9")

    headless_server.ok("continue")

    # Once we're stopped at the breakpoint, the locals should reflect
    # the call args from main().
    a = headless_server.ok("evaluate", "a")
    b = headless_server.ok("evaluate", "b")
    assert "1" in a
    assert "2" in b


def test_inspect_multiple_expressions(headless_server):
    headless_server.ok("set_breakpoint", f"{SAMPLE_PROGRAM}:9")
    headless_server.ok("continue")

    out = headless_server.ok("inspect", "a", "b", "a + b")
    assert "1" in out
    assert "2" in out
    assert "3" in out


def test_get_stack_trace_shows_user_frames(headless_server):
    headless_server.ok("set_breakpoint", f"{SAMPLE_PROGRAM}:9")
    headless_server.ok("continue")

    out = headless_server.ok("get_stack_trace")
    # `add` is on top, `main` underneath it.
    assert "add" in out
    assert "main" in out


def test_step_over_advances_one_line(headless_server):
    headless_server.ok("set_breakpoint", f"{SAMPLE_PROGRAM}:9")
    headless_server.ok("continue")

    # `total = a + b` -> stepping over should land on the next line (`return total`)
    headless_server.ok("next")
    out = headless_server.ok("evaluate", "total")
    assert "3" in out


def test_step_after_program_exits_returns_error(headless_server):
    """After `continue` runs the program to completion, the step actions
    must report `Program has terminated` instead of trying to send DAP
    requests to a dead session.
    """
    headless_server.ok("continue")
    body = headless_server.call("next")
    assert body["success"] is False
    assert "terminated" in body["value"].lower()


def test_pause_after_program_exits_returns_error(headless_server):
    headless_server.ok("continue")
    body = headless_server.call("pause")
    assert body["success"] is False
    assert "terminated" in body["value"].lower()


def test_continue_after_program_exits_returns_error(headless_server):
    """A second `continue` after the program has ended is rejected,
    not silently re-executed.
    """
    headless_server.ok("continue")
    body = headless_server.call("continue")
    assert body["success"] is False
    assert "terminated" in body["value"].lower()

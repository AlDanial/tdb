"""Integration tests for breakpoint set / list / remove via RPC."""

from __future__ import annotations

from .conftest import SAMPLE_PROGRAM


def test_set_and_list_breakpoint(headless_server):
    location = f"{SAMPLE_PROGRAM}:14"  # `x = 1` inside main()
    headless_server.ok("set_breakpoint", location)

    listed = headless_server.ok("list_breakpoints")
    assert str(SAMPLE_PROGRAM) in listed
    assert ":14" in listed


def test_set_conditional_breakpoint(headless_server):
    location = f"{SAMPLE_PROGRAM}:9"  # inside add(): `total = a + b`
    headless_server.ok("set_breakpoint", location, "a > 0")
    listed = headless_server.ok("list_breakpoints")
    assert "a > 0" in listed


def test_remove_breakpoint(headless_server):
    location = f"{SAMPLE_PROGRAM}:14"
    headless_server.ok("set_breakpoint", location)
    headless_server.ok("remove_breakpoint", location)
    listed = headless_server.ok("list_breakpoints")
    assert ":14" not in listed


def test_set_breakpoint_invalid_format(headless_server):
    body = headless_server.call("set_breakpoint", "no-colon-here")
    assert body["success"] is False


def test_set_breakpoint_invalid_line(headless_server):
    body = headless_server.call("set_breakpoint", f"{SAMPLE_PROGRAM}:notanumber")
    assert body["success"] is False

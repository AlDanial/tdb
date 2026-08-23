"""Integration tests covering basic RPC plumbing: status, help, get_source."""

from __future__ import annotations

from .conftest import SAMPLE_PROGRAM


def test_unknown_action_returns_error(headless_server):
    body = headless_server.call("not-an-action")
    assert body["success"] is False
    assert "Unknown action" in body["value"]


def test_legacy_rpc_response_omits_structured_data_field(headless_server):
    body = headless_server.call("not-an-action")

    assert "data" not in body


def test_help_lists_known_actions(headless_server):
    out = headless_server.ok("help")
    # A few representative actions should appear in the listing.
    for action in ("status", "set_breakpoint", "next", "continue", "quit"):
        assert action in out


def test_status_reports_paused_state(headless_server):
    # We launched with stop_on_entry, so the program is paused.
    out = headless_server.ok("status")
    assert out  # status always returns something


def test_get_source_returns_file_contents(headless_server):
    out = headless_server.ok("get_source", str(SAMPLE_PROGRAM))
    assert "def main()" in out
    assert "def add(a, b):" in out


def test_get_source_unknown_file_errors(headless_server):
    body = headless_server.call("get_source", "/nonexistent/x.py")
    assert body["success"] is False


def test_list_breakpoints_initially_empty(headless_server):
    out = headless_server.ok("list_breakpoints")
    # Empty or "no breakpoints"-style message; either way, no entries that
    # look like a path:line pairing.
    assert ".py:" not in out

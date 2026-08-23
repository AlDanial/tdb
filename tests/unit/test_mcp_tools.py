"""Schema-level tests for the MCP tool surface.

The agent's first interaction with tdb-mcp is the tool list and the
parameter schemas. Pin both: name set, count, that every tool has a
description, and that the consolidated `control` action enumerates
exactly the actions the dispatch table supports.

Behavior tests for individual tools live in test_mcp_session.py
(driving McpSession._call directly) — those would be redundant here
since the tool wrappers are thin translations into _call.
"""

from __future__ import annotations

import asyncio

import pytest

from tdb.mcp.server import _format, _parse_breakpoints, _parse_path_mappings, build_mcp
from tdb.mcp.session import McpSession
from tdb.server.rpc_types import RpcResponse


# --- Tool registration --------------------------------------------------


EXPECTED_TOOL_NAMES = {
    # Lifecycle (3)
    "debug_launch",
    "debug_attach",
    "quit",
    # Control (1, consolidates 6 dispatch actions)
    "control",
    # Inspection (5)
    "inspect",
    "read_source",
    "stack_trace",
    "status",
    "get_output",
    # Breakpoints (3)
    "set_breakpoint",
    "remove_breakpoint",
    "list_breakpoints",
    # Differentiators (5)
    "threads",
    "tasks",
    "processes",
    "wait_graph",
    "rust_concurrency",
}


@pytest.fixture
def mcp_tools():
    mcp = build_mcp(session=McpSession())
    return asyncio.run(mcp.list_tools())


def test_expected_tool_set(mcp_tools):
    """Pin the curated tool surface so adding/removing a tool is a
    visible decision in code review. Auto-generating one MCP tool per
    dispatch-table action (26 of them) was explicitly rejected — the
    agent's tool list should stay small and focused."""
    names = {t.name for t in mcp_tools}
    assert names == EXPECTED_TOOL_NAMES


def test_every_tool_has_a_description(mcp_tools):
    """Agents pick tools from descriptions, not from inferring intent
    from names. A missing description silently degrades tool-selection
    quality — fail loudly here instead."""
    for t in mcp_tools:
        assert t.description, f"tool {t.name!r} has no description"


def test_control_enumerates_every_supported_action(mcp_tools):
    """`control` consolidates 6 dispatch-table actions behind one
    tool. Its action Literal must match exactly what RpcHandlers
    dispatches — drift here would mean MCP exposes / hides actions
    the HTTP API does not."""
    control = next(t for t in mcp_tools if t.name == "control")
    action_enum = set(control.inputSchema["properties"]["action"]["enum"])
    assert action_enum == {
        "continue",
        "next",
        "step_in",
        "step_out",
        "pause",
        "wait_for_stop",
    }


def test_control_default_timeout_is_30s(mcp_tools):
    """30s default matches Fable's recommendation: short enough that
    an agent polling a runaway program gets a 'still running' return
    promptly, long enough that normal step+stop completes in one call."""
    control = next(t for t in mcp_tools if t.name == "control")
    assert control.inputSchema["properties"]["timeout_s"]["default"] == 30.0


def test_debug_attach_accepts_rust_program_and_profile_selection(mcp_tools):
    attach = next(tool for tool in mcp_tools if tool.name == "debug_attach")
    properties = attach.inputSchema["properties"]
    assert {"program", "lang", "adapter"} <= properties.keys()


# --- _format ------------------------------------------------------------


def test_format_success_returns_value():
    assert _format(RpcResponse.ok("at file.py:42")) == "at file.py:42"


def test_format_success_with_empty_value_returns_ok():
    """RpcResponse.ok() (no value) returns "" — agents shouldn't see
    a blank string and assume failure; surface a benign 'ok'."""
    assert _format(RpcResponse.ok()) == "ok"


def test_format_structured_response_returns_stable_json():
    assert _format(RpcResponse.ok_data({"warnings": [], "threads": []})) == (
        '{"threads": [], "warnings": []}'
    )


def test_format_error_prefixes_with_error_keyword():
    """The 'still running' sentinel is a SUCCESS — agents distinguish
    success/error by the 'Error:' prefix, not by parsing the message."""
    rendered = _format(RpcResponse.error("not authorized"))
    assert rendered.startswith("Error:")
    assert "not authorized" in rendered


# --- _parse_breakpoints -------------------------------------------------


def test_parse_breakpoints_none_returns_none():
    assert _parse_breakpoints(None) is None


def test_parse_breakpoints_empty_returns_none():
    assert _parse_breakpoints([]) is None


def test_parse_breakpoints_splits_file_and_line():
    assert _parse_breakpoints(["foo.py:42", "bar/baz.py:7"]) == [
        ("foo.py", 42),
        ("bar/baz.py", 7),
    ]


def test_parse_breakpoints_handles_windows_drive_letter():
    """Last-colon split — drive letter survives intact."""
    assert _parse_breakpoints(["C:/proj/foo.py:42"]) == [("C:/proj/foo.py", 42)]


def test_parse_breakpoints_rejects_missing_colon():
    with pytest.raises(ValueError, match="file:line"):
        _parse_breakpoints(["no-colon-here"])


# --- _parse_path_mappings -----------------------------------------------


def test_parse_path_mappings_none_returns_none():
    assert _parse_path_mappings(None) is None


def test_parse_path_mappings_normalizes_json_arrays_to_tuples():
    """JSON wire format is array-of-arrays; the DAP client wants
    tuples. Normalize at the MCP boundary."""
    assert _parse_path_mappings(
        [["/local/a", "/remote/a"], ["/local/b", "/remote/b"]]
    ) == [
        ("/local/a", "/remote/a"),
        ("/local/b", "/remote/b"),
    ]

"""Syncing tdb's breakpoint table with breakpoints the user set directly
in the native debugger (issue #52).

With `gdb -i dap`, REPL input such as `b 83` goes straight to gdb and
creates a breakpoint tdb never hears about. When the Breakpoints View
gains focus the controller asks the adapter for a listing command,
parses the result, reconciles it with `state.breakpoints`, and takes
ownership of any CLI-created breakpoints (delete in gdb, re-push over
DAP) so later tdb operations on them actually take effect.
"""

from __future__ import annotations

import json

import pytest

from tdb.dap.types import SourceBreakpoint
from tdb.session.state import SessionPhase
from tdb.languages.base import AdapterSpec
from tdb.languages.cpp import GdbDapAdapter, LldbDapAdapter, build_cpp_profile
from tdb.session.breakpoint_sync import (
    DebuggerBreakpoint,
    parse_breakpoint_listing,
    reconcile,
)

from tests.unit.test_controller_actions import _make


# --- adapter hook -------------------------------------------------------


def test_base_adapter_has_no_query_command():
    assert AdapterSpec().breakpoint_query_command() is None


def test_lldb_adapter_has_no_query_command():
    assert LldbDapAdapter().breakpoint_query_command() is None


def test_gdb_query_command_is_one_python_line_listing_breakpoints():
    cmd = GdbDapAdapter().breakpoint_query_command()
    assert cmd is not None
    assert "\n" not in cmd
    assert cmd.startswith("python ")
    assert "gdb.breakpoints()" in cmd
    # Distinguishes DAP-created from CLI-created breakpoints, skipping
    # the stale map entries a CLI `delete` leaves behind (gdb 17.1
    # raises "Breakpoint N is invalid" on reading their number).
    assert "breakpoint_map" in cmd
    assert "is_valid()" in cmd


# --- listing parser -----------------------------------------------------


def _row(n, path, line, enabled=True, cond=None, dap=False) -> dict:
    return {
        "n": n,
        "path": path,
        "line": line,
        "enabled": enabled,
        "cond": cond,
        "dap": dap,
    }


def test_parse_listing_reads_json_line():
    text = json.dumps(
        [
            _row(3, "/src/a.c", 83, cond="x > 1"),
            _row(4, "/src/b.c", 5, enabled=False, dap=True),
        ]
    )
    got = parse_breakpoint_listing(text + "\n")
    assert got == [
        DebuggerBreakpoint(
            number=3,
            path="/src/a.c",
            line=83,
            enabled=True,
            condition="x > 1",
            dap_owned=False,
        ),
        DebuggerBreakpoint(
            number=4,
            path="/src/b.c",
            line=5,
            enabled=False,
            condition=None,
            dap_owned=True,
        ),
    ]


def test_parse_listing_skips_leading_noise_lines():
    text = "some gdb chatter\n" + json.dumps([_row(1, "/src/a.c", 7)]) + "\n"
    assert [bp.line for bp in parse_breakpoint_listing(text)] == [7]


def test_parse_listing_rejects_garbage():
    with pytest.raises(ValueError):
        parse_breakpoint_listing("No breakpoints or watchpoints.\n")


# --- reconciliation -----------------------------------------------------


def _dbp(n, path, line, enabled=True, cond=None, dap=False) -> DebuggerBreakpoint:
    return DebuggerBreakpoint(
        number=n, path=path, line=line, enabled=enabled, condition=cond, dap_owned=dap
    )


def test_reconcile_adopts_cli_breakpoint_with_condition_and_deletes_it_in_gdb():
    state = {}
    plan = reconcile(state, False, [_dbp(3, "/src/a.c", 83, cond="i == 2")])
    assert plan.changed_paths == {"/src/a.c"}
    assert plan.delete_numbers == [3]
    (bp,) = state["/src/a.c"]
    assert (bp.line, bp.condition, bp.enabled) == (83, "i == 2", True)


def test_reconcile_adopts_disabled_cli_breakpoint_as_disabled():
    state = {}
    reconcile(state, False, [_dbp(3, "/src/a.c", 83, enabled=False)])
    assert state["/src/a.c"][0].enabled is False


def test_reconcile_leaves_dap_owned_breakpoints_alone():
    state = {"/src/a.c": [SourceBreakpoint(line=83)]}
    plan = reconcile(state, False, [_dbp(1, "/src/a.c", 83, dap=True)])
    assert plan.changed_paths == set()
    assert plan.delete_numbers == []
    assert [bp.line for bp in state["/src/a.c"]] == [83]


def test_reconcile_removes_tdb_breakpoint_deleted_in_gdb():
    state = {"/src/a.c": [SourceBreakpoint(line=83), SourceBreakpoint(line=90)]}
    plan = reconcile(state, False, [_dbp(1, "/src/a.c", 90, dap=True)])
    assert plan.changed_paths == {"/src/a.c"}
    assert [bp.line for bp in state["/src/a.c"]] == [90]


def test_reconcile_removes_whole_file_deleted_in_gdb():
    state = {"/src/a.c": [SourceBreakpoint(line=83)]}
    plan = reconcile(state, False, [])
    assert plan.changed_paths == {"/src/a.c"}
    assert state == {}


def test_reconcile_keeps_tdb_disabled_breakpoint_absent_from_gdb():
    # Disabled breakpoints are never sent to the adapter, so gdb not
    # listing them is expected — not a user deletion.
    state = {"/src/a.c": [SourceBreakpoint(line=83, enabled=False)]}
    plan = reconcile(state, False, [])
    assert plan.changed_paths == set()
    assert [bp.line for bp in state["/src/a.c"]] == [83]


def test_reconcile_with_disable_all_only_adds():
    state = {"/src/a.c": [SourceBreakpoint(line=83)]}
    plan = reconcile(state, True, [_dbp(5, "/src/b.c", 9)])
    assert [bp.line for bp in state["/src/a.c"]] == [83]
    assert [bp.line for bp in state["/src/b.c"]] == [9]
    assert plan.changed_paths == {"/src/b.c"}
    assert plan.delete_numbers == [5]


def test_reconcile_takes_condition_and_enabled_from_gdb_for_matches():
    state = {"/src/a.c": [SourceBreakpoint(line=83, condition="old")]}
    plan = reconcile(
        state, False, [_dbp(1, "/src/a.c", 83, enabled=False, cond="new", dap=True)]
    )
    assert plan.changed_paths == {"/src/a.c"}
    (bp,) = state["/src/a.c"]
    assert (bp.condition, bp.enabled) == ("new", False)


def test_reconcile_unchanged_match_reports_nothing():
    state = {"/src/a.c": [SourceBreakpoint(line=83, condition="c")]}
    plan = reconcile(state, False, [_dbp(1, "/src/a.c", 83, cond="c", dap=True)])
    assert plan.changed_paths == set()
    assert plan.delete_numbers == []


def test_reconcile_cli_duplicate_of_dap_breakpoint_is_deleted_not_readded():
    # `b 83` typed when tdb already has line 83: gdb now holds two
    # breakpoints there. Drop the CLI one so tdb's stays authoritative.
    state = {"/src/a.c": [SourceBreakpoint(line=83)]}
    plan = reconcile(
        state,
        False,
        [_dbp(1, "/src/a.c", 83, dap=True), _dbp(2, "/src/a.c", 83)],
    )
    assert plan.delete_numbers == [2]
    assert [bp.line for bp in state["/src/a.c"]] == [83]


# --- controller orchestration ------------------------------------------


def _listing(*rows) -> str:
    return json.dumps(list(rows)) + "\n"


async def test_controller_sync_noop_without_query_command():
    ctrl, fake, _ = _make(profile=build_cpp_profile(adapter="lldb-dap"))
    assert await ctrl.sync_breakpoints_from_debugger() is False
    assert fake.calls_to("evaluate") == []


async def test_controller_sync_noop_when_not_paused():
    ctrl, fake, _ = _make(profile=build_cpp_profile(adapter="gdb"))
    ctrl.state.transition_to(SessionPhase.RUNNING)
    assert await ctrl.sync_breakpoints_from_debugger() is False
    assert fake.calls_to("evaluate") == []


async def test_controller_sync_adopts_cli_breakpoint_and_repushes_file():
    ctrl, fake, _ = _make(profile=build_cpp_profile(adapter="gdb"))
    fake.evaluate_effects = [
        (_listing(_row(3, "/src/a.c", 83, cond="i == 2")), 0),
        ("", 0),  # delete 3
    ]
    changed = await ctrl.sync_breakpoints_from_debugger()
    assert changed is True
    evals = [c[1] for c in fake.calls_to("evaluate")]
    assert evals[0] == GdbDapAdapter().breakpoint_query_command()
    assert evals[1] == "delete 3"
    assert fake.calls_to("setBreakpoints") == [("setBreakpoints", "/src/a.c", (83,))]
    (bp,) = ctrl.state.breakpoints["/src/a.c"]
    assert (bp.line, bp.condition) == (83, "i == 2")


async def test_controller_sync_repushes_file_after_gdb_side_delete():
    ctrl, fake, _ = _make(profile=build_cpp_profile(adapter="gdb"))
    ctrl.state.breakpoints["/src/a.c"] = [SourceBreakpoint(line=83)]
    fake.evaluate_effects = [(_listing(), 0)]
    assert await ctrl.sync_breakpoints_from_debugger() is True
    assert ctrl.state.breakpoints == {}
    assert fake.calls_to("setBreakpoints") == [("setBreakpoints", "/src/a.c", ())]


async def test_controller_sync_returns_false_when_nothing_changed():
    ctrl, fake, _ = _make(profile=build_cpp_profile(adapter="gdb"))
    ctrl.state.breakpoints["/src/a.c"] = [SourceBreakpoint(line=83)]
    fake.evaluate_effects = [(_listing(_row(1, "/src/a.c", 83, dap=True)), 0)]
    assert await ctrl.sync_breakpoints_from_debugger() is False
    assert fake.calls_to("setBreakpoints") == []


async def test_controller_sync_warns_and_keeps_state_on_bad_listing():
    ctrl, fake, handler = _make(profile=build_cpp_profile(adapter="gdb"))
    ctrl.state.breakpoints["/src/a.c"] = [SourceBreakpoint(line=83)]
    fake.evaluate_effects = [("No symbol table is loaded.\n", 0)]
    assert await ctrl.sync_breakpoints_from_debugger() is False
    assert [bp.line for bp in ctrl.state.breakpoints["/src/a.c"]] == [83]
    assert any("sync" in text and cat == "console" for text, cat in handler.outputs)


async def test_controller_sync_warns_and_keeps_state_on_evaluate_error():
    ctrl, fake, handler = _make(profile=build_cpp_profile(adapter="gdb"))
    ctrl.state.breakpoints["/src/a.c"] = [SourceBreakpoint(line=83)]
    fake.evaluate_effects = [RuntimeError("boom")]
    assert await ctrl.sync_breakpoints_from_debugger() is False
    assert [bp.line for bp in ctrl.state.breakpoints["/src/a.c"]] == [83]
    assert any("sync" in text and cat == "console" for text, cat in handler.outputs)

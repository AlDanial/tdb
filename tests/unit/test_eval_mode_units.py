"""Unit tests for eval_mode helpers: bound-line matching (adapters may
rebind a breakpoint to a different line at setBreakpoints time), the
no-threadId resume fallback, and final-exit-code semantics."""

from tdb import eval_mode
from tdb.dap.types import SourceBreakpoint
from tests.unit.test_controller_actions import _make


async def test_send_breakpoints_records_bound_lines():
    ctrl, fake, _ = _make()
    fake.breakpoint_results = [
        {"id": 1, "verified": True, "line": 5},
        {"id": 2, "verified": True, "line": 27},
    ]
    bps = [SourceBreakpoint(line=5), SourceBreakpoint(line=25)]
    await ctrl._send_breakpoints("/src/prog.c", bps)
    assert ctrl.state.bound_breakpoint_lines == {"/src/prog.c": {5: 5, 25: 27}}


async def test_send_breakpoints_ignores_missing_bound_line():
    # An adapter that omits `line` in its response (Breakpoint.line
    # defaults to 0) must not poison the mapping.
    ctrl, fake, _ = _make()
    fake.breakpoint_results = [{"id": 1, "verified": True}]
    await ctrl._send_breakpoints("/src/prog.c", [SourceBreakpoint(line=25)])
    assert ctrl.state.bound_breakpoint_lines == {"/src/prog.c": {}}


def test_match_on_requested_line(tmp_path):
    p = tmp_path / "prog.py"
    p.write_text("x = 1\n")
    points = [(str(p), 25, "print(x)")]
    hits = eval_mode._matching_points(points, {}, str(p), 25)
    assert hits == points


def test_match_on_rebound_line(tmp_path):
    # gdb binds line 25 (a declaration) to line 27; stops report 27.
    p = tmp_path / "prog.c"
    p.write_text("int x;\n")
    points = [(str(p), 25, "print x")]
    bound = {str(p): {25: 27}}
    assert eval_mode._matching_points(points, bound, str(p), 27) == points
    assert eval_mode._matching_points(points, bound, str(p), 25) == points
    assert eval_mode._matching_points(points, bound, str(p), 26) == []


def test_no_match_on_other_file(tmp_path):
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text("x = 1\n")
    b.write_text("x = 1\n")
    points = [(str(a), 3, "x")]
    assert eval_mode._matching_points(points, {}, str(b), 3) == []


async def test_resume_client_falls_back_to_threads_when_no_thread_id():
    # A `stopped` event without threadId (optional per DAP) must still
    # resume the process, or the run hangs; resolve a thread first.
    _, fake, _ = _make()
    await eval_mode._resume_client(fake, None)
    assert ("continue", 1) in fake.calls


async def test_resume_client_uses_given_thread_id():
    _, fake, _ = _make()
    await eval_mode._resume_client(fake, 7)
    assert ("continue", 7) in fake.calls


async def test_resume_client_none_is_noop():
    # No client (a stop we couldn't attribute) must not raise.
    await eval_mode._resume_client(None, 3)


def test_final_exit_code_prefers_real_code():
    console = eval_mode._EvalRunHandler()
    console.exit_code = 7
    assert eval_mode._final_exit_code(console) == 7


def test_final_exit_code_zero_is_zero():
    console = eval_mode._EvalRunHandler()
    console.exit_code = 0
    assert eval_mode._final_exit_code(console) == 0


def test_final_exit_code_terminated_without_exited_is_failure():
    # `terminated` with no `exited` event (hard crash, adapter death)
    # must not report success to the shell.
    console = eval_mode._EvalRunHandler()
    assert console.exit_code is None
    assert eval_mode._final_exit_code(console) == 1

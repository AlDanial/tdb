"""BashSession <-> tdb_harness.sh, no DAP layer involved."""

import asyncio
import os
from pathlib import Path

import pytest

from tdb.adapters.bash.session import BashProtocolError, BashSession, b64, canonical
from tests.integration.bash_adapter_harness import bash_ok

FIXTURES = Path(__file__).parent / "fixtures"


pytestmark = pytest.mark.skipif(not bash_ok(), reason="needs bash >= 4.4")


class Recorder:
    def __init__(self):
        self.output: list[tuple[str, str]] = []
        self.stops: list[tuple[str, str, int]] = []
        self.exit_code = None
        self.stop_event = asyncio.Event()
        self.exit_event = asyncio.Event()

    def on_output(self, text, category):
        self.output.append((text, category))

    def on_stop(self, reason, path, line):
        self.stops.append((reason, path, line))
        self.stop_event.set()

    def on_exit(self, code):
        self.exit_code = code
        self.exit_event.set()

    async def wait_stop(self):
        await asyncio.wait_for(self.stop_event.wait(), 10)
        self.stop_event.clear()
        return self.stops[-1]

    def stdout(self):
        return "".join(t for t, c in self.output if c == "stdout")

    def stderr(self):
        return "".join(t for t, c in self.output if c == "stderr")


async def _launch(program, rec, args=None):
    session = BashSession(rec.on_output, rec.on_stop, rec.on_exit)
    await session.launch(
        program=str(program), args=args or [], cwd=str(program.parent), env=None
    )
    return session


@pytest.mark.asyncio
async def test_launch_reaches_config_phase():
    rec = Recorder()
    session = await _launch(FIXTURES / "bash_hello.sh", rec)
    assert session.stopped is True  # config phase counts as stopped
    await session.stop()


@pytest.mark.asyncio
async def test_stop_on_entry_then_continue_to_exit():
    rec = Recorder()
    session = await _launch(FIXTURES / "bash_hello.sh", rec)
    session.resume("step")  # stopOnEntry = arm step before line 1
    reason, path, line = await rec.wait_stop()
    assert reason == "entry"
    assert path.endswith("bash_hello.sh")
    assert line == 1
    assert session.stopped is True
    session.resume("continue")
    await asyncio.wait_for(rec.exit_event.wait(), 10)
    assert rec.exit_code == 7
    assert "hello from bash" in rec.stdout()
    assert "x is 1" in rec.stdout()
    await session.stop()


@pytest.mark.asyncio
async def test_continue_from_config_runs_to_exit():
    rec = Recorder()
    session = await _launch(FIXTURES / "bash_hello.sh", rec)
    session.resume("continue")  # stopOnEntry=false path
    await asyncio.wait_for(rec.exit_event.wait(), 10)
    assert rec.exit_code == 7
    assert rec.stops == []
    await session.stop()


# --- regression tests added after code review (C1, C2, I1, I5) -----------


@pytest.mark.asyncio
async def test_strict_mode_stop_on_entry_then_continue_to_exit():
    """C1 regression: under `set -euo pipefail`, "${#FUNCNAME[@]}" as a
    literal trap argument is an unbound-variable error once `set -u` takes
    effect (FUNCNAME is unset at top level), which used to make extdebug
    skip every remaining command (or, without `set -e`, made the trap exit
    nonzero and skip everything anyway). The fix computes depth inside
    __tdb_should_stop instead of passing it in as a trap argument.
    """
    rec = Recorder()
    session = await _launch(FIXTURES / "bash_strict.sh", rec)
    session.resume("step")
    reason, path, line = await rec.wait_stop()
    assert reason == "entry"
    assert path.endswith("bash_strict.sh")
    assert line == 1
    session.resume("continue")
    await asyncio.wait_for(rec.exit_event.wait(), 10)
    assert rec.exit_code == 7
    assert "line one" in rec.stdout()
    assert "line two" in rec.stdout()
    await session.stop()


@pytest.mark.asyncio
async def test_extdebug_armed_without_bashdb_startup_noise():
    """C2 regression: `shopt -s extdebug` set directly in the BASH_ENV
    startup file makes bash try to load its bashdb debugger profile
    ("cannot start debugger; debugging mode disabled" on stderr) and ends
    up leaving extdebug OFF. The fix arms extdebug from inside the DEBUG
    trap instead (after startup-file processing has finished).
    """
    rec = Recorder()
    session = await _launch(FIXTURES / "bash_hello.sh", rec)
    session.resume("step")
    await rec.wait_stop()
    result = await session.request("eval " + b64("shopt -p extdebug"))
    assert "-s extdebug" in result  # "-s" = on; "-u" would mean still off
    session.resume("continue")
    await asyncio.wait_for(rec.exit_event.wait(), 10)
    stderr_text = rec.stderr()
    assert "bashdb" not in stderr_text
    assert "debugging mode disabled" not in stderr_text
    await session.stop()


@pytest.mark.asyncio
async def test_debuggee_ifs_does_not_corrupt_command_channel():
    """I1 regression: __tdb_read_cmd's inner field-split `read` used to
    inherit the debuggee's global IFS, so a debuggee that does `IFS=,`
    would corrupt every multi-field command (e.g. `setbp <path> <line>
    <cond>`) sent afterwards. The fix makes IFS local to that function.
    """
    rec = Recorder()
    session = await _launch(FIXTURES / "bash_ifs.sh", rec)
    session.resume("step")
    reason, path, line = await rec.wait_stop()
    assert reason == "entry" and line == 1
    session.resume("step")  # advance past `IFS=,` — debuggee IFS is now ','
    reason, path, line = await rec.wait_stop()
    assert reason == "step" and line == 2

    bp_path = canonical(str(FIXTURES / "bash_ifs.sh"), base=session.launch_cwd)
    await session.request(f"setbp {b64(bp_path)} 3 {b64('')}")

    session.resume("continue")
    reason, path, line = await rec.wait_stop()
    assert reason == "breakpoint"
    assert line == 3

    session.resume("continue")
    await asyncio.wait_for(rec.exit_event.wait(), 10)
    assert rec.exit_code == 5
    assert "before" in rec.stdout()
    assert "after" in rec.stdout()
    await session.stop()


@pytest.mark.asyncio
async def test_exit_reported_promptly_despite_background_grandchild():
    """I5 regression: a backgrounded grandchild (`sleep 30 &`) inherits
    stdout/stderr (and the control pipes), so the output pumps never see
    EOF on their own. The fix bounds how long _reap waits for pump flush
    before reporting on_exit, so exit is reported promptly instead of
    hanging until the grandchild also exits.
    """
    rec = Recorder()
    session = await _launch(FIXTURES / "bash_bg_grandchild.sh", rec)
    session.resume("continue")
    await asyncio.wait_for(rec.exit_event.wait(), 4)
    assert rec.exit_code == 3
    assert "started" in rec.stdout()
    await session.stop()


# --- Task 5: stepping semantics + trap-correctness regressions -----------


async def _stop_lines(session, rec, mode, count):
    """Resume with `mode` `count` times, collecting (path-tail, line)."""
    hits = []
    for _ in range(count):
        session.resume(mode)
        reason, path, line = await rec.wait_stop()
        hits.append((path.rsplit("/", 1)[-1], line))
    return hits


@pytest.mark.asyncio
async def test_step_in_descends_into_function():
    rec = Recorder()
    session = await _launch(FIXTURES / "bash_functions.sh", rec)
    session.resume("step")
    await rec.wait_stop()  # entry: line 9 (`outer` call — see next test's note)
    # keep stepping; we must eventually stop on the `local iv=99` line (2)
    lines = [line for _, line in await _stop_lines(session, rec, "step", 6)]
    assert 2 in lines  # inside inner()
    await session.stop()


@pytest.mark.asyncio
async def test_next_steps_over_call():
    """DEVIATION from the brief: the brief's version looped `next` waiting
    for an intermediate stop at line 9 before checking the "step over"
    landing. Empirically (confirmed against a vanilla `bash -o functrace`
    trap, independent of the harness) the DEBUG trap never fires on a bare
    function-definition line — only on the first *executable* statement.
    bash_functions.sh's lines 1-8 are function definitions, so the very
    first trap firing (our stopOnEntry `step`) already lands on line 9,
    the `outer` call itself. The brief's while-loop therefore never saw a
    second line==9 stop and hung until timeout. This is a fixture/trap-
    semantics miscount, not a harness defect: a single `next` from the
    call line must land clean on line 10 without ever stopping inside
    outer()/inner() (depth 1/2), which is the actual "step over" invariant
    this test protects.
    """
    rec = Recorder()
    session = await _launch(FIXTURES / "bash_functions.sh", rec)
    session.resume("step")
    reason, path, line = await rec.wait_stop()
    assert reason == "entry"
    assert line == 9  # the `outer` call: first executable line in the file
    session.resume("next")
    _, _, line = await rec.wait_stop()
    assert line == 10  # stepped clean over outer()'s (and inner()'s) body
    await session.stop()


@pytest.mark.asyncio
async def test_finish_returns_to_caller():
    rec = Recorder()
    session = await _launch(FIXTURES / "bash_functions.sh", rec)
    session.resume("step")
    await rec.wait_stop()
    # step until inside inner() (line 2)
    for _ in range(20):
        session.resume("step")
        _, _, line = await rec.wait_stop()
        if line == 2:
            break
    else:
        raise AssertionError("never reached inner()")
    session.resume("finish")
    _, _, line = await rec.wait_stop()
    assert line == 7  # back in outer(): echo line
    await session.stop()


@pytest.mark.asyncio
async def test_step_follows_sourced_file():
    rec = Recorder()
    session = await _launch(FIXTURES / "bash_sourced_main.sh", rec)
    session.resume("step")
    await rec.wait_stop()
    files = set()
    for _ in range(10):
        session.resume("step")
        _, path, _ = await rec.wait_stop()
        files.add(path.rsplit("/", 1)[-1])
        if "bash_sourced_lib.sh" in files:
            break
    assert "bash_sourced_lib.sh" in files
    await session.stop()


@pytest.mark.asyncio
async def test_dollar_question_survives_a_stop():
    """REGRESSION (spec): the trap must not clobber $? nor skip commands.

    Stops on every line via step mode; rc=1 must still be printed."""
    rec = Recorder()
    session = await _launch(FIXTURES / "bash_status.sh", rec)
    session.resume("step")
    await rec.wait_stop()
    for _ in range(10):
        if rec.exit_event.is_set():
            break
        session.resume("step")
        try:
            await asyncio.wait_for(rec.stop_event.wait(), 5)
            rec.stop_event.clear()
        except asyncio.TimeoutError:
            break
    await asyncio.wait_for(rec.exit_event.wait(), 5)
    assert "rc=1" in rec.stdout()


@pytest.mark.asyncio
async def test_no_statement_skipped_under_stepping():
    """REGRESSION (spec): extdebug + nonzero trap status skips commands.

    All three echo lines of bash_hello.sh must run when stepping through."""
    rec = Recorder()
    session = await _launch(FIXTURES / "bash_hello.sh", rec)
    session.resume("step")
    await rec.wait_stop()
    while not rec.exit_event.is_set():
        session.resume("step")
        try:
            await asyncio.wait_for(rec.stop_event.wait(), 5)
            rec.stop_event.clear()
        except asyncio.TimeoutError:
            break
    assert "hello from bash" in rec.stdout()
    assert "x is 1" in rec.stdout()
    assert rec.exit_code == 7


# --- Task 6: breakpoints — set/clear, conditions, live edits, pause ------


@pytest.mark.asyncio
async def test_breakpoint_hits_and_reports_line():
    rec = Recorder()
    fixture = FIXTURES / "bash_loop.sh"
    session = await _launch(fixture, rec)
    await session.set_breakpoint(str(fixture), 5)  # echo "total=$total"
    session.resume("continue")
    reason, path, line = await rec.wait_stop()
    assert (reason, line) == ("breakpoint", 5)
    assert path.endswith("bash_loop.sh")
    assert "total=" not in rec.stdout()  # stopped BEFORE the echo
    session.resume("continue")
    await asyncio.wait_for(rec.exit_event.wait(), 15)
    assert "total=15" in rec.stdout()
    await session.stop()


@pytest.mark.asyncio
async def test_conditional_breakpoint_stops_when_condition_exits_zero():
    rec = Recorder()
    fixture = FIXTURES / "bash_loop.sh"
    session = await _launch(fixture, rec)
    await session.set_breakpoint(str(fixture), 3, "(( i == 4 ))")
    session.resume("continue")
    reason, _, line = await rec.wait_stop()
    assert (reason, line) == ("breakpoint", 3)
    # the condition saw the loop variable: i is 4 right now
    payload = await session.request("eval " + b64('echo "i=$i"'))
    assert "i=4" in payload
    session.resume("continue")
    # i never equals 4 again -> runs to completion
    await asyncio.wait_for(rec.exit_event.wait(), 15)
    await session.stop()


@pytest.mark.asyncio
async def test_clear_breakpoints_while_stopped():
    rec = Recorder()
    fixture = FIXTURES / "bash_loop.sh"
    session = await _launch(fixture, rec)
    await session.set_breakpoint(str(fixture), 3)
    session.resume("continue")
    await rec.wait_stop()
    await session.clear_breakpoints()
    session.resume("continue")
    await asyncio.wait_for(rec.exit_event.wait(), 15)  # no further stops
    await session.stop()


@pytest.mark.asyncio
async def test_live_breakpoint_add_while_running():
    """The drain path: a bp set while bash is running (slow loop) is honored."""
    rec = Recorder()
    fixture = FIXTURES / "bash_loop.sh"
    session = await _launch(fixture, rec)
    session.resume("continue")  # no breakpoints; enters slow loop
    await asyncio.sleep(0.3)
    session.set_breakpoint_nowait(str(fixture), 11)  # echo "slept"
    reason, _, line = await rec.wait_stop()
    assert (reason, line) == ("breakpoint", 11)
    session.resume("continue")
    await asyncio.wait_for(rec.exit_event.wait(), 15)
    await session.stop()


@pytest.mark.asyncio
async def test_pause_while_running():
    rec = Recorder()
    session = await _launch(FIXTURES / "bash_loop.sh", rec)
    session.resume("continue")
    await asyncio.sleep(0.3)  # inside the slow loop
    session.pause()
    reason, path, line = await rec.wait_stop()
    assert reason == "pause"
    assert path.endswith("bash_loop.sh")
    session.resume("continue")
    await asyncio.wait_for(rec.exit_event.wait(), 15)
    await session.stop()


# --- Task 7: inspection — stack, locals, globals, evaluate ----------------


@pytest.mark.asyncio
async def test_stack_inside_nested_call():
    rec = Recorder()
    fixture = FIXTURES / "bash_functions.sh"
    session = await _launch(fixture, rec)
    await session.set_breakpoint(str(fixture), 3)  # echo "inner"
    session.resume("continue")
    await rec.wait_stop()
    frames = await session.stack()
    names = [f["func"] for f in frames]
    assert names[0] == "inner"
    assert "outer" in names
    assert names[-1] == "main"
    assert frames[0]["line"] == 3
    assert all(f["file"].endswith(".sh") for f in frames)
    await session.stop()


@pytest.mark.asyncio
async def test_locals_visible_in_innermost_frame():
    rec = Recorder()
    fixture = FIXTURES / "bash_functions.sh"
    session = await _launch(fixture, rec)
    await session.set_breakpoint(str(fixture), 3)  # after `local iv=99`
    session.resume("continue")
    await rec.wait_stop()
    lv = {v.name: v for v in await session.locals()}
    assert "iv" in lv
    assert "99" in lv["iv"].value
    await session.stop()


@pytest.mark.asyncio
async def test_locals_empty_at_top_level():
    rec = Recorder()
    fixture = FIXTURES / "bash_hello.sh"
    session = await _launch(fixture, rec)
    await session.set_breakpoint(str(fixture), 3)
    session.resume("continue")
    await rec.wait_stop()
    assert await session.locals() == []  # local -p fails outside a function
    await session.stop()


@pytest.mark.asyncio
async def test_globals_include_arrays_and_filter_internals():
    rec = Recorder()
    fixture = FIXTURES / "bash_arrays.sh"
    session = await _launch(fixture, rec)
    await session.set_breakpoint(str(fixture), 8)  # echo (final line)
    session.resume("continue")
    await rec.wait_stop()
    gv = {v.name: v for v in await session.globals_vars()}
    assert gv["fruits"].value == "array[3]"
    assert ("1", '"banana"') in gv["fruits"].children
    assert gv["prices"].value == "assoc[2]"
    assert "greeting" in gv
    assert not any(n.startswith("__tdb_") for n in gv)
    assert "BASH_VERSINFO" not in gv
    # regression: unanchored HIST/EPOCH/SHELL prefixes used to hide these
    # real user variables (HISTORY, EPOCH_START, SHELLCHECK_OPTS are not
    # bash specials, despite superficially resembling HISTFILE/EPOCHSECONDS/
    # SHELLOPTS).
    assert "HISTORY" in gv
    assert "EPOCH_START" in gv
    assert "SHELLCHECK_OPTS" in gv
    # FUNCNEST is a real bash special (shopt-adjacent recursion-depth
    # limit) and must still be filtered.
    assert "FUNCNEST" not in gv
    await session.stop()


# --- Final review (I1, I2, I3): wire-protocol correlation ids + globals ---


@pytest.mark.asyncio
async def test_late_reply_does_not_corrupt_next_request():
    """I1 regression: a timed-out request()'s late reply used to resolve
    whatever request happened to be pending by the time it finally
    arrived -- request()'s `finally` cleared self._pending on timeout, so
    _resp_loop had no way to tell a stale reply from the current one. In
    the field: `eval sleep 30` times out after 15s; the user opens
    Variables; the sleep's late "ok" resolves the globals request's
    future instead -> empty scope, and every reply after that is shifted
    by one, forever.

    Reproduced here: an eval that outlives a short 0.5s request() timeout
    is still being executed (synchronously, inline in the trap) by the
    harness when a second request is issued right behind it. The second
    request must get its OWN correct reply, not the stale sleep's.
    """
    rec = Recorder()
    session = await _launch(FIXTURES / "bash_hello.sh", rec)
    session.resume("step")
    await rec.wait_stop()
    with pytest.raises(asyncio.TimeoutError):
        await session.request("eval " + b64("sleep 3"), timeout=0.5)
    # The stale sleep-eval is still running in the harness (it blocks the
    # trap for the full 3s); queue the next request immediately behind it.
    rc, out = await session.evaluate("echo marker123")
    assert rc == 0
    assert "marker123" in out
    session.resume("continue")
    await asyncio.wait_for(rec.exit_event.wait(), 10)
    await session.stop()


@pytest.mark.asyncio
async def test_resp_loop_err_without_payload_does_not_hang():
    """I3 regression: the err branch of _resp_loop used to index fields[1]
    unconditionally (no length guard, unlike the ok branch), so a
    malformed/payload-less err frame raised IndexError inside the
    try/except and left the pending future unresolved for the full
    timeout instead of failing fast. Exercise the guarded path directly
    against a fake response stream.
    """
    rec = Recorder()
    session = BashSession(rec.on_output, rec.on_stop, rec.on_exit)
    session._resp_reader = asyncio.StreamReader()
    fut = asyncio.get_running_loop().create_future()
    session._pending = fut
    session._pending_id = 1
    session._resp_reader.feed_data(b"err 1\n")
    session._resp_reader.feed_eof()
    await session._resp_loop()
    assert fut.done()
    with pytest.raises(BashProtocolError):
        fut.result()


@pytest.mark.asyncio
async def test_strict_split_globals_vs_environment():
    """Strict split: unexported -> Globals only; exported (script's own
    AND inherited) -> Environment only; the two lists are disjoint.

    Also covers the script-touched annotation (final-review finding 1):
    the script's own export gets a "* " value marker and sorts ahead of
    untouched inherited noise; an untouched inherited var has no marker.
    """
    rec = Recorder()
    fixture = FIXTURES / "bash_env_scopes.sh"
    session = BashSession(rec.on_output, rec.on_stop, rec.on_exit)
    env = dict(os.environ)
    env["TDB_TEST_SENTINEL"] = "inherited-untouched"
    await session.launch(
        program=str(fixture), args=[], cwd=str(fixture.parent), env=env
    )
    await session.set_breakpoint(str(fixture), 4)  # echo "marker"
    session.resume("continue")
    await rec.wait_stop()

    gnames = {v.name for v in await session.globals_vars()}
    envvars_list = await session.environment_vars()
    envvars = {v.name: v for v in envvars_list}

    assert "plain_var" in gnames  # unexported -> Globals
    assert "plain_var" not in envvars
    assert "exported_var" in envvars  # script export -> Environment
    assert "exported_var" not in gnames
    assert envvars["exported_var"].value == '* "from-script"'  # touched marker
    assert "TDB_TEST_SENTINEL" in envvars  # inherited -> Environment
    assert "TDB_TEST_SENTINEL" not in gnames
    assert envvars["TDB_TEST_SENTINEL"].value == '"inherited-untouched"'  # no marker
    assert "PATH" in envvars  # real env deliberately shown
    assert not any(n.startswith(("__tdb_", "__TDB_")) for n in envvars)
    assert not any(n.startswith(("__tdb_", "__TDB_")) for n in gnames)
    assert gnames.isdisjoint(envvars)

    # touched entries (marker-prefixed values) sort ahead of untouched ones
    marked = [v.value.startswith("* ") for v in envvars_list]
    assert marked == sorted(marked, reverse=True)
    names_env = [v.name for v in envvars_list]
    assert names_env.index("exported_var") < names_env.index("TDB_TEST_SENTINEL")

    session.resume("continue")
    await asyncio.wait_for(rec.exit_event.wait(), 10)
    await session.stop()


@pytest.mark.asyncio
async def test_environment_touched_annotation_on_modified_inherited_var():
    """Item 1: a script export-MODIFYING an inherited var (not just
    creating a new one) also earns the touched marker -- exercises the
    value-diff path in _is_touched(), not just the "new name" path."""
    rec = Recorder()
    fixture = FIXTURES / "bash_env_scopes.sh"
    session = BashSession(rec.on_output, rec.on_stop, rec.on_exit)
    env = dict(os.environ)
    env["TDB_TEST_SENTINEL2"] = "orig"
    await session.launch(
        program=str(fixture), args=[], cwd=str(fixture.parent), env=env
    )
    await session.set_breakpoint(str(fixture), 4)  # echo "marker"
    session.resume("continue")
    await rec.wait_stop()

    rc, _ = await session.evaluate("export TDB_TEST_SENTINEL2=changed")
    assert rc == 0

    envvars = {v.name: v for v in await session.environment_vars()}
    assert envvars["TDB_TEST_SENTINEL2"].value == '* "changed"'

    session.resume("continue")
    await asyncio.wait_for(rec.exit_event.wait(), 10)
    await session.stop()


@pytest.mark.asyncio
async def test_set_a_vars_marked_touched():
    """`set -a` regression: every subsequent assignment is auto-exported,
    so it's brand-new to Environment (never in the launch snapshot) --
    both vars must come back touched/marked, not lost among inherited
    noise. Globals may end up empty since `set -a` never leaves an
    unexported var behind."""
    rec = Recorder()
    fixture = FIXTURES / "bash_set_a.sh"
    session = await _launch(fixture, rec)
    await session.set_breakpoint(str(fixture), 5)  # echo "marker"
    session.resume("continue")
    await rec.wait_stop()

    envvars = {v.name: v for v in await session.environment_vars()}
    assert envvars["var1"].value == '* "alpha"'
    assert envvars["var2"].value == '* "beta"'
    gnames = {v.name for v in await session.globals_vars()}
    assert "var1" not in gnames
    assert "var2" not in gnames

    session.resume("continue")
    await asyncio.wait_for(rec.exit_event.wait(), 10)
    await session.stop()


@pytest.mark.asyncio
async def test_eval_side_effects_persist_and_rc_reported():
    rec = Recorder()
    fixture = FIXTURES / "bash_hello.sh"
    session = await _launch(fixture, rec)
    await session.set_breakpoint(str(fixture), 3)  # before echo "x is $x"
    session.resume("continue")
    await rec.wait_stop()
    rc, out = await session.evaluate("x=42")  # mutate the debuggee
    assert rc == 0
    rc, out = await session.evaluate("echo x=$x; test -f /nonexistent")
    assert rc != 0
    assert "x=42" in out
    session.resume("continue")
    await asyncio.wait_for(rec.exit_event.wait(), 10)
    assert "x is 42" in rec.stdout()  # the mutation persisted
    await session.stop()


@pytest.mark.asyncio
async def test_harness_connects_over_fifo_paths_and_reports_pid(tmp_path):
    script = tmp_path / "t.sh"
    script.write_text("x=1\nx=2\n")
    rec = Recorder()
    s = BashSession(rec.on_output, rec.on_stop, rec.on_exit)
    try:
        await s.launch(program=str(script), args=[], cwd=str(tmp_path), env=None)
        assert s.debuggee_pid is not None
        os.kill(s.debuggee_pid, 0)  # alive, and it is bash itself
        assert "__TDB_CMD_FD" not in s._launch_env_snapshot
        assert s._launch_env_snapshot["__TDB_CMD_PATH"].endswith("cmd.fifo")
    finally:
        await s.stop()

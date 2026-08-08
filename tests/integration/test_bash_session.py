"""BashSession <-> tdb_harness.sh, no DAP layer involved."""

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from tdb.adapters.bash.session import BashSession, b64, canonical

FIXTURES = Path(__file__).parent / "fixtures"


def _bash_ok() -> bool:
    bash = shutil.which("bash")
    if not bash:
        return False
    out = subprocess.run(
        [bash, "-c", 'echo "${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"'],
        capture_output=True,
        text=True,
    ).stdout.strip()
    major, minor = (int(p) for p in out.split("."))
    return (major, minor) >= (4, 4)


pytestmark = pytest.mark.skipif(not _bash_ok(), reason="needs bash >= 4.4")


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
    lines = [l for _, l in await _stop_lines(session, rec, "step", 6)]
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

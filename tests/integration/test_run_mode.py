"""End-to-end run mode against a real debugpy session, in-process:
exit-code passthrough, output streaming, SIGUSR1 -> pause -> episode ->
detach -> resume -> second episode -> terminate."""

import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess

import pytest

from tdb import run_mode
from tdb.persist import TdbConfig
from tdb.session.state import SessionPhase
from tests.integration.ruby_adapter_harness import rdbg_ok

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="signal-driven run mode tests are POSIX-only"
)

perl_available = pytest.mark.skipif(
    shutil.which("perl") is None
    or subprocess.run(["perl", "-e", "require v5.18"]).returncode != 0,
    reason="perl >= 5.18 required",
)

ruby_available = pytest.mark.skipif(
    not rdbg_ok(), reason="needs rdbg (debug gem >= 1.9)"
)


# `os._exit` (not `sys.exit`) deliberately: debugpy's `userUnhandled`
# exception filter ignores SystemExit(0)/SystemExit(None) but NOT other
# codes (pydevd_process_net_command_json.py's `ignore_system_exit_codes
# = [0, None]` default) — `sys.exit(7)` would trip a real exception-stop
# and open a TUI episode instead of exiting cleanly, which is correct
# debugpy behavior but not what this passthrough test wants to exercise.
# `sys.stdout.flush()` matters because debugpy's output redirection
# still sees Python-level writes, but os._exit skips interpreter
# finalization (which would otherwise flush automatically).
EXIT_SCRIPT = "import os, sys\nprint('bye')\nsys.stdout.flush()\nos._exit(7)\n"
LOOP_SCRIPT = "import time\ni = 0\nwhile True:\n    i += 1\n    time.sleep(0.01)\n"


async def _wait_until(pred, timeout=20.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        assert loop.time() < deadline, "condition not met in time"
        await asyncio.sleep(0.05)


async def test_exit_code_and_output_passthrough(tmp_path, capfd):
    p = tmp_path / "exit7.py"
    p.write_text(EXIT_SCRIPT)
    code = await run_mode.run(program=str(p), config=TdbConfig())
    assert code == 7
    assert "bye" in capfd.readouterr().out


@perl_available
async def test_perl_compile_phase_runs_headless_without_tui_episode(tmp_path, capfd):
    """Regression: `tdb --run prog.pl` where prog.pl has compile-time
    statements (`use` lines). The compile-phase shim's re-trap after the
    adapter's entry `c` surfaced as a spurious "step" stop, which run
    mode treated as a signal to open the TUI. The program must instead
    run straight to completion with no episode."""
    from tdb.languages.perl import build_perl_profile

    p = tmp_path / "hello.pl"
    p.write_text('use strict;\nuse warnings;\nprint "phello\\n";\n')
    episodes = []

    async def fake_episode(controller, handler, console, config, program):
        episodes.append(controller.state.phase)
        return False

    code = await asyncio.wait_for(
        run_mode.run(
            program=str(p),
            config=TdbConfig(),
            profile=build_perl_profile(),
            tui_episode=fake_episode,
        ),
        timeout=60.0,
    )
    assert episodes == [], "spurious TUI episode during headless perl run"
    assert code == 0
    assert "phello" in capfd.readouterr().out


async def test_signal_pause_episode_detach_and_terminate(tmp_path):
    p = tmp_path / "loop.py"
    p.write_text(LOOP_SCRIPT)
    box = {}
    episodes = []

    def ready(controller):
        box["controller"] = controller

    async def fake_episode(controller, handler, console, config, program):
        episodes.append(controller.state.phase)
        assert controller.state.phase is SessionPhase.STOPPED
        assert console.last_stop is not None
        # Episode 1 detaches; episode 2 terminates.
        return len(episodes) == 1

    async def pulses():
        await _wait_until(
            lambda: (
                box.get("controller") is not None
                and box["controller"].state.phase is SessionPhase.RUNNING
            )
        )
        os.kill(os.getpid(), signal.SIGUSR1)
        await _wait_until(
            lambda: (
                len(episodes) == 1
                and box["controller"].state.phase is SessionPhase.RUNNING
            )
        )
        os.kill(os.getpid(), signal.SIGUSR1)

    pulse_task = asyncio.ensure_future(pulses())
    try:
        code = await asyncio.wait_for(
            run_mode.run(
                program=str(p),
                config=TdbConfig(),
                tui_episode=fake_episode,
                on_session_ready=ready,
            ),
            timeout=90.0,
        )
    finally:
        pulse_task.cancel()
    assert episodes and len(episodes) == 2
    assert code == 0
    assert box["controller"].state.is_terminated


async def test_run_cleans_up_when_tui_episode_raises(tmp_path):
    """A TUI episode that raises must not orphan the adapter+debuggee:
    run() should stop the controller before re-raising."""
    p = tmp_path / "loop.py"
    p.write_text(LOOP_SCRIPT)
    box = {}

    def ready(controller):
        box["controller"] = controller

    async def raising_episode(controller, handler, console, config, program):
        raise RuntimeError("boom")

    async def pulse():
        await _wait_until(
            lambda: (
                box.get("controller") is not None
                and box["controller"].state.phase is SessionPhase.RUNNING
            )
        )
        os.kill(os.getpid(), signal.SIGUSR1)

    pulse_task = asyncio.ensure_future(pulse())
    try:
        with pytest.raises(RuntimeError, match="boom"):
            await asyncio.wait_for(
                run_mode.run(
                    program=str(p),
                    config=TdbConfig(),
                    tui_episode=raising_episode,
                    on_session_ready=ready,
                ),
                timeout=90.0,
            )
    finally:
        pulse_task.cancel()
    assert box["controller"].state.is_terminated


@ruby_available
async def test_ruby_runs_headless_without_tui_episode(tmp_path, capfd):
    from tdb.languages.ruby import build_ruby_profile

    p = tmp_path / "hello.rb"
    p.write_text('puts "rhello"\n')
    episodes = []

    async def fake_episode(controller, handler, console, config, program):
        episodes.append(controller.state.phase)
        return False

    code = await asyncio.wait_for(
        run_mode.run(
            program=str(p),
            config=TdbConfig(),
            profile=build_ruby_profile(),
            tui_episode=fake_episode,
        ),
        timeout=60.0,
    )
    assert episodes == [], "spurious TUI episode during headless ruby run"
    assert code == 0
    assert "rhello" in capfd.readouterr().out


@ruby_available
async def test_ruby_exit_code_passthrough(tmp_path, capfd):
    from tdb.languages.ruby import build_ruby_profile

    p = tmp_path / "exit7.rb"
    p.write_text('puts "rbye"\n$stdout.flush\nexit 7\n')
    code = await asyncio.wait_for(
        run_mode.run(program=str(p), config=TdbConfig(), profile=build_ruby_profile()),
        timeout=60.0,
    )
    assert code == 7
    assert "rbye" in capfd.readouterr().out


THREAD_SCRIPT = """\
import threading, time
def spin():
    while True:
        time.sleep(0.01)
threading.Thread(target=spin, name="spinner", daemon=True).start()
while True:
    time.sleep(0.01)
"""

ASYNC_SCRIPT = """\
import asyncio
async def waiter(lock):
    async with lock:
        await asyncio.sleep(3600)
async def main():
    lock = asyncio.Lock()
    asyncio.create_task(waiter(lock), name="holder")
    await asyncio.sleep(0)
    asyncio.create_task(waiter(lock), name="blocked")
    while True:
        await asyncio.sleep(0.01)
asyncio.run(main())
"""

# The child touches CHILD_READY as its first act inside `child()`, so the
# marker's existence means "the child is parked in the frame the examine
# assertion looks for" — not merely "the child process exists". Attach
# (`debugpyAttach` -> a client in `controller._child_clients`) and arrival
# in `child()` are separate events and can land in either order; the test
# waits for both instead of guessing a settle time. The path is derived
# from __file__ so it works under every start method (`fork` inherits it,
# `spawn`/`forkserver` recompute it when they re-import __main__).
MP_SCRIPT = """\
import multiprocessing, os, time
CHILD_READY = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "child-ready"
)
def child():
    open(CHILD_READY, "w").close()
    while True:
        time.sleep(0.01)
if __name__ == "__main__":
    p = multiprocessing.Process(target=child, name="kid")
    p.start()
    while True:
        time.sleep(0.01)
"""


async def _examine_once(
    program: str,
    dests: list[str],
    captures_wanted: int = 1,
    settle: float = 1.0,
    ready=None,
    landed=None,
):
    """Run `program` headless, send SIGUSR2 `captures_wanted` times once
    the debuggee is running, then terminate via a TUI episode.

    Two hooks replace wall-clock guesses where the caller can name the
    condition it is actually waiting for:

    `ready(controller)` is polled after the debuggee reaches RUNNING and
    before the first SIGUSR2. It should return True once the debuggee has
    reached the state the capture is meant to snapshot. Callers that pass
    it get no `settle` sleep at all; callers that don't fall back to
    sleeping `settle` seconds.

    `landed(n)` is polled after the nth SIGUSR2 (1-based) and should
    return True once that capture's record has been written. Without it
    the harness sleeps a flat 1.0s per capture and hopes, which loses the
    race on a loaded runner: SIGUSR1 then terminates the session while
    the capture is still outstanding and the "ok" record is never
    written."""
    box = {}

    def on_ready(controller):
        box["controller"] = controller

    async def fake_episode(controller, handler, console, config, program):
        return False  # terminate

    async def pulses():
        await _wait_until(
            lambda: (
                box.get("controller") is not None
                and box["controller"].state.phase is SessionPhase.RUNNING
            )
        )
        if ready is None:
            await asyncio.sleep(settle)
        else:
            await _wait_until(lambda: ready(box["controller"]))
        for n in range(1, captures_wanted + 1):
            os.kill(os.getpid(), signal.SIGUSR2)
            if landed is None:
                await asyncio.sleep(1.0)
            else:
                await _wait_until(lambda n=n: landed(n))
        await _wait_until(lambda: box["controller"].state.phase is SessionPhase.RUNNING)
        os.kill(os.getpid(), signal.SIGUSR1)

    task = asyncio.create_task(pulses())
    try:
        code = await asyncio.wait_for(
            run_mode.run(
                program=program,
                config=TdbConfig(),
                tui_episode=fake_episode,
                on_session_ready=on_ready,
                examine_dests=dests,
            ),
            timeout=90.0,
        )
    finally:
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return code


def _stdout_records(capfd):
    out = capfd.readouterr().out
    return [json.loads(l) for l in out.splitlines() if l.startswith("{")]


def _ok_records(path):
    """Completed records in an examine log, newest last.

    A capture whose pause doesn't land inside run mode's `_PAUSE_TIMEOUT`
    emits a `status: "pending"` placeholder first and the real record
    later under the same `seq`, so "the first line in the file" is not
    reliably the snapshot under test. Filtering on status is also what
    makes the file safe to poll while the run is still in progress: a
    record only reaches "ok" once it is fully collected."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # a partially flushed line; it'll be whole next poll
        if rec.get("status") == "ok":
            out.append(rec)
    return out


async def test_examine_threads_to_stdout(tmp_path, capfd):
    p = tmp_path / "threads.py"
    p.write_text(THREAD_SCRIPT)
    await _examine_once(str(p), ["-"])
    recs = _stdout_records(capfd)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["schema"] == 1 and rec["status"] == "ok" and rec["trigger"] == "SIGUSR2"
    assert rec["language"] == "python" and rec["program"] == str(p)
    assert "landed_at" in rec and rec["elapsed_s"] >= 0
    parent = rec["processes"][0]
    assert parent["role"] == "parent" and isinstance(parent["pid"], int)
    names = {t["name"] for t in parent["threads"]}
    assert "spinner" in names
    spinner = next(t for t in parent["threads"] if t["name"] == "spinner")
    assert any(
        f["function"] == "spin" and f["file"] == str(p) for f in spinner["frames"]
    )
    assert "goroutines" not in rec and "rust_concurrency" not in rec


async def test_examine_asyncio_tasks(tmp_path, capfd):
    p = tmp_path / "tasks.py"
    p.write_text(ASYNC_SCRIPT)
    await _examine_once(str(p), ["-"])
    rec = _stdout_records(capfd)[0]
    tasks = {t["name"]: t for t in rec["processes"][0].get("tasks", [])}
    assert {"holder", "blocked"} <= set(tasks)
    assert tasks["blocked"]["awaiting"] == "Lock.acquire"


async def test_examine_multiprocessing_children(tmp_path, capfd):
    p = tmp_path / "mp.py"
    p.write_text(MP_SCRIPT)
    child_ready = tmp_path / "child-ready"
    log = tmp_path / "mp.jsonl"

    await _examine_once(
        str(p),
        [str(log)],
        # Snapshot only once the child is both attached and parked in
        # `child()`; a 3s sleep was standing in for both and lost the race
        # whenever the runner was busy.
        ready=lambda c: c.has_child_clients() and child_ready.exists(),
        landed=lambda n: len(_ok_records(log)) >= n,
    )

    recs = _ok_records(log)
    raw = log.read_text() if log.exists() else "<log never created>"
    assert recs, f"no completed examine record written; log contents:\n{raw}"
    rec = recs[0]
    assert rec["processes"][0]["role"] == "parent"
    children = [pr for pr in rec["processes"] if pr["role"] == "child"]
    # `any`, not `next`: under `spawn`/`forkserver` the parent can own more
    # than one tracked child (the forkserver itself attaches too), and
    # `_add_children` orders them by pid, so the first child is not
    # necessarily the worker running `child()`.
    assert children, rec
    assert all(isinstance(pr["pid"], int) for pr in children)
    assert any(
        f["function"] == "child"
        for pr in children
        for t in pr["threads"]
        for f in t["frames"]
    ), rec


async def test_examine_log_file_appends_across_captures(tmp_path, capfd):
    p = tmp_path / "threads.py"
    p.write_text(THREAD_SCRIPT)
    log = tmp_path / "hang.jsonl"
    log.write_text('{"pre":true}\n')
    await _examine_once(str(p), [str(log)], captures_wanted=2)
    lines = log.read_text().splitlines()
    assert lines[0] == '{"pre":true}'
    seqs = [json.loads(l)["seq"] for l in lines[1:]]
    assert seqs == [1, 2]
    captured = capfd.readouterr()
    assert f"examine #1 written to {log}" in captured.err
    assert f"examine #2 written to {log}" in captured.err
    assert not [l for l in captured.out.splitlines() if l.startswith("{")]

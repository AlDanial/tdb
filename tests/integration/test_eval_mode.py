"""End-to-end -e/--eval mode against a real debugpy session, in-process:
evaluate at each breakpoint hit, side effects persist, non-matching
stops continue, bad expressions don't kill the run, exit-code
passthrough."""

import asyncio
import os
import signal

import pytest

from tdb import eval_mode

# The eval point sits on the `total += i` line inside the loop, so it
# hits once per iteration.
LOOP_SCRIPT = """\
total = 0
for i in range(3):
    total += i
print(f"final={total}")
"""

# Line 3 is where the eval point mutates `limit`; without mutation the
# program prints small=yes, with limit lowered it prints small=no.
MUTATE_SCRIPT = """\
limit = 100
x = 5
print("checkpoint")
print("small=" + ("yes" if x < limit else "no"))
"""

RAISE_SCRIPT = """\
x = 1
print("before")
raise ValueError("boom")
"""

EXIT_SCRIPT = """\
import os, sys
print("bye")
sys.stdout.flush()
os._exit(7)
"""


async def test_evaluates_on_every_hit(tmp_path, capfd):
    p = tmp_path / "loop.py"
    p.write_text(LOOP_SCRIPT)
    code = await eval_mode.run(
        program=str(p), eval_points=[(str(p), 3, "print(f'{i=}')")]
    )
    out = capfd.readouterr().out
    assert code == 0
    assert "i=0" in out
    assert "i=1" in out
    assert "i=2" in out
    assert "final=3" in out


async def test_bare_expression_result_is_printed(tmp_path, capfd):
    p = tmp_path / "loop.py"
    p.write_text(LOOP_SCRIPT)
    code = await eval_mode.run(program=str(p), eval_points=[(str(p), 3, "i * 10")])
    out = capfd.readouterr().out
    assert code == 0
    assert "20" in out
    assert "final=3" in out


async def test_side_effects_persist(tmp_path, capfd):
    p = tmp_path / "mutate.py"
    p.write_text(MUTATE_SCRIPT)
    code = await eval_mode.run(program=str(p), eval_points=[(str(p), 3, "limit = 1")])
    out = capfd.readouterr().out
    assert code == 0
    assert "small=no" in out


async def test_bad_expression_does_not_kill_the_run(tmp_path, capfd):
    p = tmp_path / "loop.py"
    p.write_text(LOOP_SCRIPT)
    code = await eval_mode.run(
        program=str(p), eval_points=[(str(p), 3, "no_such_name")]
    )
    captured = capfd.readouterr()
    assert code == 0
    assert "final=3" in captured.out
    assert "no_such_name" in captured.out + captured.err


async def test_non_matching_stop_continues(tmp_path, capfd):
    # An uncaught exception stops the debuggee at a line with no eval
    # point; eval mode must continue (per design), letting the program
    # terminate with its normal traceback instead of hanging.
    p = tmp_path / "boom.py"
    p.write_text(RAISE_SCRIPT)
    code = await eval_mode.run(program=str(p), eval_points=[(str(p), 2, "x")])
    captured = capfd.readouterr()
    assert code != 0
    assert "before" in captured.out
    assert "ValueError" in captured.err + captured.out


async def test_exit_code_passthrough(tmp_path, capfd):
    p = tmp_path / "exit7.py"
    p.write_text(EXIT_SCRIPT)
    code = await eval_mode.run(program=str(p), eval_points=[(str(p), 2, "x = 1")])
    assert code == 7
    assert "bye" in capfd.readouterr().out


async def test_saved_breakpoints_are_never_loaded(tmp_path, capfd, monkeypatch):
    # Contract test (passes by construction today): eval mode must never
    # read the breakpoints file — a user's saved breakpoint would
    # silently pause the headless run.
    from tdb import persist

    def _boom(*a, **kw):
        raise AssertionError("eval mode read the breakpoints file")

    monkeypatch.setattr(persist, "load_breakpoints", _boom)
    p = tmp_path / "loop.py"
    p.write_text(LOOP_SCRIPT)
    code = await eval_mode.run(program=str(p), eval_points=[(str(p), 4, "total")])
    assert code == 0
    assert "3" in capfd.readouterr().out


FOREVER_SCRIPT = """\
import time
x = 1
while x:
    time.sleep(0.05)
"""

MULTIPROC_SCRIPT = """\
import multiprocessing as mp

def worker(n):
    tag = n * 11
    print(f"worker{n}={tag}", flush=True)

if __name__ == "__main__":
    ps = [mp.Process(target=worker, args=(i,)) for i in (1, 2)]
    for p in ps:
        p.start()
    for p in ps:
        p.join()
    print("done", flush=True)
"""


@pytest.mark.skipif(os.name == "nt", reason="signal delivery test is POSIX-only")
async def test_sigint_stops_cleanly(tmp_path, capfd):
    # Without armed signal handling, Ctrl-C raises KeyboardInterrupt
    # through the loop and can orphan the session-detached debuggee;
    # eval mode must stop the session and exit 130 instead.
    p = tmp_path / "forever.py"
    p.write_text(FOREVER_SCRIPT)
    loop = asyncio.get_running_loop()
    # 20s (matching the "generous ceiling for adapter spawn + debuggee
    # start" used elsewhere, e.g. test_dap_session.py's WAIT): under a
    # loaded CI runner (Docker + ptrace/seccomp flags), debugpy attach
    # can take longer than a few seconds, and firing SIGINT before
    # eval_mode.run() reaches its own signal handler installation lets
    # the interrupt fall through to Python's default handler instead —
    # an uncaught KeyboardInterrupt that kills the whole pytest run
    # rather than just this test.
    handle = loop.call_later(20.0, os.kill, os.getpid(), signal.SIGINT)
    try:
        code = await asyncio.wait_for(
            eval_mode.run(program=str(p), eval_points=[(str(p), 2, "7")]),
            timeout=90,
        )
    finally:
        handle.cancel()
    captured = capfd.readouterr()
    assert code == 130
    assert "interrupted" in captured.err
    assert "7" in captured.out  # the eval point fired before the interrupt


async def test_multiprocess_children_all_evaluate(tmp_path, capfd):
    # Two children hit the eval line near-simultaneously; the stop
    # queue + batch drain must evaluate BOTH before the continue-all
    # resumes every process (a single coalescing Event lost one).
    p = tmp_path / "mp_prog.py"
    p.write_text(MULTIPROC_SCRIPT)
    code = await asyncio.wait_for(
        eval_mode.run(program=str(p), eval_points=[(str(p), 4, "print(f'hit={n}')")]),
        timeout=120,
    )
    out = capfd.readouterr().out
    assert code == 0
    assert "hit=1" in out
    assert "hit=2" in out
    assert "worker1=11" in out
    assert "worker2=22" in out
    assert "done" in out


async def test_adapter_not_found(tmp_path, capsys):
    from tdb.languages.base import AdapterNotFoundError

    async def boom(*a, **kw):
        raise AdapterNotFoundError("install the thing")

    p = tmp_path / "prog.py"
    p.write_text("print('hi')\n")

    import tdb.session.controller as controller_mod

    orig = controller_mod.DebugController.start
    controller_mod.DebugController.start = boom
    try:
        code = await eval_mode.run(program=str(p), eval_points=[(str(p), 1, "1")])
    finally:
        controller_mod.DebugController.start = orig
    assert code == 2
    assert "install the thing" in capsys.readouterr().err

"""`tdb --eval`: headless run that evaluates expressions at lines.

Each `-e FILE:LINE EXPR` becomes a transient breakpoint; every time the
debuggee reaches one, EXPR is evaluated in the stopped frame (DAP
`evaluate`, context "repl" — side effects persist in the debuggee) and
the run continues. No TUI ever opens: debuggee output streams to the
terminal, evaluation results print to stdout, evaluation errors to
stderr. Stops that match no eval point (e.g. an uncaught-exception
stop) are continued untouched, and tdb-induced pause-all stops are
never treated as hits. Saved breakpoints are never loaded — only the
TUI reads the breakpoints file. Ctrl-C stops the debuggee and exits
130. If the session ends without a real exit code (`terminated` with
no `exited` event — hard crash, adapter death), tdb exits 1.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from tdb.run_mode import (
    ConsoleRunHandler,
    _arm_signals,
    _disarm_signals,
    configure_when_initialized,
    start_session,
    stop_session_on_error,
)
from tdb.session.controller import DebugController

if TYPE_CHECKING:
    from tdb.dap.client import DAPClient
    from tdb.languages.base import LanguageProfile

log = logging.getLogger(__name__)

# (abs_path, requested_line, expression)
EvalPoint = tuple[str, int, str]


class _EvalRunHandler(ConsoleRunHandler):
    """ConsoleRunHandler plus a per-stop queue.

    The base class's single `stopped` Event coalesces simultaneous
    stops from multiple processes, and a racing parent `continued`
    event can clear it after a child's stop set it (lost wakeup). Each
    on_stopped call instead enqueues (client, thread_id, reason).
    controller.active_client is captured synchronously: both the
    parent's _on_stopped and the child manager's on_child_stopped set
    _active_client immediately before calling the handler in the same
    call stack, so at this moment it identifies the process that
    stopped — even if the pause-all machinery flips it again later.
    """

    def __init__(self) -> None:
        super().__init__()
        self.controller: DebugController | None = None  # late-bound
        self.stops: asyncio.Queue[tuple[DAPClient | None, int | None, str]] = (
            asyncio.Queue()
        )

    def on_stopped(
        self,
        thread_id: int | None,
        reason: str,
        description: str | None = None,
        text: str | None = None,
    ) -> None:
        super().on_stopped(thread_id, reason, description, text)
        client = self.controller.active_client if self.controller is not None else None
        self.stops.put_nowait((client, thread_id, reason))


def _same_file(a: str, b: str) -> bool:
    """Compare an eval-point path with an adapter-reported stop path.

    resolve() + normcase covers symlinks and Windows case differences;
    an unresolvable path (deleted file, remote-style path) is simply
    not a match rather than an error.
    """
    try:
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(
            str(Path(b).resolve())
        )
    except OSError:
        return False


def _matching_points(
    eval_points: list[EvalPoint],
    bound_lines: dict[str, dict[int, int]],
    stop_path: str,
    stop_line: int,
) -> list[EvalPoint]:
    """Eval points hit by a stop at (stop_path, stop_line).

    A point matches on its requested line or on the line the adapter
    actually bound the breakpoint to (state.bound_breakpoint_lines,
    recorded from the setBreakpoints response): non-Python languages
    get no statement snapping, and gdb-style adapters move a breakpoint
    on a declaration to the next executable line — every stop then
    reports the moved line, which must still count as a hit.
    """
    hits: list[EvalPoint] = []
    for path, line, expr in eval_points:
        effective = bound_lines.get(path, {}).get(line, line)
        if stop_line in (line, effective) and _same_file(path, stop_path):
            hits.append((path, line, expr))
    return hits


async def _handle_stop(
    controller: DebugController,
    client: DAPClient | None,
    thread_id: int | None,
    eval_points: list[EvalPoint],
) -> None:
    """Locate one stop and evaluate any matching points.

    Talks only to the per-stop captured `client` and its own top frame:
    fetch_stop_info would cost ~5 round-trips (threads, full stack,
    scopes, every variable) and read controller._active_client, which
    the pause-all machinery can flip mid-iteration under subProcess —
    sending the evaluate to the wrong process. One stackTrace(levels=1)
    yields path, line, and frame_id together.
    """
    if client is None:
        return
    tid = thread_id
    if tid is None:
        # `stopped` events may omit threadId (optional per DAP).
        try:
            threads = await client.threads()
        except Exception:
            return
        if not threads:
            return
        tid = threads[0].id
    try:
        frames = await client.stack_trace(tid, levels=1)
    except Exception:
        return  # process resumed or exited under us — nothing to evaluate
    if not frames or frames[0].source is None or not frames[0].source.path:
        return
    top = frames[0]
    hits = _matching_points(
        eval_points, controller.state.bound_breakpoint_lines, top.source.path, top.line
    )
    for path, line, expr in hits:
        # Result lines go to stdout (skipping empty/None results so
        # statement-style expressions like `print(...)` or `x = 1`
        # don't emit noise); adapter errors go to stderr and never
        # abort the run — matching the Evaluate console, where a typo
        # isn't fatal.
        try:
            result, _ = await client.evaluate(expr, frame_id=top.id, context="repl")
        except Exception as e:
            print(f"tdb: -e {path}:{line}: {e}", file=sys.stderr)
            continue
        # Statement-style expressions (`print(...)`, assignments) and
        # None-valued ones return an empty result under debugpy's repl
        # context — suppress those; a non-empty result is a value the
        # user asked to see (including a literal "None" string from an
        # adapter that reports one).
        if result and result.strip():
            print(result)


async def _resume_client(client: DAPClient | None, thread_id: int | None) -> None:
    """Resume exactly the one process this stop belonged to.

    Eval mode resumes per-client, not via controller.continue_() (which
    resumes every known client): under subProcess, a blanket resume
    after one child's hit cascades to a sibling child that stopped in
    the same tick but hasn't been evaluated yet, stealing its stop
    before the loop can read it. A missing threadId (optional per DAP)
    is resolved from threads() so the resume never silently no-ops and
    hangs the run.
    """
    if client is None:
        return
    tid = thread_id
    if tid is None:
        try:
            threads = await client.threads()
        except Exception:
            return
        if not threads:
            return
        tid = threads[0].id
    try:
        await client.continue_nowait(tid)
    except Exception:
        pass  # already resumed, or the process exited under us


def _final_exit_code(console: ConsoleRunHandler) -> int:
    """Exit code once the session has ended.

    `terminated` can arrive without `exited` (hard crash, adapter
    death, SIGKILL); exit_code is then still None, and reporting 0
    would tell a `tdb -e ... && next-step` pipeline the run succeeded.
    """
    if console.exit_code is not None:
        return console.exit_code
    return 1


async def _next_wake(
    console: _EvalRunHandler, interrupt: asyncio.Event
) -> tuple[str, tuple[DAPClient | None, int | None, str] | None]:
    """Wait for the next actionable event: ("exited"|"interrupt", None)
    or ("stop", queue item). Priority exited > interrupt > stop — a
    stop dequeued in the same tick as exit/interrupt is dropped, since
    the session is ending either way."""
    stop_get = asyncio.ensure_future(console.stops.get())
    exited = asyncio.ensure_future(console.exited.wait())
    intr = asyncio.ensure_future(interrupt.wait())
    done, pending = await asyncio.wait(
        {stop_get, exited, intr}, return_when=asyncio.FIRST_COMPLETED
    )
    for t in pending:
        t.cancel()
    if exited in done:
        return ("exited", None)
    if intr in done:
        return ("interrupt", None)
    return ("stop", stop_get.result())


async def run(
    program: str,
    args: list[str] | None = None,
    cwd: str | None = None,
    eval_points: list[EvalPoint] | None = None,
    just_my_code: bool = True,
    python: str | None = None,
    sub_process: bool = True,
    profile: LanguageProfile | None = None,
) -> int:
    """Run `program` headless, evaluating each eval point when hit.

    Returns tdb's exit code (the debuggee's when it exits normally;
    130 on Ctrl-C; 1 when the session dies without an exit code).
    """
    eval_points = eval_points or []
    console = _EvalRunHandler()
    controller = DebugController(console, profile=profile)
    console.controller = controller
    # Transient (persist=False): eval points must never reach the
    # breakpoints file.
    controller.state.install_cli_breakpoints(
        [(path, line, False) for path, line, _ in eval_points]
    )

    bail = await start_session(
        controller,
        program=program,
        args=args,
        cwd=cwd or str(Path.cwd()),
        stop_on_entry=False,
        just_my_code=just_my_code,
        python=python,
        sub_process=sub_process,
    )
    if bail is not None:
        return bail

    async with stop_session_on_error(controller):
        await configure_when_initialized(console, controller)

        # Ctrl-C must stop the session cleanly: the adapter+debuggee
        # run in their own process group (start_new_session=True), so a
        # raw KeyboardInterrupt tearing down our loop would orphan them
        # with no terminal signal able to reach them.
        loop = asyncio.get_running_loop()
        interrupt = asyncio.Event()
        installed = _arm_signals(loop, interrupt.set)
        try:
            while True:
                event, stop = await _next_wake(console, interrupt)
                if event == "exited":
                    return _final_exit_code(console)
                if event == "interrupt":
                    print("tdb: interrupted — stopping the program", file=sys.stderr)
                    await controller.stop()
                    return 130
                # Drain the whole burst so every process that stopped in
                # this tick is evaluated and resumed on its own — under
                # subProcess two children can hit in the same tick.
                batch = [stop]
                while True:
                    try:
                        batch.append(console.stops.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                for client, tid, reason in batch:
                    if console.exited.is_set():
                        break
                    # reason == "pause" is a tdb-induced pause-all stop
                    # (the controller pauses the parent when a child
                    # stops), never a real hit — resume it, don't
                    # evaluate.
                    if reason != "pause":
                        await _handle_stop(controller, client, tid, eval_points)
                    await _resume_client(client, tid)
                if console.exited.is_set():
                    return _final_exit_code(console)
        finally:
            _disarm_signals(loop, installed, ignore=False)

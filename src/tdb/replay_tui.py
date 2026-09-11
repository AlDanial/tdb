"""Replay a --record session inside the live TUI (`tdb --replay-tui`).

The headless engine (replay.py) feeds records through the JSON-RPC
dispatch table. That is invisible by design. This driver instead
re-enters each record at the TUI's own gesture layer — the same handlers
a keypress or click would reach — so the Code, Stack, Variables and
Evaluate panels show exactly what the original session showed.

The driver runs as a Textual worker owned by `TdbApp` (see
`TdbApp.on_mount`). Blocking actions (`next`, `continue`, ...) wait on
`TdbApp.stop_settled`, which the TUI's stopped-event handler sets only
after the panels have been refreshed for a *final* stop — intermediate
statement-mode stops never set it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Awaitable, Callable

from tdb.replay import Recording
from tdb.server.handlers import parse_file_line
from tdb.session.state import SessionPhase

if TYPE_CHECKING:
    from tdb.app import TdbApp

log = logging.getLogger(__name__)

# Recorded action name -> CodeView.DebugAction name.
_STEP_GESTURES = {
    "next": "step_over",
    "step_in": "step_in",
    "step_out": "step_out",
    "continue": "continue_",
}

STILL_RUNNING_MSG = "still running (no stop within the replay timeout)"


class ReplayDriver:
    """Feed a `Recording` through a running `TdbApp`, one gesture at a time."""

    def __init__(
        self,
        recording: Recording,
        *,
        label: str,
        replay_timeout: float = 30.0,
        announce: bool = True,
        interval: float | None = None,
    ) -> None:
        self.recording = recording
        self.label = label
        self.replay_timeout = replay_timeout
        # Fixed delay before every action (`--replay-interval`); None
        # means reproduce the recorded gaps.
        self.interval = interval
        # Toast each action as it is performed (`--replay-quiet` turns
        # this off; error toasts and the final summary always show).
        self.announce = announce
        self.errors = 0
        # One line per command, mirroring the headless transcript. Kept
        # in memory (the TUI owns the terminal) for tests and post-run
        # diagnostics.
        self.transcript: list[str] = []
        self.finished = asyncio.Event()
        # Injection point so tests can drop the recorded pacing.
        self.sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    # --- main loop ------------------------------------------------------

    async def run(self, app: TdbApp) -> int:
        """Replay every record. Returns the number of failed commands."""
        records = self.recording.records
        total = len(records)
        original_title = app.title
        try:
            if not await self._wait_settled(app):
                self._report(app, "entry stop never arrived", ok=False)
                return self.errors
            prev_t = 0.0
            for i, rec in enumerate(records, start=1):
                delay = (
                    self.interval if self.interval is not None else rec["t"] - prev_t
                )
                if delay > 0:
                    await self.sleep(delay)
                prev_t = rec["t"]
                app.title = f"{original_title} ▶ {self.label} {i}/{total}"
                if self.announce:
                    app.notify(_describe(rec), title="Replay", timeout=2)
                try:
                    ok, detail = await self._dispatch(app, rec)
                except Exception as e:  # never let one record abort the replay
                    log.exception("replay: %s failed", rec["action"])
                    ok, detail = False, f"internal error: {e!r}"
                self.transcript.append(
                    f"[{rec['t']:8.3f}] {_describe(rec)} -> "
                    f"{'ok' if ok else 'ERROR'}: {detail}"
                )
                if not ok:
                    self._report(app, f"{_describe(rec)}: {detail}", ok=False)
                if rec["action"] == "quit":
                    break
        finally:
            app.title = original_title
            app.notify(
                f"Replay finished: {total} commands, {self.errors} errors",
                title="Replay",
                severity="error" if self.errors else "information",
                timeout=6,
            )
            self.finished.set()
        return self.errors

    def _report(self, app: TdbApp, detail: str, *, ok: bool) -> None:
        if not ok:
            self.errors += 1
            app.notify(detail, title="Replay", severity="error", timeout=5)

    # --- dispatch -------------------------------------------------------

    async def _dispatch(self, app: TdbApp, rec: dict) -> tuple[bool, str]:
        action, params = rec["action"], list(rec["params"])
        if action in _STEP_GESTURES:
            return await self._step(app, _STEP_GESTURES[action])
        if action == "evaluate":
            return await self._evaluate(app, params)
        if action in ("stack_up", "stack_down"):
            return await self._stack(app, up=action == "stack_up")
        if action == "set_breakpoint":
            return await self._set_breakpoint(app, params)
        if action == "remove_breakpoint":
            return await self._remove_breakpoint(app, params)
        if action == "inspect":
            return await self._inspect(app, params)
        if action == "restart":
            return await self._restart(app)
        if action == "quit":
            await app.action_quit_debugger()
            return True, "quit"
        return False, f"{action} is not replayable in the TUI"

    async def _step(self, app: TdbApp, gesture: str) -> tuple[bool, str]:
        from tdb.widgets.code_view import CodeView

        app.stop_settled.clear()
        await app.on_code_view_debug_action(CodeView.DebugAction(gesture))
        if not await self._wait_settled(app):
            return True, STILL_RUNNING_MSG
        return True, _location(app)

    async def _evaluate(self, app: TdbApp, params: list) -> tuple[bool, str]:
        from tdb.widgets.evaluate_console import EvaluateConsole

        if not params:
            return False, "evaluate needs an expression"
        expression = str(params[0])
        console = app.query_one("#eval-console", EvaluateConsole)
        console.echo_expression(expression)
        await app.on_evaluate_console_evaluate_requested(
            EvaluateConsole.EvaluateRequested(expression)
        )
        return True, expression

    async def _stack(self, app: TdbApp, *, up: bool) -> tuple[bool, str]:
        moved = await app._navigate_stack(up)
        if not moved:
            return (
                False,
                "already at top of stack" if up else "already at bottom of stack",
            )
        return True, _location(app)

    # --- breakpoints ----------------------------------------------------

    async def _set_breakpoint(self, app: TdbApp, params: list) -> tuple[bool, str]:
        from tdb.widgets.code_view import CodeView

        try:
            path, line = parse_file_line(str(params[0]))
        except (IndexError, ValueError):
            return False, "params[0] must be 'file:line'"
        condition = str(params[1]) if len(params) > 1 and params[1] else None
        hit_condition = str(params[2]) if len(params) > 2 and params[2] else None
        if not _has_breakpoint(app, path, line):
            await app.on_code_view_breakpoint_toggled(
                CodeView.BreakpointToggled(path, line)
            )
        if condition or hit_condition:
            await app.on_tdb_app__apply_breakpoint_condition(
                app._ApplyBreakpointCondition(path, line, condition, hit_condition)
            )
        return True, f"{path}:{line}"

    async def _remove_breakpoint(self, app: TdbApp, params: list) -> tuple[bool, str]:
        from tdb.widgets.code_view import CodeView

        try:
            path, line = parse_file_line(str(params[0]))
        except (IndexError, ValueError):
            return False, "params[0] must be 'file:line'"
        if not _has_breakpoint(app, path, line):
            return False, f"no breakpoint at {path}:{line}"
        await app.on_code_view_breakpoint_toggled(
            CodeView.BreakpointToggled(path, line)
        )
        return True, f"{path}:{line}"

    # --- variable expansion ---------------------------------------------

    async def _inspect(self, app: TdbApp, params: list) -> tuple[bool, str]:
        """Expand the Variables-tree node whose DAP evaluateName matches."""
        from tdb.widgets.variable_view import VariableView

        if not params:
            return False, "inspect needs an expression"
        name = str(params[0])
        ref = next(
            (
                v.variables_reference
                for vars_ in app.controller.state.variables.values()
                for v in vars_
                if v.evaluate_name == name and v.variables_reference > 0
            ),
            None,
        )
        if ref is None:
            return False, f"no expandable variable named {name!r} is displayed"
        var_view = app.query_one("#variable-view", VariableView)
        node = _find_node(var_view.root, ref)
        if node is None:
            return False, f"{name!r} is not in the Variables tree"
        node.expand()
        # Expansion posts LazyLoadVariables; wait for the placeholder
        # child to be replaced so the next record sees the loaded tree.
        for _ in range(40):
            children = list(node.children)
            if not (len(children) == 1 and str(children[0].label) == "..."):
                break
            await asyncio.sleep(0.05)
        return True, name

    # --- restart --------------------------------------------------------

    async def _restart(self, app: TdbApp) -> tuple[bool, str]:
        if not app.controller.supports_restart:
            return False, "restart is not available in remote-attach mode"
        old_controller = app.controller
        app.stop_settled.clear()
        worker = app._restart_session()
        await worker.wait()
        if not await self._wait_settled(app, require_new=old_controller):
            return True, STILL_RUNNING_MSG
        return True, _location(app)

    # --- stop synchronisation ------------------------------------------

    async def _wait_settled(self, app: TdbApp, *, require_new=None) -> bool:
        """Wait until the TUI has rendered a final stop (or termination).

        `stop_settled` can be set by a stale event (an old session's
        `terminated` racing a restart), so the session phase is verified
        on every wake-up and a spurious set is cleared and waited out.
        With `require_new`, a stop only counts once `app.controller` is
        no longer that object (restart has swapped in a fresh session).
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.replay_timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(app.stop_settled.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return False
            phase = app.controller.state.phase
            fresh = require_new is None or app.controller is not require_new
            if fresh and phase in (SessionPhase.STOPPED, SessionPhase.TERMINATED):
                return True
            app.stop_settled.clear()


def _has_breakpoint(app: TdbApp, path: str, line: int) -> bool:
    return any(bp.line == line for bp in app.controller.state.breakpoints.get(path, []))


def _find_node(node, ref: int):
    for child in node.children:
        if child.data == ref:
            return child
        found = _find_node(child, ref)
        if found is not None:
            return found
    return None


def _describe(rec: dict) -> str:
    return f"{rec['action']} {json.dumps(rec['params'])}"


def _location(app: TdbApp) -> str:
    state = app.controller.state
    if state.is_terminated:
        return "program terminated"
    loc, line = state.get_stop_location()
    return f"{loc}:{line}"

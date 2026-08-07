"""DAP event handler logic, lifted out of TdbApp.

The App keeps thin `on_dap_*` stubs because Textual auto-dispatches
based on method-name conventions on the App class. Each stub forwards
to a method here that holds the actual behavior. Helpers like the
exception-modal builder and the stderr-traceback parser also live here
since they're only called from this layer.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from tdb.session.messages import DapOutput, DapStopped
from tdb.widgets.console_view import ConsoleView
from tdb.widgets.modals import DEFAULT_TRACEBACK_HEADER, _TracebackModal

if TYPE_CHECKING:
    from tdb.app import TdbApp

log = logging.getLogger(__name__)


class DapEventCoordinator:
    """Owns every `on_dap_*` body plus the helpers they call.

    A reference to the App is held so this layer can:
      - read/write controller state,
      - push the traceback modal,
      - call the App's `_update_ui_state`, `_update_thread_count`,
        `_fetch_process_count`, `_fetch_async_task_count` (these stay
        on the App because they are themselves Textual workers),
      - read/write `self.app._stderr_buffer` and
        `self.app.panels.exception_modal_shown` (cross-cut state
        owned by the App via the UIPanels registry).
    """

    def __init__(self, app: TdbApp) -> None:
        self.app = app

    # --- initialized / configure ---------------------------------------

    async def do_configure(self) -> None:
        try:
            await self.app.controller.do_configure()
            log.info("do_configure completed")
            self.app._update_ui_state()
        except Exception:
            log.exception("Failed to launch")
            self.app.sub_title = "Launch failed"

    # --- stopped --------------------------------------------------------

    async def on_stopped(self, message: DapStopped) -> None:
        log.info(
            "on_dap_stopped called thread=%s reason=%s",
            message.thread_id,
            message.reason,
        )
        ctrl = self.app.controller
        try:
            # state.is_running, state.stop_reason, state.current_thread_id
            # were already set by controller._on_stopped before this async
            # handler ran — the controller is now the single state authority.
            await ctrl.fetch_stop_info()
            if self._stopped_inside_breakpoint_hook():
                # tdb.breakpoint() pauses inside breakpoint_hook.breakpoint;
                # step out so the user lands in their own caller frame.
                await ctrl.step_out()
                return
            # Statement-granularity step: if the cursor is still inside the
            # multi-line statement that began this step, fire another DAP
            # step and skip the UI refresh — a follow-up `stopped` event
            # will re-enter this handler with the new position.
            if await ctrl.maybe_continue_statement_step():
                return
            await ctrl.cleanup_run_to_cursor()
        except Exception:
            log.exception("Error handling stopped event")
        # Always update UI, even if fetch_stop_info partially failed
        self.app._update_ui_state()
        self.app._update_thread_count()
        self.app._fetch_process_count()
        self.app._fetch_async_task_count()

        if message.reason == "exception":
            self.app.panels.exception_modal_shown = True
            self._show_exception_modal(message)

    def _stopped_inside_breakpoint_hook(self) -> bool:
        """True if the active stop is inside `tdb.breakpoint()`'s helper."""
        ctrl = self.app.controller
        if ctrl.profile.id != "python":
            return False
        if not ctrl.is_remote_attach:
            return False
        frames = ctrl.state.stack_frames
        if not frames:
            return False
        top = frames[0]
        src = top.source.path if top.source else None
        if not src:
            return False
        return os.path.basename(src) == "breakpoint_hook.py"

    def _show_exception_modal(self, message: DapStopped) -> None:
        """Show a modal with the full exception traceback."""
        state = self.app.controller.state

        desc = message.description or "Exception"
        text = message.text or ""
        exception_text = f"{desc}: {text}" if text else desc

        # Build traceback from stack frames (bottom-up, like Python tracebacks)
        lines = []
        for frame in reversed(state.stack_frames):
            source = (
                frame.source.path if frame.source and frame.source.path else "<unknown>"
            )
            lines.append(f'  File "{source}", line {frame.line}, in {frame.name}')
        frames_text = "\n".join(lines) if lines else "  <no frames available>"

        def on_dismiss(result: str | None) -> None:
            if result == "restart":
                self.app._restart_session()

        can_restart = self.app.controller.supports_restart
        self.app.panels.last_exception_text = exception_text
        self.app.panels.last_frames_text = frames_text
        self.app.panels.last_can_restart = can_restart
        self.app.panels.last_header = DEFAULT_TRACEBACK_HEADER
        self.app.push_screen(
            _TracebackModal(
                exception_text,
                frames_text,
                can_restart=can_restart,
            ),
            callback=on_dismiss,
        )

    # --- continued ------------------------------------------------------

    def on_continued(self) -> None:
        try:
            # state.is_running and clear_frame_data() are now set by
            # controller._on_continued (single state authority). Only
            # App-level state (stderr buffer, exception-modal flag)
            # needs to be reset here.
            self.app._stderr_buffer.clear()
            self.app.panels.exception_modal_shown = False
            self.app._update_ui_state()
        except Exception:
            log.exception("Error handling continued event")

    # --- terminated -----------------------------------------------------

    async def on_terminated(self) -> None:
        log.info("on_dap_terminated called")
        try:
            # state.is_terminated and state.is_running are set by
            # controller._on_terminated (single state authority). This
            # handler only does TUI-side cleanup.
            if not self.app.panels.exception_modal_shown:
                # debugpy may still be delivering OutputEvents for late stderr
                # (chained tracebacks in particular span many lines). Wait for
                # the buffer to stabilize before parsing, otherwise the modal
                # shows a partial traceback.
                await self._wait_for_stderr_quiescent()
                exit_code = await self._wait_for_exit_code()
                self._check_stderr_traceback(exit_code)
            self.app._update_ui_state()
        except Exception:
            log.exception("Error handling terminated event")

    async def _wait_for_stderr_quiescent(
        self,
        quiet_for: float = 0.15,
        max_wait: float = 1.5,
    ) -> None:
        """Sleep in short ticks until stderr stops growing (or max_wait elapses)."""
        deadline = asyncio.get_event_loop().time() + max_wait
        last_len = len(self.app._stderr_buffer)
        last_change = asyncio.get_event_loop().time()
        while True:
            await asyncio.sleep(0.05)
            now = asyncio.get_event_loop().time()
            cur_len = len(self.app._stderr_buffer)
            if cur_len != last_len:
                last_len = cur_len
                last_change = now
            if now - last_change >= quiet_for:
                return
            if now >= deadline:
                return

    async def _wait_for_exit_code(
        self,
        max_wait: float = 0.3,
    ) -> int | None:
        """Bounded wait for `state.last_exit_code` to be populated.

        `terminated` and `exited` are separate DAP events and this coordinator
        runs on `terminated`. Measured empirically (see task report): debugpy
        emits `exited` BEFORE `terminated`, so `last_exit_code` is already set
        by the time this runs. The perl adapter emits them in the opposite
        order (`terminated` then `exited`, written back-to-back with no
        `await` between them), so `exited` can still be in flight -- poll
        briefly rather than assume. Falls back to whatever is present (often
        still None) once `max_wait` elapses, e.g. if the adapter never sends
        `exited` at all (either event is independently sufficient -- see
        DebugController._on_terminated/_on_exited).
        """
        state = self.app.controller.state
        deadline = asyncio.get_event_loop().time() + max_wait
        while state.last_exit_code is None:
            if asyncio.get_event_loop().time() >= deadline:
                break
            await asyncio.sleep(0.02)
        return state.last_exit_code

    def _check_stderr_traceback(self, exit_code: int | None = None) -> None:
        """If stderr contains a fatal error the active profile's parser
        recognizes, show it in a modal, build synthetic stack frames,
        and navigate Code View to the deepest frame.

        `exit_code` is the debuggee's real DAP `exited` code if known by
        the time this runs (see `_wait_for_exit_code`); passed straight
        through to the profile's parser, which may ignore it (python) or
        use it to gate fatality (perl)."""
        from tdb.dap.types import Source, StackFrame

        parse_error = self.app.controller.profile.presentation.parse_error
        if parse_error is None:
            return

        stderr = "".join(self.app._stderr_buffer)
        parsed = parse_error(stderr, exit_code)
        if parsed is None:
            return

        # Build synthetic stack frames (reversed: deepest = index 0, like
        # DAP). ParsedError.frames is OUTERMOST-first (source order); the
        # parser does not do this inversion, so it happens here.
        state = self.app.controller.state
        synthetic_frames: list[StackFrame] = []
        for i, frame in enumerate(reversed(parsed.frames)):
            synthetic_frames.append(
                StackFrame(
                    id=i,
                    name=frame.func or "<module>",
                    source=Source(path=frame.path, name=os.path.basename(frame.path)),
                    line=frame.line,
                )
            )

        if synthetic_frames:
            # Synthetic: the debuggee is already gone (this runs after a
            # terminated event), so debugpy has no frame ids these would
            # map to. Flag them so any straggling evaluate routes around
            # `current_frame_id` via `resolve_evaluate_frame_id`.
            state.set_stack(synthetic_frames, synthetic=True)

        # Modal body: the parser's raw detail text (verbatim source
        # snippets / chained-exception separator text for Python; the die
        # message + call-frame lines for Perl) -- NOT rebuilt from
        # `parsed.frames`, which only carries structured File/line/func
        # data for the synthetic StackFrames above.
        frames_text = parsed.detail

        def on_dismiss(result: str | None) -> None:
            if result == "restart":
                self.app._restart_session()

        exc_label = parsed.message or "Program crashed"
        can_restart = self.app.controller.supports_restart
        self.app.panels.last_exception_text = exc_label
        self.app.panels.last_frames_text = frames_text
        self.app.panels.last_can_restart = can_restart
        self.app.panels.last_header = parsed.header
        self.app.push_screen(
            _TracebackModal(
                exc_label,
                frames_text,
                can_restart=can_restart,
                header=parsed.header,
            ),
            callback=on_dismiss,
        )

    # --- exited / external terminal / output ---------------------------

    def on_exited(self, exit_code: int) -> None:
        try:
            console = self.app.query_one("#console-view", ConsoleView)
            console.write_output(f"\nProcess exited with code {exit_code}\n", "console")
        except Exception:
            log.exception("Error handling exited event")

    def on_external_terminal_started(self) -> None:
        try:
            console = self.app.query_one("#console-view", ConsoleView)
            console.write_output(
                "Debuggee running in external terminal window.\n"
                "Program output will appear there, not here.\n",
                "console",
            )
        except Exception:
            log.exception("Error handling external terminal started")

    def on_output(self, message: DapOutput) -> None:
        try:
            if message.category == "stderr":
                self.app._stderr_buffer.append(message.text)
            console = self.app.query_one("#console-view", ConsoleView)
            console.write_output(message.text, message.category)
        except Exception:
            log.exception("Error handling output event")

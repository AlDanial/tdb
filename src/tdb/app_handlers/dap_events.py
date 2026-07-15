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
import re
from typing import TYPE_CHECKING

from tdb.session.messages import DapOutput, DapStopped
from tdb.widgets.console_view import ConsoleView
from tdb.widgets.modals import _TracebackModal

if TYPE_CHECKING:
    from tdb.app import TdbApp

log = logging.getLogger(__name__)


# Pre-compiled at module level so repeated traceback parses don't
# re-build the regex each time.
_TB_FILE_RE = re.compile(
    r'^\s*File "(.+)", line (\d+)(?:, in (.+))?',
    re.MULTILINE,
)


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
                self._check_stderr_traceback()
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

    def _check_stderr_traceback(self) -> None:
        """If stderr contains a Python traceback, show it in a modal,
        build synthetic stack frames, and navigate Code View to the
        deepest frame."""
        from tdb.dap.types import Source, StackFrame

        stderr = "".join(self.app._stderr_buffer)
        tb_header = "Traceback (most recent call last):"
        if tb_header not in stderr:
            return

        # Capture from the FIRST traceback header to the end, so chained
        # exceptions ("The above exception was the direct cause..." /
        # "During handling of the above exception...") are preserved in full.
        tb_start = stderr.find(tb_header)
        tb_text = stderr[tb_start:].rstrip()

        # Split into individual traceback blocks (one per chained exception).
        block_starts = [m.start() for m in re.finditer(re.escape(tb_header), tb_text)]
        blocks: list[str] = []
        for i, s in enumerate(block_starts):
            e = block_starts[i + 1] if i + 1 < len(block_starts) else len(tb_text)
            blocks.append(tb_text[s:e].rstrip())

        # Synthetic stack frames come from the LAST block — that is the
        # exception that actually terminated the process (Python prints
        # cause/context first, final exception last).
        final_block = blocks[-1] if blocks else tb_text
        matches = list(_TB_FILE_RE.finditer(final_block))

        # The exception line is the last non-empty, non-indented line of the
        # final block (Python prints it after all "File" frames).
        lines = final_block.split("\n")
        exception_text = ""
        for line in reversed(lines):
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("File ") or stripped.startswith("Traceback "):
                break
            if line.startswith("    ") or line.startswith("\t"):
                continue
            exception_text = stripped
            break

        # Build synthetic stack frames (reversed: deepest = index 0, like DAP)
        state = self.app.controller.state
        synthetic_frames: list[StackFrame] = []
        for i, m in enumerate(reversed(matches)):
            path = m.group(1)
            line = int(m.group(2))
            func = m.group(3) or "<module>"
            synthetic_frames.append(
                StackFrame(
                    id=i,
                    name=func,
                    source=Source(path=path, name=os.path.basename(path)),
                    line=line,
                )
            )

        if synthetic_frames:
            # Synthetic: the debuggee is already gone (this runs after a
            # terminated event), so debugpy has no frame ids these would
            # map to. Flag them so any straggling evaluate routes around
            # `current_frame_id` via `resolve_evaluate_frame_id`.
            state.set_stack(synthetic_frames, synthetic=True)

        # Modal body: show every block's body (after its header line) so
        # chained exception separator text is preserved.
        first_header_end = tb_text.index("\n") + 1 if "\n" in tb_text else len(tb_text)
        frames_text = tb_text[first_header_end:].rstrip()

        def on_dismiss(result: str | None) -> None:
            if result == "restart":
                self.app._restart_session()

        exc_label = exception_text or "Program crashed"
        can_restart = self.app.controller.supports_restart
        self.app.panels.last_exception_text = exc_label
        self.app.panels.last_frames_text = frames_text
        self.app.panels.last_can_restart = can_restart
        self.app.push_screen(
            _TracebackModal(
                exc_label,
                frames_text,
                can_restart=can_restart,
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

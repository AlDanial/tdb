"""Main textual App for tdb."""

from __future__ import annotations

import ast
import logging
import os
import re

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Label, OptionList, Static
from textual.widgets._tree import TreeNode

from tdb.session.controller import DebugController
from tdb.session.messages import (
    DapInitialized,
    DapStopped,
    DapContinued,
    DapTerminated,
    DapExited,
    DapExternalTerminalStarted,
    DapOutput,
)
from tdb.session.textual_handler import TextualEventHandler
from tdb.keybindings import KeybindingConfig, Mode
from tdb.widgets.breakpoint_view import BreakpointView
from tdb.widgets.code_view import CodeView, _BreakpointConditionModal
from tdb.widgets.console_view import ConsoleView
from tdb.widgets.evaluate_console import EvaluateConsole
from tdb.widgets.menu_bar import MenuBar, _MenuDropdown
from tdb.widgets.stack_view import StackView
from tdb.widgets.status_bar import StatusBar
from tdb.persist import load_breakpoints, save_breakpoints
from tdb.widgets.async_tasks_modal import AsyncTasksModal, AsyncTaskInfo, TASK_COLLECT_EXPR, TASK_LOCALS_EXPR, parse_task_json
from tdb.widgets.variable_view import VariableView

log = logging.getLogger(__name__)


class _KeybindingsModal(ModalScreen[None]):
    """Modal showing the keybinding reference for both modes."""

    DEFAULT_CSS = """
    _KeybindingsModal {
        align: center middle;
    }
    _KeybindingsModal #dialog {
        width: 60;
        height: auto;
        max-height: 30;
        border: solid $primary;
        background: $surface;
        padding: 1 2;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close", show=False),
        Binding("enter", "dismiss_modal", "Close", show=False),
    ]

    def __init__(self, config: KeybindingConfig) -> None:
        super().__init__()
        self._config = config

    def compose(self):
        lines = []
        lines.append("[bold]Keybindings[/bold]  (ESC toggles mode)\n")

        lines.append("[bold underline]Navigation Mode[/bold underline]")
        for key_display, description in self._config.format_bindings(Mode.NAVIGATION):
            lines.append(f"  [bold cyan]{key_display:<12}[/bold cyan] {description}")

        lines.append("")
        lines.append("[bold underline]Debug Mode[/bold underline]")
        for key_display, description in self._config.format_bindings(Mode.DEBUG):
            lines.append(f"  [bold cyan]{key_display:<12}[/bold cyan] {description}")

        lines.append("")
        lines.append("[dim]Press ESC or Enter to close[/dim]")

        with Vertical(id="dialog"):
            yield Static("\n".join(lines), markup=True)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)


class _TracebackModal(ModalScreen[str | None]):
    """Scrollable modal showing a full exception traceback."""

    DEFAULT_CSS = """
    _TracebackModal {
        align: center middle;
    }
    _TracebackModal #dialog {
        width: 90%;
        height: 80%;
        border: solid $error;
        background: $surface;
        padding: 1 2;
        overflow-y: auto;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close", show=False),
        Binding("enter", "dismiss_modal", "Close", show=False),
        Binding("q", "dismiss_modal", "Close", show=False),
        Binding("R", "restart", "Restart", show=False),
    ]

    def __init__(self, exception_text: str, frames_text: str) -> None:
        super().__init__()
        self._exception_text = exception_text
        self._frames_text = frames_text

    def compose(self):
        lines = []
        lines.append(f"[bold red]{self._exception_text}[/bold red]")
        lines.append("")
        lines.append("[bold]Traceback (most recent call last):[/bold]")
        lines.append(self._frames_text)
        lines.append("")
        lines.append("[dim]Press ESC, Enter, or q to close · Press R to restart[/dim]")

        with Vertical(id="dialog"):
            yield Static("\n".join(lines), markup=True)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    def action_restart(self) -> None:
        self.dismiss("restart")


class TdbApp(App):
    """TUI Python debugger."""

    TITLE = "tdb"
    SUB_TITLE = "Python Debugger"

    CSS = """
    Screen {
        layers: default above;
    }

    #upper {
        height: 3fr;
    }

    #lower {
        height: 1fr;
    }

    #upper-left {
        width: 2fr;
    }

    #upper-right {
        width: 1fr;
    }

    #lower-left {
        width: 2fr;
    }

    #lower-right {
        width: 1fr;
    }

    #code-view {
        height: 1fr;
    }

    #upper-right > * {
        height: 1fr;
    }

    #lower-right > * {
        height: 1fr;
    }

    /* Inactive pane borders are gray; active pane is blue */
    .pane {
        border: solid gray;
        border-title-color: gray;
    }
    .pane:focus-within {
        border: solid $primary;
        border-title-color: $text;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit_debugger", "Quit"),
        Binding("escape", "escape_handler", "Escape", show=False),
        Binding("ctrl+c", "focus_code", "Code", show=False),
        Binding("ctrl+o", "focus_console", "Console", show=False),
        Binding("ctrl+e", "focus_eval", "Evaluate", show=False),
        Binding("ctrl+v", "focus_variables", "Variables", show=False),
        Binding("ctrl+s", "focus_stack", "Stack", show=False),
        Binding("ctrl+b", "focus_breakpoints", "Breakpoints", show=False),
    ]

    # --- Custom messages for UI updates ---

    class BreakpointsChanged(Message):
        pass

    class LazyLoadVariables(Message):
        def __init__(self, variables_reference: int, node: TreeNode[int]) -> None:
            self.variables_reference = variables_reference
            self.node = node
            super().__init__()

    class RefreshAsyncTasks(Message):
        pass

    def __init__(
        self,
        program: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        stop_on_entry: bool = False,
        just_my_code: bool = True,
        python: str | None = None,
        external_terminal: bool = False,
        keybindings: str = "vim",
        cli_breakpoints: list[tuple[str, int]] | None = None,
        server_port: int | None = None,
    ) -> None:
        super().__init__()
        self._program = program
        self._args = args
        self._cwd = cwd
        self._stop_on_entry = stop_on_entry
        self._just_my_code = just_my_code
        self._python = python
        self._external_terminal = external_terminal
        self._keybindings = keybindings
        self._cli_breakpoints = cli_breakpoints or []
        self._server_port = server_port

        self._textual_handler = TextualEventHandler(self)
        if server_port is not None:
            from tdb.server.event_handler import ServerEventHandler
            from tdb.session.event_bus import CompositeEventHandler
            self._server_handler = ServerEventHandler()
            self._event_handler = CompositeEventHandler(
                self._textual_handler, self._server_handler,
            )
        else:
            self._server_handler = None
            self._event_handler = self._textual_handler

        self.controller = DebugController(self._event_handler)
        self._stderr_buffer: list[str] = []
        self._exception_modal_shown = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield MenuBar(
            {
                "File": ["Open"],
                "Configure": ["Color Theme", "Keybindings"],
                "Help": ["Documentation", "About"],
            },
            action_labels={"async-tasks-label": "Async Tasks"},
            id="menu-bar",
        )
        with Horizontal(id="upper"):
            with Vertical(id="upper-left"):
                yield CodeView(id="code-view", classes="pane")
            with Vertical(id="upper-right"):
                yield ConsoleView(id="console-view", classes="pane")
                yield VariableView(id="variable-view", classes="pane")
                yield StackView(id="stack-view", classes="pane")
        yield StatusBar(id="status-bar")
        with Horizontal(id="lower"):
            with Vertical(id="lower-left"):
                yield EvaluateConsole(id="eval-console", classes="pane")
            with Vertical(id="lower-right"):
                yield BreakpointView(id="breakpoint-view", classes="pane")
        yield Footer()

    def on_mount(self) -> None:
        code_view = self.query_one("#code-view", CodeView)
        code_view.keybindings = KeybindingConfig.from_scheme(self._keybindings)
        self._update_code_title(code_view)
        code_view.load_file(self._program)
        code_view.focus()
        # Restore breakpoints from previous run
        saved = load_breakpoints()
        if saved:
            self.controller.state.breakpoints = saved
        # Add CLI breakpoints (additive, won't duplicate)
        for bp_path, bp_line in self._cli_breakpoints:
            bps = self.controller.state.breakpoints.get(bp_path, [])
            if not any(bp.line == bp_line for bp in bps):
                from tdb.dap.types import SourceBreakpoint
                bps.append(SourceBreakpoint(line=bp_line))
                self.controller.state.breakpoints[bp_path] = bps
        # Update visuals
        if self.controller.state.breakpoints:
            all_bps = self.controller.state.breakpoints
            bps = all_bps.get(code_view.source_path, []) if code_view.source_path else []
            code_view.set_breakpoints(bps)
            bp_view = self.query_one("#breakpoint-view", BreakpointView)
            bp_view.update_breakpoints(all_bps)
        self._start_session()
        if self._server_port is not None:
            self._start_server()

    def _update_code_title(self, code_view: CodeView) -> None:
        mode_label = code_view.mode.value
        code_view.border_title = f"Code \\[{mode_label}]"

    def on_code_view_mode_changed(self, message: CodeView.ModeChanged) -> None:
        code_view = self.query_one("#code-view", CodeView)
        self._update_code_title(code_view)

    @work(exclusive=True)
    async def _start_session(self) -> None:
        try:
            await self.controller.start(
                program=self._program,
                args=self._args,
                cwd=self._cwd,
                stop_on_entry=self._stop_on_entry,
                just_my_code=self._just_my_code,
                python=self._python,
                external_terminal=self._external_terminal,
            )
        except Exception:
            log.exception("Failed to start debug session")
            self.sub_title = "Failed to start"

    @work(exclusive=True, group="server")
    async def _start_server(self) -> None:
        """Start the JSON-RPC debug server alongside the TUI."""
        import uvicorn
        from tdb.server.app import ControllerRef, create_app

        assert self._server_handler is not None
        self._controller_ref = ControllerRef(self.controller)
        fastapi_app = create_app(self._controller_ref, self._server_handler)
        config = uvicorn.Config(
            fastapi_app,
            host="127.0.0.1",
            port=self._server_port,
            log_level="warning",
        )
        self._uvicorn_server = uvicorn.Server(config)
        log.info("Starting debug server on port %d", self._server_port)
        await self._uvicorn_server.serve()

    @work(exclusive=True)
    async def _restart_session(self) -> None:
        """Stop the current session and start a new one with the same arguments."""
        log.info("Restarting debug session")
        # Preserve breakpoints across restart
        saved_breakpoints = dict(self.controller.state.breakpoints)

        try:
            await self.controller.stop()
        except Exception:
            log.exception("Error stopping session for restart")

        # Create a fresh controller and restore breakpoints
        self._textual_handler = TextualEventHandler(self)
        if self._server_handler is not None:
            from tdb.session.event_bus import CompositeEventHandler
            self._server_handler.initialized_event.clear()
            self._server_handler.stopped_event.clear()
            self._server_handler.terminated_event.clear()
            self._server_handler.exit_code = None
            self._server_handler.last_stop_thread_id = None
            self._server_handler.last_stop_reason = None
            self._server_handler.last_stop_description = None
            self._server_handler.last_stop_text = None
            self._server_handler._output_buffer.clear()
            self._event_handler = CompositeEventHandler(
                self._textual_handler, self._server_handler,
            )
        else:
            self._event_handler = self._textual_handler
        self.controller = DebugController(self._event_handler)
        self.controller.state.breakpoints = saved_breakpoints

        # Update the server's controller reference so RPC sees the new one
        if hasattr(self, '_controller_ref'):
            self._controller_ref.set(self.controller)

        self._stderr_buffer.clear()
        self._exception_modal_shown = False

        status_bar = self.query_one("#status-bar", StatusBar)
        status_bar.set_idle()
        self.sub_title = "Restarting..."

        # Reload the source file in Code View
        code_view = self.query_one("#code-view", CodeView)
        code_view.load_file(self._program)
        code_view.current_line = None

        try:
            await self.controller.start(
                program=self._program,
                args=self._args,
                cwd=self._cwd,
                stop_on_entry=self._stop_on_entry,
                just_my_code=self._just_my_code,
                python=self._python,
                external_terminal=self._external_terminal,
            )
        except Exception:
            log.exception("Failed to restart debug session")
            self.sub_title = "Failed to restart"

    # --- DAP event message handlers ---
    # These run in textual's message loop, so async is safe.

    def on_dap_initialized(self, message: DapInitialized) -> None:
        log.info("on_dap_initialized called")
        self._do_configure_work()

    @work(exclusive=True, group="configure")
    async def _do_configure_work(self) -> None:
        try:
            await self.controller.do_configure()
            log.info("do_configure completed")
            self._update_ui_state()
        except Exception:
            log.exception("Failed to launch")
            self.sub_title = "Launch failed"

    async def on_dap_stopped(self, message: DapStopped) -> None:
        log.info("on_dap_stopped called thread=%s reason=%s", message.thread_id, message.reason)
        try:
            state = self.controller.state
            state.is_running = False
            state.stop_reason = message.reason
            if message.thread_id is not None:
                state.current_thread_id = message.thread_id
            await self.controller.fetch_stop_info()
            await self.controller.cleanup_run_to_cursor()
        except Exception:
            log.exception("Error handling stopped event")
        # Always update UI, even if fetch_stop_info partially failed
        self._update_ui_state()
        self._fetch_async_task_count()

        if message.reason == "exception":
            self._exception_modal_shown = True
            self._show_exception_modal(message)

    def _show_exception_modal(self, message: DapStopped) -> None:
        """Show a modal with the full exception traceback."""
        state = self.controller.state

        # Build exception header
        desc = message.description or "Exception"
        text = message.text or ""
        exception_text = f"{desc}: {text}" if text else desc

        # Build traceback from stack frames (bottom-up, like Python tracebacks)
        lines = []
        for frame in reversed(state.stack_frames):
            source = frame.source.path if frame.source and frame.source.path else "<unknown>"
            lines.append(f"  File \"{source}\", line {frame.line}, in {frame.name}")
        frames_text = "\n".join(lines) if lines else "  <no frames available>"

        def on_dismiss(result: str | None) -> None:
            if result == "restart":
                self._restart_session()

        self.push_screen(_TracebackModal(exception_text, frames_text), callback=on_dismiss)

    _TB_FILE_RE = re.compile(
        r'^\s*File "(.+)", line (\d+)(?:, in (.+))?', re.MULTILINE,
    )

    def _check_stderr_traceback(self) -> None:
        """If stderr contains a Python traceback, show it in a modal,
        build synthetic stack frames, and navigate Code View to the
        deepest frame."""
        from tdb.dap.types import Source, StackFrame

        stderr = "".join(self._stderr_buffer)
        if "Traceback (most recent call last):" not in stderr:
            return

        # Extract the last traceback block from stderr
        tb_start = stderr.rfind("Traceback (most recent call last):")
        tb_text = stderr[tb_start:].rstrip()

        # Parse all File "...", line N, in func entries
        matches = list(self._TB_FILE_RE.finditer(tb_text))

        # The exception line is typically the last non-empty line
        lines = tb_text.split("\n")
        exception_text = ""
        for line in reversed(lines):
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("File ") or stripped.startswith("Traceback "):
                break
            exception_text = stripped
            break

        # Build synthetic stack frames (reversed: deepest = index 0, like DAP)
        state = self.controller.state
        synthetic_frames: list[StackFrame] = []
        for i, m in enumerate(reversed(matches)):
            path = m.group(1)
            line = int(m.group(2))
            func = m.group(3) or "<module>"
            synthetic_frames.append(StackFrame(
                id=i,
                name=func,
                source=Source(path=path, name=os.path.basename(path)),
                line=line,
            ))

        if synthetic_frames:
            state.stack_frames = synthetic_frames
            state.current_frame_id = synthetic_frames[0].id

        # Show the raw traceback body (everything after the header line)
        header_end = tb_text.index("\n") + 1 if "\n" in tb_text else len(tb_text)
        frames_text = tb_text[header_end:].rstrip()

        def on_dismiss(result: str | None) -> None:
            if result == "restart":
                self._restart_session()

        self.push_screen(
            _TracebackModal(exception_text or "Program crashed", frames_text),
            callback=on_dismiss,
        )

    def on_dap_continued(self, message: DapContinued) -> None:
        try:
            self._stderr_buffer.clear()
            self._exception_modal_shown = False
            state = self.controller.state
            state.is_running = True
            state.clear_frame_data()
            self._update_ui_state()
        except Exception:
            log.exception("Error handling continued event")

    def on_dap_terminated(self, message: DapTerminated) -> None:
        log.info("on_dap_terminated called")
        try:
            state = self.controller.state
            state.is_terminated = True
            state.is_running = False
            if not self._exception_modal_shown:
                self._check_stderr_traceback()
            self._update_ui_state()
        except Exception:
            log.exception("Error handling terminated event")

    def on_dap_exited(self, message: DapExited) -> None:
        try:
            console = self.query_one("#console-view", ConsoleView)
            console.write_output(
                f"\nProcess exited with code {message.exit_code}\n", "console"
            )
        except Exception:
            log.exception("Error handling exited event")

    def on_dap_external_terminal_started(self, message: DapExternalTerminalStarted) -> None:
        try:
            console = self.query_one("#console-view", ConsoleView)
            console.write_output(
                "Debuggee running in external terminal window.\n"
                "Program output will appear there, not here.\n",
                "console",
            )
        except Exception:
            log.exception("Error handling external terminal started")

    def on_dap_output(self, message: DapOutput) -> None:
        try:
            if message.category == "stderr":
                self._stderr_buffer.append(message.text)
            console = self.query_one("#console-view", ConsoleView)
            console.write_output(message.text, message.category)
        except Exception:
            log.exception("Error handling output event")

    # --- UI update helper ---

    def _update_ui_state(self) -> None:
        state = self.controller.state
        code_view = self.query_one("#code-view", CodeView)
        status_bar = self.query_one("#status-bar", StatusBar)

        if state.is_terminated:
            self.sub_title = "Terminated"
            if state.stack_frames:
                # Show crash location from synthetic or real stack frames
                stack_view = self.query_one("#stack-view", StackView)
                stack_view.update_frames(state.stack_frames, state.current_frame_id)
                # Navigate Code View and status bar to the current frame
                for frame in state.stack_frames:
                    if frame.id == state.current_frame_id and frame.source and frame.source.path:
                        if frame.source.path != code_view.source_path:
                            code_view.load_file(frame.source.path)
                        code_view.current_line = frame.line
                        status_bar.set_paused(frame.source.path, frame.line, reason="terminated")
                        break
                else:
                    status_bar.set_terminated()
            else:
                code_view.current_line = None
                status_bar.set_terminated()
            return

        if state.is_running:
            self.sub_title = "Running..."
            status_bar.set_running()
            code_view.current_line = None
            return

        # Stopped
        reason = state.stop_reason or "stopped"
        self.sub_title = f"Stopped ({reason})"

        source_path = state.get_current_source_path()
        current_line = state.get_current_line()
        status_bar.set_paused(source_path, current_line, reason=reason)

        # Use top-of-stack as fallback if current frame has no source path
        if not source_path:
            stop_location, stop_line = state.get_stop_location()
            if stop_location and os.path.isfile(stop_location):
                source_path = stop_location
                current_line = stop_line
                status_bar.set_paused(source_path, current_line, reason=reason)

        if source_path and source_path != code_view.source_path:
            code_view.load_file(source_path)
        if source_path:
            bps = state.breakpoints.get(source_path, [])
            code_view.set_breakpoints(bps)
        code_view.set_breakpoints_disabled(state.breakpoints_disabled)
        code_view.current_line = current_line

        stack_view = self.query_one("#stack-view", StackView)
        stack_view.update_frames(state.stack_frames, state.current_frame_id)

        var_view = self.query_one("#variable-view", VariableView)
        var_view.update_variables(state.scopes, state.variables)

    # --- Widget message handlers ---

    async def on_code_view_breakpoint_toggled(self, message: CodeView.BreakpointToggled) -> None:
        try:
            await self.controller.toggle_breakpoint(message.source_path, message.line)
            self.post_message(self.BreakpointsChanged())
        except Exception:
            log.exception("Error toggling breakpoint")

    class _ApplyBreakpointCondition(Message):
        """Internal message to apply condition after modal dismisses."""
        def __init__(self, source_path: str, line: int,
                     condition: str | None, hit_condition: str | None) -> None:
            self.source_path = source_path
            self.line = line
            self.condition = condition
            self.hit_condition = hit_condition
            super().__init__()

    def _open_breakpoint_condition_modal(self, source_path: str, line: int) -> None:
        """Show the condition/hit-count modal for a breakpoint."""
        bps = self.controller.state.breakpoints.get(source_path, [])
        bp = next((b for b in bps if b.line == line), None)
        if bp is None:
            return
        modal = _BreakpointConditionModal(
            source_path, line,
            condition=bp.condition,
            hit_condition=bp.hit_condition,
        )

        def on_dismiss(result: tuple[str | None, str | None] | None) -> None:
            if result is None:
                return
            condition, hit_condition = result
            self.post_message(self._ApplyBreakpointCondition(
                source_path, line, condition, hit_condition,
            ))

        self.push_screen(modal, callback=on_dismiss)

    async def on_tdb_app__apply_breakpoint_condition(
        self, message: _ApplyBreakpointCondition,
    ) -> None:
        try:
            await self.controller.set_breakpoint_condition(
                message.source_path, message.line,
                message.condition, message.hit_condition,
            )
            self.post_message(self.BreakpointsChanged())
        except Exception:
            log.exception("Error setting breakpoint condition")

    def on_code_view_breakpoint_condition_requested(
        self, message: CodeView.BreakpointConditionRequested,
    ) -> None:
        self._open_breakpoint_condition_modal(message.source_path, message.line)

    def on_breakpoint_view_breakpoint_condition_requested(
        self, message: BreakpointView.BreakpointConditionRequested,
    ) -> None:
        self._open_breakpoint_condition_modal(message.source_path, message.line)

    async def on_code_view_debug_action(self, message: CodeView.DebugAction) -> None:
        log.info("on_code_view_debug_action called: %s", message.action)
        try:
            if message.action in ("stack_up", "stack_down"):
                await self._navigate_stack(message.action == "stack_up")
                return
            if message.action == "restart":
                self._restart_session()
                return
            handler = {
                "continue_": self.controller.continue_,
                "step_over": self.controller.step_over,
                "step_in": self.controller.step_in,
                "step_out": self.controller.step_out,
                "pause": self.controller.pause,
            }.get(message.action)
            if handler:
                await handler()
                self._update_ui_state()
        except Exception:
            log.exception("Error executing debug action: %s", message.action)

    async def _navigate_stack(self, up: bool) -> None:
        """Move to the next/previous frame in the call stack."""
        try:
            await self.controller.navigate_stack(up)
        except Exception:
            log.exception("Error navigating stack")
        self._update_ui_state()

    async def on_code_view_run_to_cursor(self, message: CodeView.RunToCursor) -> None:
        try:
            await self.controller.run_to_cursor(message.source_path, message.line)
            self._update_ui_state()
        except Exception:
            log.exception("Error in run to cursor")

    async def on_stack_view_frame_selected(self, message: StackView.FrameSelected) -> None:
        await self.controller.select_frame(message.frame_id)
        if message.source_path:
            code_view = self.query_one("#code-view", CodeView)
            if message.source_path != code_view.source_path:
                code_view.load_file(message.source_path)
            code_view.current_line = message.line
        self._update_ui_state()

    async def on_breakpoint_view_breakpoint_selected(
        self, message: BreakpointView.BreakpointSelected
    ) -> None:
        code_view = self.query_one("#code-view", CodeView)
        if message.source_path != code_view.source_path:
            code_view.load_file(message.source_path)
        code_view.goto_line(message.line)

    def on_tdb_app_breakpoints_changed(self, message: BreakpointsChanged) -> None:
        state = self.controller.state
        code_view = self.query_one("#code-view", CodeView)
        if code_view.source_path:
            bps = state.breakpoints.get(code_view.source_path, [])
            code_view.set_breakpoints(bps)
        code_view.set_breakpoints_disabled(state.breakpoints_disabled)
        bp_view = self.query_one("#breakpoint-view", BreakpointView)
        bp_view.update_breakpoints(state.breakpoints)
        bp_view.set_disabled_state(state.breakpoints_disabled)

    async def on_breakpoint_view_disable_all_requested(
        self, message: BreakpointView.DisableAllRequested,
    ) -> None:
        try:
            state = self.controller.state
            if state.breakpoints_disabled:
                await self.controller.enable_all_breakpoints()
            else:
                await self.controller.disable_all_breakpoints()
            self.post_message(self.BreakpointsChanged())
        except Exception:
            log.exception("Error toggling breakpoint disable")

    async def on_breakpoint_view_clear_all_requested(
        self, message: BreakpointView.ClearAllRequested,
    ) -> None:
        try:
            await self.controller.clear_all_breakpoints()
            self.post_message(self.BreakpointsChanged())
        except Exception:
            log.exception("Error clearing breakpoints")

    async def on_tdb_app_lazy_load_variables(self, message: LazyLoadVariables) -> None:
        try:
            variables = await self.controller.client.variables(message.variables_reference)
            var_view = self.query_one("#variable-view", VariableView)
            var_view.load_children(message.node, variables)
        except Exception:
            log.debug("Failed to load variables (reference may be stale): %d",
                      message.variables_reference)

    async def on_evaluate_console_evaluate_requested(
        self, message: EvaluateConsole.EvaluateRequested
    ) -> None:
        result = await self.controller.evaluate(message.expression)
        eval_console = self.query_one("#eval-console", EvaluateConsole)
        eval_console.show_result(result)

    async def on_evaluate_console_help_requested(
        self, message: EvaluateConsole.HelpRequested
    ) -> None:
        eval_console = self.query_one("#eval-console", EvaluateConsole)
        obj = message.expression
        parts: list[str] = []

        # Try to get signature (may fail for C builtins or non-callables)
        sig_result = await self.controller.evaluate(
            f"str(__import__('inspect').signature({obj}))"
        )
        sig_result = _unquote_dap_string(sig_result)
        # signature() returns "(param, ...)" on success; errors contain "Error"
        if sig_result.startswith("("):
            parts.append(f"{obj}{sig_result}")

        # Get docstring
        doc_result = await self.controller.evaluate(
            f"getattr({obj}, '__doc__', None) or ''"
        )
        doc_result = _unquote_dap_string(doc_result)
        if doc_result:
            parts.append(doc_result)

        if parts:
            eval_console.show_result("\n".join(parts))
        else:
            eval_console.show_error("No documentation available")

    async def on_evaluate_console_completion_requested(
        self, message: EvaluateConsole.CompletionRequested
    ) -> None:
        try:
            items = await self.controller.client.completions(
                text=message.text,
                column=message.column,
                frame_id=self.controller.state.current_frame_id,
            )
            completions = [(item.label, item.text) for item in items]
            eval_console = self.query_one("#eval-console", EvaluateConsole)
            eval_console.apply_completion(message.text, completions)
        except Exception:
            log.exception("Error fetching completions")

    # --- Menu handlers ---

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Handle dropdown menu selections (dropdowns are mounted on Screen)."""
        if not isinstance(event.option_list, _MenuDropdown):
            return
        option_id = event.option.id or ""
        if ":" not in option_id:
            return
        menu, item = option_id.split(":", 1)
        # Close the dropdown
        menu_bar = self.query_one("#menu-bar", MenuBar)
        menu_bar._close_all()

        if menu == "File" and item == "Open":
            self.action_open_file()
        elif menu == "Configure" and item == "Color Theme":
            self.action_color_theme()
        elif menu == "Configure" and item == "Keybindings":
            self.action_keybindings()
        elif menu == "Help" and item == "Documentation":
            self.action_documentation()
        elif menu == "Help" and item == "About":
            self.action_about()
        event.stop()

    def action_open_file(self) -> None:
        self.notify("Open file: not yet implemented", title="File")

    def action_color_theme(self) -> None:
        self.notify("Color theme: not yet implemented", title="Configure")

    def action_keybindings(self) -> None:
        code_view = self.query_one("#code-view", CodeView)
        self.push_screen(_KeybindingsModal(code_view.keybindings))

    def action_documentation(self) -> None:
        self.notify("tdb — TUI Python Debugger\n\nESC toggles Navigation/Debug mode\nCtrl+E = Evaluate console\nConfigure > Keybindings for full reference", title="Documentation")

    def action_about(self) -> None:
        self.notify("tdb v0.1.0\nA TUI-based Python debugger\nPowered by debugpy + textual", title="About")

    # --- Async tasks ---

    def on_menu_bar_action_label_clicked(self, message: MenuBar.ActionLabelClicked) -> None:
        if message.label_id == "async-tasks-label":
            self._open_async_tasks()

    @work(exclusive=True, group="async-tasks")
    async def _fetch_async_task_count(self) -> None:
        """Evaluate asyncio.all_tasks() count and update the menu bar label."""
        if self.controller.state.is_terminated or self.controller.state.is_running:
            return
        try:
            result = await self.controller.evaluate(
                "len(__import__('asyncio').all_tasks())"
            )
            # Result is a string like "5"
            count = int(result)
            menu_bar = self.query_one("#menu-bar", MenuBar)
            menu_bar.update_action_label("async-tasks-label", f"Async Tasks ({count})")
        except Exception:
            log.debug("Could not fetch async task count (program may not use asyncio)")

    @work(exclusive=True, group="async-tasks-open")
    async def _open_async_tasks(self) -> None:
        """Fetch full task info and open the modal."""
        if self.controller.state.is_terminated:
            self.notify("Program has terminated", title="Async Tasks")
            return
        if self.controller.state.is_running:
            self.notify("Program is running — pause first", title="Async Tasks")
            return
        try:
            raw = await self.controller.evaluate(TASK_COLLECT_EXPR)
            tasks = parse_task_json(raw)
        except Exception:
            log.exception("Error fetching async tasks")
            tasks = []

        if not tasks:
            # Show the raw evaluate result so failures aren't silent
            log.warning("Async task collection returned no tasks. Raw: %s", raw[:300] if raw else "(empty)")
            self.notify("No asyncio tasks found (program may not use asyncio)", title="Async Tasks")
            return

        self._async_tasks_modal = AsyncTasksModal(tasks)
        self.push_screen(self._async_tasks_modal)

    async def on_tdb_app_refresh_async_tasks(self, message: RefreshAsyncTasks) -> None:
        """Handle refresh request from the async tasks modal."""
        if self.controller.state.is_terminated or self.controller.state.is_running:
            return
        try:
            raw = await self.controller.evaluate(TASK_COLLECT_EXPR)
            tasks = parse_task_json(raw)
        except Exception:
            log.exception("Error refreshing async tasks")
            return
        if hasattr(self, "_async_tasks_modal"):
            self._async_tasks_modal.update_tasks(tasks)

    async def on_async_tasks_modal_load_task_variables(
        self, message: AsyncTasksModal.LoadTaskVariables
    ) -> None:
        """Fetch a task's local variables via DAP and populate the tree."""
        if self.controller.state.is_terminated or self.controller.state.is_running:
            return
        if not hasattr(self, "_async_tasks_modal"):
            return
        try:
            expr = TASK_LOCALS_EXPR.format(task_name=message.task_name)
            _result, var_ref = await self.controller.client.evaluate(
                expr,
                frame_id=self.controller.state.current_frame_id,
                context="repl",
            )
            if var_ref > 0:
                variables = await self.controller.client.variables(var_ref)
            else:
                variables = []
        except Exception:
            log.debug("Failed to load variables for task %s", message.task_name)
            variables = []
        self._async_tasks_modal.show_task_variables(variables)

    # --- Actions ---

    def action_escape_handler(self) -> None:
        """ESC from non-CodeView widgets: focus the code view."""
        code_view = self.query_one("#code-view", CodeView)
        if not code_view.has_focus:
            code_view.focus()

    def action_focus_code(self) -> None:
        self.query_one("#code-view", CodeView).focus()

    def action_focus_console(self) -> None:
        self.query_one("#console-view", ConsoleView).focus()

    def action_focus_eval(self) -> None:
        self.query_one("#eval-console", EvaluateConsole).focus_input()

    def action_focus_variables(self) -> None:
        self.query_one("#variable-view", VariableView).focus()

    def action_focus_stack(self) -> None:
        self.query_one("#stack-view", StackView).focus()

    def action_focus_breakpoints(self) -> None:
        self.query_one("#breakpoint-view", BreakpointView).focus()

    async def action_quit_debugger(self) -> None:
        save_breakpoints(self.controller.state.breakpoints)
        await self.controller.stop()
        if hasattr(self, '_uvicorn_server'):
            self._uvicorn_server.should_exit = True
        self.exit()


def _unquote_dap_string(s: str) -> str:
    """Strip repr quoting from a DAP evaluate result that is a Python string.

    debugpy returns string results as their repr (e.g. "'hello\\nworld'").
    This converts that back to the actual string content.
    """
    if not s:
        return s
    try:
        value = ast.literal_eval(s)
        if isinstance(value, str):
            return value
    except (ValueError, SyntaxError):
        pass
    return s

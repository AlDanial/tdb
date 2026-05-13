"""Main textual App for tdb."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from textwrap import dedent

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
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
from tdb.widgets.modals import (
    _AboutModal,
    _DocumentationModal,
    _KeybindingsModal,
    _OpenFileModal,
    _PauseFailedModal,
    _QuitConfirmModal,
    _TracebackModal,
)
from tdb.widgets.stack_view import StackView
from tdb.widgets.status_bar import StatusBar
from tdb.app_helpers import find_readme, unquote_dap_string
from tdb.persist import load_breakpoints, load_theme, save_breakpoints, save_config
from tdb.widgets.async_tasks_modal import AsyncTasksModal
from tdb.widgets.processes_modal import ProcessesModal
from tdb.widgets.threads_modal import ThreadsModal
from tdb.widgets.variable_view import VariableView
from tdb import __version__ as tdb_version

log = logging.getLogger(__name__)


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
        Binding("q", "confirm_quit", "Quit", show=False),
        Binding("escape", "escape_handler", "Escape", show=False),
        Binding("ctrl+c", "focus_code", "Code", show=False),
        Binding("ctrl+o", "focus_console", "Console", show=False),
        Binding("ctrl+e", "focus_eval", "Evaluate", show=False),
        Binding("ctrl+v", "focus_variables", "Variables", show=False),
        Binding("ctrl+s", "focus_stack", "Stack", show=False),
        Binding("ctrl+b", "focus_breakpoints", "Breakpoints", show=False),
        # Menu-bar shortcuts: Alt+first-letter matches the convention used
        # by nano, mc, htop, and every desktop menu bar. Hidden from the
        # footer to keep it uncluttered; they appear in the Keybindings modal.
        # Textual's ANSI parser rewrites ESC+f / ESC+b (what terminals send
        # for Alt+F / Alt+B) to ctrl+right / ctrl+left — readline word-motion
        # convention. So we bind both forms for File; a user pressing Alt+F
        # gets ctrl+right delivered to tdb, which still opens the menu.
        Binding("alt+f,ctrl+right", "menu_file", "File menu", show=False),
        Binding("alt+c", "menu_configure", "Configure menu", show=False),
        Binding("alt+t", "menu_threads", "Threads", show=False),
        Binding("alt+p", "menu_processes", "Processes", show=False),
        Binding("alt+a", "menu_async_tasks", "Async Tasks", show=False),
        Binding("alt+h", "menu_help", "Help menu", show=False),
    ]

    # --- Custom messages for UI updates ---

    class BreakpointsChanged(Message):
        pass

    class LazyLoadVariables(Message):
        def __init__(
            self,
            variables_reference: int,
            node: TreeNode[int],
            source: VariableView | None = None,
        ) -> None:
            self.variables_reference = variables_reference
            self.node = node
            # The VariableView the request originated from. When None, the
            # main #variable-view is assumed (for backward compatibility).
            self.source = source
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
        terminal: str | None = None,
        keybindings: str = "vim",
        cli_breakpoints: list[tuple[str, int]] | None = None,
        attach_host: str | None = None,
        attach_port: int | None = None,
        sub_process: bool = True,
        server_port: int | None = None,
        post_mortem_snapshot: dict | None = None,
    ) -> None:
        super().__init__()
        self._program = program
        self._args = args
        self._cwd = cwd
        self._stop_on_entry = stop_on_entry
        self._just_my_code = just_my_code
        self._python = python
        self._terminal = terminal
        self._keybindings = keybindings
        self._cli_breakpoints = cli_breakpoints or []
        self._attach_host = attach_host
        self._attach_port = attach_port
        self._sub_process = sub_process
        self._server_port = server_port
        self._post_mortem_snapshot = post_mortem_snapshot

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
        # True after File > Open loads a new program but before the user
        # presses `r` or `c` to start it. While pending, debug actions other
        # than restart/continue are ignored; `c` is rerouted to start.
        self._session_pending = False
        # Set true once a quit has been confirmed/initiated. Guards both
        # the confirm-modal path (`q`) and the direct path (`Ctrl+Q`) so
        # repeat keypresses while `controller.stop()` is in flight don't
        # pile up modals or kick off concurrent shutdowns.
        self._is_quitting = False

        # Logic collaborators that the App forwards to. Keeping them on
        # the instance (rather than importing functions) lets each one
        # close over `self` for `query_one`, `push_screen`, `notify`,
        # etc., without per-call boilerplate.
        from tdb.app_handlers.dap_events import DapEventCoordinator
        from tdb.app_handlers.inspection import InspectionWorkflows
        self._inspection = InspectionWorkflows(self)
        self._dap = DapEventCoordinator(self)

    def compose(self) -> ComposeResult:
        yield Header()
        yield MenuBar(
            {
                "Configure": ["Color Theme", "Keybindings"],
                "Help": ["Documentation", "About"],
            },
            leading_action_labels={
                "open-file-label": "File",
            },
            action_labels={
                "threads-label": "Threads",
                "processes-label": "Processes",
                "async-tasks-label": "Async Tasks",
            },
            right_menus=["Help"],
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
        saved_theme = load_theme()
        if saved_theme and saved_theme in self.available_themes:
            self.theme = saved_theme

        code_view = self.query_one("#code-view", CodeView)
        code_view.keybindings = KeybindingConfig.from_scheme(self._keybindings)
        self._update_code_title(code_view)

        if self._post_mortem_snapshot is not None:
            self._enter_post_mortem(code_view)
            return

        if self._program:
            code_view.load_file(self._program)
        code_view.focus()
        # Restore breakpoints saved for this specific program
        program_key = str(Path(self._program).resolve()) if self._program else ""
        saved = load_breakpoints(program=program_key) if program_key else {}
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

    def _enter_post_mortem(self, code_view: CodeView) -> None:
        """Populate the UI from a frozen crash snapshot (no DAP session)."""
        snapshot = self._post_mortem_snapshot
        assert snapshot is not None
        self.controller.load_post_mortem(snapshot)
        state = self.controller.state

        exc = snapshot.get("exception", {})
        self.sub_title = f"Post-mortem · {exc.get('type', 'Exception')}"

        # Pipe the formatted traceback into the console view so the user
        # can scroll through the full chain.
        tb_text = exc.get("traceback_text", "")
        if tb_text:
            console = self.query_one("#console-view", ConsoleView)
            console.write_output(tb_text, "stderr")

        # Load crash site into Code View.
        if state.stack_frames:
            top = state.stack_frames[0]
            if top.source and top.source.path and os.path.isfile(top.source.path):
                code_view.load_file(top.source.path)
            code_view.current_line = top.line

        # Populate stack + variables panes.
        stack_view = self.query_one("#stack-view", StackView)
        stack_view.update_frames(state.stack_frames, state.current_frame_id)
        var_view = self.query_one("#variable-view", VariableView)
        var_view.update_variables(state.scopes, state.variables)

        status_bar = self.query_one("#status-bar", StatusBar)
        if state.stack_frames:
            top = state.stack_frames[0]
            src = top.source.path if top.source else None
            status_bar.set_paused(src, top.line, reason="exception")
        else:
            status_bar.set_terminated()

        code_view.focus()

    def _update_code_title(self, code_view: CodeView) -> None:
        mode_label = code_view.mode.value
        styled = f"[red]{mode_label}[/]" if mode_label.lower() == "navigation" else mode_label
        code_view.border_title = f"[bold orange]C[/]ode \\[{styled}]"

    def on_code_view_mode_changed(self, message: CodeView.ModeChanged) -> None:
        code_view = self.query_one("#code-view", CodeView)
        self._update_code_title(code_view)

    @work(exclusive=True)
    async def _start_session(self) -> None:
        try:
            if self._attach_host is not None and self._attach_port is not None:
                await self.controller.remote_attach(
                    host=self._attach_host,
                    port=self._attach_port,
                )
            else:
                await self.controller.start(
                    program=self._program,
                    args=self._args,
                    cwd=self._cwd,
                    stop_on_entry=self._stop_on_entry,
                    just_my_code=self._just_my_code,
                    python=self._python,
                    terminal=self._terminal,
                    sub_process=self._sub_process,
                )
        except Exception:
            log.exception("Failed to start debug session")
            self.sub_title = "Failed to start"

    @work(exclusive=True, group="server")
    async def _start_server(self) -> None:
        """Start the JSON-RPC debug server alongside the TUI."""
        import uvicorn
        from tdb.server.app import create_app
        from tdb.server.handlers import ControllerRef, HandlerRef, RpcHandlers

        assert self._server_handler is not None
        self._controller_ref = ControllerRef(self.controller)
        self._handler_ref = HandlerRef(self._server_handler)
        handlers = RpcHandlers(self._controller_ref, self._handler_ref)
        fastapi_app = create_app(handlers)
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
    async def _restart_session(
        self,
        new_program: str | None = None,
        *,
        start_immediately: bool = True,
    ) -> None:
        """Stop the current session and start a new one.

        If new_program is given, switch to that program (saving the old
        program's breakpoints under its key, and loading any saved
        breakpoints for the new program). Otherwise reuse self._program.

        If start_immediately is False, prepare the new session (stop old,
        recreate controller, load file, restore breakpoints) but do NOT
        launch the debuggee — wait for the user to press `r` or `c`.
        """
        # Restart in remote-attach mode (incl. tdb.breakpoint() sessions)
        # has no debuggee to relaunch — _program is empty and the original
        # debugpy server is gone. Drop the request rather than tearing
        # down the controller and trying to start nothing.
        if new_program is None and not self.controller.supports_restart:
            self.notify(
                "Restart is not available in remote-attach / tdb.breakpoint() mode.",
                severity="warning",
            )
            return

        if new_program:
            log.info("Switching debug session to: %s", new_program)
            if self._program:
                try:
                    old_key = str(Path(self._program).resolve())
                    save_breakpoints(
                        self.controller.state.breakpoints, program=old_key,
                    )
                except Exception:
                    log.exception("Failed to save breakpoints for previous program")
            new_key = str(Path(new_program).resolve())
            saved_breakpoints = load_breakpoints(program=new_key)
            self._program = new_program
            self._cli_breakpoints = []
            self._args = []
            self._cwd = None
        else:
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
            from tdb.server.event_handler import ServerEventHandler
            from tdb.session.event_bus import CompositeEventHandler
            # Swap in a fresh ServerEventHandler. HandlerRef.set migrates
            # SSE subscribers from the old instance and broadcasts a
            # `session_restart` event so connected clients can reset
            # before they see events from the new session.
            self._server_handler = ServerEventHandler()
            if hasattr(self, '_handler_ref'):
                self._handler_ref.set(self._server_handler)
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

        # Reload the source file in Code View
        code_view = self.query_one("#code-view", CodeView)
        code_view.load_file(self._program)
        code_view.current_line = None

        if not start_immediately:
            self._session_pending = True
            self.sub_title = f"Loaded {Path(self._program).name} — press r or c to start"
            return

        self._session_pending = False
        self.sub_title = "Restarting..."
        try:
            await self.controller.start(
                program=self._program,
                args=self._args,
                cwd=self._cwd,
                stop_on_entry=self._stop_on_entry,
                just_my_code=self._just_my_code,
                python=self._python,
                terminal=self._terminal,
                sub_process=self._sub_process,
            )
        except Exception:
            log.exception("Failed to restart debug session")
            self.sub_title = "Failed to restart"

    # --- DAP event message handlers ---
    # These run in textual's message loop, so async is safe.

    # --- DAP event handlers (delegate to DapEventCoordinator) -----------
    # These stubs exist because Textual auto-dispatches `on_dap_*` based
    # on method name on the App. Logic is in self._dap.

    def on_dap_initialized(self, message: DapInitialized) -> None:
        log.info("on_dap_initialized called")
        self._do_configure_work()

    @work(exclusive=True, group="configure")
    async def _do_configure_work(self) -> None:
        await self._dap.do_configure()

    async def on_dap_stopped(self, message: DapStopped) -> None:
        await self._dap.on_stopped(message)

    def on_dap_continued(self, message: DapContinued) -> None:
        self._dap.on_continued()

    async def on_dap_terminated(self, message: DapTerminated) -> None:
        await self._dap.on_terminated()

    def on_dap_exited(self, message: DapExited) -> None:
        self._dap.on_exited(message.exit_code)

    def on_dap_external_terminal_started(self, message: DapExternalTerminalStarted) -> None:
        self._dap.on_external_terminal_started()

    def on_dap_output(self, message: DapOutput) -> None:
        self._dap.on_output(message)

    # --- UI update helper ---

    def _update_ui_state(self) -> None:
        state = self.controller.state
        code_view = self.query_one("#code-view", CodeView)
        status_bar = self.query_one("#status-bar", StatusBar)

        if state.is_terminated:
            if state.is_post_mortem:
                exc_type = "Exception"
                if self._post_mortem_snapshot is not None:
                    exc_type = self._post_mortem_snapshot.get(
                        "exception", {},
                    ).get("type", "Exception")
                self.sub_title = f"Post-mortem · {exc_type}"
            else:
                self.sub_title = "Terminated"
            if state.stack_frames:
                reason = "exception" if state.is_post_mortem else "terminated"
                stack_view = self.query_one("#stack-view", StackView)
                stack_view.update_frames(state.stack_frames, state.current_frame_id)
                # Navigate Code View and status bar to the current frame
                for frame in state.stack_frames:
                    if frame.id == state.current_frame_id and frame.source and frame.source.path:
                        if frame.source.path != code_view.source_path:
                            code_view.load_file(frame.source.path)
                        code_view.current_line = frame.line
                        status_bar.set_paused(frame.source.path, frame.line, reason=reason)
                        break
                else:
                    status_bar.set_terminated()
                if state.is_post_mortem:
                    # Post-mortem has per-frame scopes — refresh the var tree
                    # so frame navigation actually shows the selected frame's
                    # locals.
                    var_view = self.query_one("#variable-view", VariableView)
                    var_view.update_variables(state.scopes, state.variables)
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
        if self.controller.state.is_post_mortem:
            self.notify(
                "Breakpoints disabled in post-mortem mode",
                title="tdb", severity="warning",
            )
            return
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
            if message.action == "quit":
                self.action_confirm_quit()
                return
            if self.controller.state.is_post_mortem:
                # Frozen snapshot — no live debuggee to step/continue/restart.
                self.notify(
                    "Not available in post-mortem mode",
                    title="tdb", severity="warning",
                )
                return
            if message.action == "restart":
                self._restart_session()
                return
            if self._session_pending and message.action == "continue_":
                # File was opened but never started — `c` starts it.
                self._restart_session()
                return
            if message.action == "pause":
                # pause() returns False when the request didn't land
                # within the timeout — typical for a fully-deadlocked
                # asyncio program (no Python frames running, debugpy
                # has nowhere to deliver the pause). Surface that in
                # a modal so the keypress isn't silently ignored.
                paused = await self.controller.pause()
                if not paused and not self.controller.state.is_terminated:
                    self.push_screen(_PauseFailedModal())
                self._update_ui_state()
                return
            handler = {
                "continue_": self.controller.continue_,
                "step_over": self.controller.step_over,
                "step_in": self.controller.step_in,
                "step_out": self.controller.step_out,
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

    async def on_breakpoint_view_breakpoint_delete_requested(
        self, message: BreakpointView.BreakpointDeleteRequested,
    ) -> None:
        try:
            await self.controller.remove_breakpoint(message.source_path, message.line)
            self.post_message(self.BreakpointsChanged())
        except Exception:
            log.exception("Error deleting breakpoint")

    async def on_breakpoint_view_breakpoint_toggle_enabled_requested(
        self, message: BreakpointView.BreakpointToggleEnabledRequested,
    ) -> None:
        try:
            await self.controller.toggle_breakpoint_enabled(
                message.source_path, message.line,
            )
            self.post_message(self.BreakpointsChanged())
        except Exception:
            log.exception("Error toggling breakpoint enabled state")

    async def on_tdb_app_lazy_load_variables(self, message: LazyLoadVariables) -> None:
        # Post-mortem: all variables are pre-populated in state.variables.
        # Look them up directly — there is no live DAP session to query.
        if self.controller.state.is_post_mortem:
            source = message.source
            variables = self.controller.state.variables.get(
                message.variables_reference, [],
            )
            target = source if source is not None else self.query_one(
                "#variable-view", VariableView,
            )
            target.load_children(message.node, variables)
            return

        # If the request originated from the Processes modal's VariableView,
        # resolve the child DAPClient for the currently-selected process —
        # the variablesReference is scoped to that session, not the parent's.
        source = message.source
        if source is not None and source.id == "proc-vars":
            modal = getattr(self, "_processes_modal", None)
            pid = getattr(modal, "_current_pid", None) if modal is not None else None
            child = self.controller.get_child_client(pid) if pid is not None else None
            if child is None:
                source.load_children(message.node, [])
                return
            try:
                variables = await child.variables(message.variables_reference)
            except Exception:
                log.debug(
                    "Failed to load child (pid=%s) variables (ref=%d)",
                    pid, message.variables_reference,
                )
                source.load_children(message.node, [])
                return
            source.load_children(message.node, variables)
            return

        try:
            variables = await self.controller.active_client.variables(message.variables_reference)
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
        sig_result = unquote_dap_string(sig_result)
        # signature() returns "(param, ...)" on success; errors contain "Error"
        if sig_result.startswith("("):
            parts.append(f"{obj}{sig_result}")

        # Get docstring
        doc_result = await self.controller.evaluate(
            f"getattr({obj}, '__doc__', None) or ''"
        )
        doc_result = unquote_dap_string(doc_result)
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
            items = await self.controller.active_client.completions(
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

        if menu == "Configure" and item == "Color Theme":
            self.action_color_theme()
        elif menu == "Configure" and item == "Keybindings":
            self.action_keybindings()
        elif menu == "Help" and item == "Documentation":
            self.action_documentation()
        elif menu == "Help" and item == "About":
            self.action_about()
        event.stop()

    def action_open_file(self) -> None:
        initial = self._cwd or (
            str(Path(self._program).parent) if self._program else str(Path.cwd())
        )

        def on_dismiss(path: str | None) -> None:
            if path:
                self._restart_session(new_program=path, start_immediately=False)

        self.push_screen(_OpenFileModal(initial), callback=on_dismiss)

    def action_color_theme(self) -> None:
        # Opens textual's built-in fuzzy-search theme palette. The watch_theme
        # hook below persists the choice when the user selects one.
        self.action_change_theme()

    def watch_theme(self, theme: str) -> None:
        # Fires on any theme change (startup + user-initiated). Save the
        # user-facing picks; startup-restored value is already what's on
        # disk so re-saving is a harmless no-op.
        save_config(theme=theme)

    def action_keybindings(self) -> None:
        code_view = self.query_one("#code-view", CodeView)

        def on_scheme_change(scheme: str) -> None:
            code_view.keybindings = KeybindingConfig.from_scheme(scheme)
            self._keybindings = scheme
            save_config(keybindings=scheme)

        self.push_screen(_KeybindingsModal(code_view.keybindings, on_scheme_change))

    def action_documentation(self) -> None:
        readme = find_readme()
        if readme is None:
            self.notify(
                "README.md not found in the installation.",
                title="Documentation", severity="warning",
            )
            return
        self.push_screen(_DocumentationModal(readme))

    def action_about(self) -> None:
        body = dedent(f"""\
            [bold]tdb v{tdb_version}[/bold]
            by Al Danial (with Claude Code)
            Copyright (c) 2026

            GitHub: https://github.com/AlDanial/tdb
            PyPI  : https://pypi.org/project/textual-debugger/

            A TUI Python debugger based on debugpy and textual""")
        self.push_screen(_AboutModal(body))

    # --- Async tasks ---

    def on_menu_bar_action_label_clicked(self, message: MenuBar.ActionLabelClicked) -> None:
        if message.label_id == "open-file-label":
            self.action_open_file()
        elif message.label_id == "threads-label":
            self._open_threads()
        elif message.label_id == "processes-label":
            self._open_processes()
        elif message.label_id == "async-tasks-label":
            self._open_async_tasks()

    # --- Menu-bar keyboard shortcuts (Alt+first-letter) ---

    def _close_open_menu(self) -> None:
        """Close any open dropdown before firing a different menu action."""
        try:
            self.query_one("#menu-bar", MenuBar).close_menus()
        except Exception:
            pass

    def action_menu_file(self) -> None:
        self._close_open_menu()
        self.action_open_file()

    def action_menu_configure(self) -> None:
        self.query_one("#menu-bar", MenuBar).open_menu("Configure")

    def action_menu_threads(self) -> None:
        self._close_open_menu()
        self._open_threads()

    def action_menu_processes(self) -> None:
        self._close_open_menu()
        self._open_processes()

    def action_menu_async_tasks(self) -> None:
        self._close_open_menu()
        self._open_async_tasks()

    def action_menu_help(self) -> None:
        self.query_one("#menu-bar", MenuBar).open_menu("Help")

    @work(exclusive=True, group="async-tasks")
    # --- Inspection workflows (delegate to InspectionWorkflows) ---------
    # The `@work` decorator must wrap a method on this class because
    # Textual's worker manager is owned by the App. Every body below is
    # a thin forward to `self._inspection`, which holds the real logic.

    async def _fetch_async_task_count(self) -> None:
        await self._inspection.fetch_async_task_count()

    @work(exclusive=True, group="async-tasks-open")
    async def _open_async_tasks(self) -> None:
        await self._inspection.open_async_tasks()

    async def on_tdb_app_refresh_async_tasks(self, message: RefreshAsyncTasks) -> None:
        await self._inspection.refresh_async_tasks()

    async def on_async_tasks_modal_load_task_variables(
        self, message: AsyncTasksModal.LoadTaskVariables
    ) -> None:
        await self._inspection.load_task_variables(message.task_name)

    def _update_thread_count(self) -> None:
        self._inspection.update_thread_count()

    @work(exclusive=True, group="threads-open")
    async def _open_threads(self) -> None:
        await self._inspection.open_threads()

    async def on_threads_modal_load_thread_detail(
        self, message: ThreadsModal.LoadThreadDetail,
    ) -> None:
        await self._inspection.load_thread_detail(message.thread_id)

    async def on_threads_modal_refresh_threads(
        self, message: ThreadsModal.RefreshThreads,
    ) -> None:
        await self._inspection.refresh_threads()

    @work(exclusive=True, group="process-count")
    async def _fetch_process_count(self) -> None:
        await self._inspection.fetch_process_count()

    def _open_processes(self) -> None:
        if self._inspection.open_processes_modal():
            self._open_processes_worker()

    @work(exclusive=True, group="processes-open")
    async def _open_processes_worker(self) -> None:
        await self._inspection.open_processes_worker()

    async def on_processes_modal_refresh_processes(
        self, message: ProcessesModal.RefreshProcesses,
    ) -> None:
        await self._inspection.refresh_processes()

    async def on_processes_modal_load_process_detail(
        self, message: ProcessesModal.LoadProcessDetail,
    ) -> None:
        await self._inspection.load_process_detail(message.pid)

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
        # Idempotent: Ctrl+Q and the q-confirm path both land here, and
        # `controller.stop()` can take a moment when many child DAP
        # sessions need to be torn down — without this guard, a second
        # keypress would spawn another concurrent shutdown.
        if self._is_quitting:
            return
        self._is_quitting = True
        # Save this program's breakpoints under its own key, preserving
        # breakpoints saved for other programs.
        if self._program:
            program_key = str(Path(self._program).resolve())
            save_breakpoints(self.controller.state.breakpoints, program=program_key)
        await self.controller.stop()
        if hasattr(self, '_uvicorn_server'):
            self._uvicorn_server.should_exit = True
        self.exit()

    # Threshold for what counts as a "render frame" vs a "control sequence"
    # in the writer thread's queue. Restoration sequences from
    # `stop_application_mode` (alt-screen exit, cursor restore, etc.) are
    # all short (≤ 8 bytes); render frames are typically thousands of bytes.
    _DRAIN_BYTES_KEEP_BELOW = 64

    def _drain_writer_queue_fast(self) -> int:
        """Discard queued render frames from the writer thread.

        Returns the number of frames dropped (for tests).

        Why: at quit time Textual's writer thread can have ~25 queued render
        frames waiting to be flushed to the pty, with the alt-screen-exit
        sequence at the *end* of that backlog. The writer drains FIFO, so
        the user's terminal stays in alt-screen mode while every queued
        frame is written through — on a busy or slow pty that's many
        seconds. Tdb appears to "close" (no longer rendering) but its
        process keeps running, the terminal stays unresponsive, and the
        user has to Ctrl+C to recover.

        Those queued frames would all be over-painted by the
        alt-screen-exit anyway, so dropping them is purely cosmetic and
        lets the writer thread join in milliseconds instead.
        """
        driver = self._driver
        if driver is None:
            return 0
        wt = getattr(driver, "_writer_thread", None)
        if wt is None:
            return 0
        q = wt._queue
        items: list = []
        try:
            while True:
                items.append(q.get_nowait())
        except Exception:
            pass  # queue empty
        kept: list = []
        dropped = 0
        for item in items:
            if item is None or len(item) <= self._DRAIN_BYTES_KEEP_BELOW:
                kept.append(item)
            else:
                dropped += 1
        for item in kept:
            try:
                q.put_nowait(item)
            except Exception:
                break
        return dropped

    async def _shutdown(self) -> None:
        # Replicate Textual's App._shutdown but slip
        # `_drain_writer_queue_fast` in just before `driver.close()` so the
        # writer thread doesn't have to flush a backlog of render frames
        # through a slow pty before joining. See _drain_writer_queue_fast.
        from textual import events as _evts
        self._begin_batch()
        driver = self._driver
        self._running = False
        if driver is not None:
            driver.disable_input()
        await self._close_all()
        await self._close_messages()
        await self._dispatch_message(_evts.Unmount())
        self._drain_writer_queue_fast()
        if self._driver is not None:
            self._driver.close()
        self._nodes._clear()
        if self.devtools is not None and self.devtools.is_connected:
            await self._disconnect_devtools()
        self._print_error_renderables()

    def action_confirm_quit(self) -> None:
        # Don't reopen the modal once a quit is already in flight; also
        # don't double-push it if it's currently the active screen.
        if self._is_quitting or isinstance(self.screen, _QuitConfirmModal):
            return

        def on_dismiss(confirmed: bool | None) -> None:
            if confirmed:
                self.run_worker(self.action_quit_debugger())

        self.push_screen(_QuitConfirmModal(), callback=on_dismiss)



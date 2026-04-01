"""Main textual App for tdbg."""

from __future__ import annotations

import logging

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Footer, Header, OptionList
from textual.widgets._tree import TreeNode

from tdbg.session.controller import (
    DebugController,
    DapInitialized,
    DapStopped,
    DapContinued,
    DapTerminated,
    DapExited,
    DapOutput,
)
from tdbg.widgets.breakpoint_view import BreakpointView
from tdbg.widgets.code_view import CodeView
from tdbg.widgets.console_view import ConsoleView
from tdbg.widgets.evaluate_console import EvaluateConsole
from tdbg.widgets.menu_bar import MenuBar, _MenuDropdown
from tdbg.widgets.stack_view import StackView
from tdbg.widgets.variable_view import VariableView

log = logging.getLogger(__name__)


class TdbgApp(App):
    """TUI Python debugger."""

    TITLE = "tdbg"
    SUB_TITLE = "Python Debugger"

    CSS = """
    Screen {
        layers: default above;
    }

    #main {
        height: 1fr;
    }

    #left-panel {
        width: 2fr;
    }

    #right-panel {
        width: 1fr;
    }

    #code-view {
        height: 3fr;
        border: solid $primary;
        border-title-color: $text;
    }

    #eval-console {
        height: 1fr;
    }

    #right-panel > * {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit_debugger", "Quit"),
        Binding("escape", "focus_code", "Code View", show=False),
    ]

    # --- Custom messages for UI updates ---

    class BreakpointsChanged(Message):
        pass

    class LazyLoadVariables(Message):
        def __init__(self, variables_reference: int, node: TreeNode[int]) -> None:
            self.variables_reference = variables_reference
            self.node = node
            super().__init__()

    def __init__(
        self,
        program: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        stop_on_entry: bool = False,
        just_my_code: bool = True,
        python: str | None = None,
    ) -> None:
        super().__init__()
        self._program = program
        self._args = args
        self._cwd = cwd
        self._stop_on_entry = stop_on_entry
        self._just_my_code = just_my_code
        self._python = python
        self.controller = DebugController(self)

    def compose(self) -> ComposeResult:
        yield Header()
        yield MenuBar(
            {
                "File": ["Open"],
                "Configure": ["Color Theme", "Keybindings"],
                "Help": ["Documentation", "About"],
            },
            id="menu-bar",
        )
        with Horizontal(id="main"):
            with Vertical(id="left-panel"):
                yield CodeView(id="code-view")
                yield EvaluateConsole(id="eval-console")
            with Vertical(id="right-panel"):
                yield ConsoleView(id="console-view")
                yield VariableView(id="variable-view")
                yield StackView(id="stack-view")
                yield BreakpointView(id="breakpoint-view")
        yield Footer()

    def on_mount(self) -> None:
        code_view = self.query_one("#code-view", CodeView)
        code_view.border_title = "Code"
        code_view.load_file(self._program)
        code_view.focus()
        self._start_session()

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
            )
        except Exception:
            log.exception("Failed to start debug session")
            self.sub_title = "Failed to start"

    # --- DAP event message handlers ---
    # These run in textual's message loop, so async is safe.

    async def on_dap_initialized(self, message: DapInitialized) -> None:
        log.info("on_dap_initialized called")
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
            self._update_ui_state()
        except Exception:
            log.exception("Error handling stopped event")

    def on_dap_continued(self, message: DapContinued) -> None:
        try:
            state = self.controller.state
            state.is_running = True
            state.clear_frame_data()
            self._update_ui_state()
        except Exception:
            log.exception("Error handling continued event")

    def on_dap_terminated(self, message: DapTerminated) -> None:
        try:
            state = self.controller.state
            state.is_terminated = True
            state.is_running = False
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

    def on_dap_output(self, message: DapOutput) -> None:
        try:
            console = self.query_one("#console-view", ConsoleView)
            console.write_output(message.text, message.category)
        except Exception:
            log.exception("Error handling output event")

    # --- UI update helper ---

    def _update_ui_state(self) -> None:
        state = self.controller.state
        code_view = self.query_one("#code-view", CodeView)

        if state.is_terminated:
            self.sub_title = "Terminated"
            code_view.current_line = None
            return

        if state.is_running:
            self.sub_title = "Running..."
            code_view.current_line = None
            return

        # Stopped
        reason = state.stop_reason or "stopped"
        self.sub_title = f"Stopped ({reason})"

        source_path = state.get_current_source_path()
        current_line = state.get_current_line()

        if source_path and source_path != code_view.source_path:
            code_view.load_file(source_path)
        if source_path:
            bps = state.breakpoints.get(source_path, [])
            code_view.set_breakpoints(bps)
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

    async def on_code_view_debug_action(self, message: CodeView.DebugAction) -> None:
        log.info("on_code_view_debug_action called: %s", message.action)
        try:
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

    def on_tdbg_app_breakpoints_changed(self, message: BreakpointsChanged) -> None:
        state = self.controller.state
        code_view = self.query_one("#code-view", CodeView)
        if code_view.source_path:
            bps = state.breakpoints.get(code_view.source_path, [])
            code_view.set_breakpoints(bps)
        bp_view = self.query_one("#breakpoint-view", BreakpointView)
        bp_view.update_breakpoints(state.breakpoints)

    async def on_tdbg_app_lazy_load_variables(self, message: LazyLoadVariables) -> None:
        variables = await self.controller.client.variables(message.variables_reference)
        var_view = self.query_one("#variable-view", VariableView)
        var_view.load_children(message.node, variables)

    async def on_evaluate_console_evaluate_requested(
        self, message: EvaluateConsole.EvaluateRequested
    ) -> None:
        result = await self.controller.evaluate(message.expression)
        eval_console = self.query_one("#eval-console", EvaluateConsole)
        eval_console.show_result(result)

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
        self.notify("Keybindings: not yet implemented", title="Configure")

    def action_documentation(self) -> None:
        self.notify("tdbg — TUI Python Debugger\n\nKeys: n=step over, s=step in, o=step out, c=continue, b=breakpoint, p=pause", title="Documentation")

    def action_about(self) -> None:
        self.notify("tdbg v0.1.0\nA TUI-based Python debugger\nPowered by debugpy + textual", title="About")

    # --- Actions ---

    def action_focus_code(self) -> None:
        self.query_one("#code-view", CodeView).focus()

    async def action_quit_debugger(self) -> None:
        await self.controller.stop()
        self.exit()

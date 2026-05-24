"""External-terminal launching for the `--terminal` option.

When the user passes `--terminal X` to tdb, the debuggee runs in a
fresh terminal emulator window instead of inheriting tdb's TTY. That
keeps debuggee stdout/stderr/input separate from the TUI — important
for programs that themselves draw to the terminal (textual apps,
prompt-toolkit, curses).

The plumbing is small and self-contained:
  - a table mapping CLI choice → (executable, exec-flag) for each
    supported emulator
  - a helper that resolves the executable on PATH
  - a `TerminalLauncher` whose `handle_run_in_terminal` is registered
    as the debugpy `runInTerminal` reverse-request handler, so when
    debugpy asks the adapter to spawn the debuggee, we spawn the
    chosen emulator with the debuggee command nested inside.

Previously this code lived inside `DebugController`, but it shares no
state with the rest of the controller (only the user's `--terminal`
choice). Lifting it out is the cleanest of the controller-split steps.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from tdb.dap.messages import Request


log = logging.getLogger(__name__)


# Maps the CLI --terminal choice to (executable name, flags before the command).
# The flags accept the remaining args as the command + arguments.
_TERMINAL_SPECS: dict[str, tuple[str, list[str]]] = {
    "xterm": ("xterm", ["-e"]),
    "konsole": ("konsole", ["-e"]),
    "gnome-terminal": ("gnome-terminal", ["--wait", "--"]),
    "ghostty": ("ghostty", ["-e"]),
    "kitty": ("kitty", []),
    "iterm2": ("iterm2", ["-e"]),
    "warp": ("warp", ["-e"]),
    "wezterm": ("wezterm", ["start", "--"]),
    "terminator": ("terminator", ["-x"]),
}


def resolve_terminal(choice: str) -> str:
    """Resolve a --terminal choice to a full executable path."""
    spec = _TERMINAL_SPECS.get(choice)
    if spec is None:
        raise RuntimeError(f"Unknown terminal choice: {choice}")
    exe, _ = spec
    path = shutil.which(exe)
    if not path:
        raise RuntimeError(
            f"Terminal '{choice}' not found on PATH. "
            f"Install it or pick a different --terminal.",
        )
    return path


def build_terminal_cmd(choice: str, args: list[str]) -> list[str]:
    """Build the command to launch the chosen terminal running args."""
    path = resolve_terminal(choice)
    _, exec_flag = _TERMINAL_SPECS[choice]
    return [path] + exec_flag + args


class TerminalLauncher:
    """Handles debugpy's `runInTerminal` reverse request.

    Registered on the parent DAPClient by `DebugController.start` when
    `--terminal` is set. debugpy asks us to spawn the debuggee in a
    user-visible terminal; we wrap the debuggee command with the
    chosen emulator's exec syntax and spawn that instead.
    """

    def __init__(
        self,
        terminal_choice: str,
        on_started: Callable[[], None] | None = None,
    ) -> None:
        self._choice = terminal_choice
        # Called after the subprocess is spawned. Wires the ConsoleView's
        # "Debuggee running in external terminal window…" hint without
        # requiring the launcher to know about the event-handler stack.
        self._on_started = on_started

    async def handle_run_in_terminal(self, request: Request) -> dict[str, Any]:
        """debugpy reverse-request handler for `runInTerminal`."""
        cmd_args: list[str] = request.arguments.get("args", [])
        cwd = request.arguments.get("cwd")
        env = request.arguments.get("env")

        full_cmd = build_terminal_cmd(self._choice, cmd_args)
        log.info("Launching external terminal: %s", full_cmd)

        # Merge request env into current env so the debuggee inherits
        # PATH, DISPLAY, etc. needed to connect back to debugpy.
        merged_env = {**os.environ, **(env or {})}

        await asyncio.create_subprocess_exec(
            *full_cmd,
            cwd=cwd,
            env=merged_env,
            start_new_session=True,
        )

        if self._on_started is not None:
            self._on_started()

        # Don't return processId — for terminals that fork-and-exit
        # (like gnome-terminal), the PID is meaningless and confuses
        # debugpy.
        return {}

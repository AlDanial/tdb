"""Client-side `runInTerminal` handlers: external terminals and pipes.

When the user passes `--terminal X` to tdb, the debuggee runs in a
fresh terminal emulator window instead of inheriting tdb's TTY. That
keeps debuggee stdout/stderr/input separate from the TUI — important
for programs that themselves draw to the terminal (textual apps,
prompt-toolkit, curses). `TerminalLauncher` does that.

`PipeLauncher` answers the same reverse request without a terminal: it
spawns the debuggee command on pipes tdb owns, so the Console view can
both show the program's output and feed its stdin (see the class
docstring for why debugpy needs this).

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
import codecs
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


class PipeLauncher:
    """Handles `runInTerminal` by spawning the debuggee ourselves on pipes.

    debugpy's `internalConsole` mode spawns its launcher with the
    *adapter's* stdin — tdb's DAP pipe — so the debuggee's `input()`
    hits EOF and tdb has no handle on the program's stdin at all (DAP
    has no "send stdin" request). Asking debugpy for an external
    terminal instead makes it hand the launch command to us, and we
    spawn it with stdin/stdout/stderr as pipes we own: stdout/stderr
    flow into the Console view through `on_output` exactly as before,
    and `write_stdin` feeds the program.

    `inherit_stdin=True` (headless run/eval modes) leaves fd 0 alone so
    `tdb --run prog.py` reads input straight from the user's terminal.
    """

    def __init__(
        self,
        on_output: Callable[[str, str], None],
        *,
        inherit_stdin: bool = False,
    ) -> None:
        self._on_output = on_output
        self._inherit_stdin = inherit_stdin
        self._process: asyncio.subprocess.Process | None = None
        self._pump_tasks: list[asyncio.Task[None]] = []
        self._exit_task: asyncio.Task[None] | None = None
        self._stdin_closed = False

    @property
    def exited(self) -> bool:
        return self._process is not None and self._process.returncode is not None

    @property
    def supports_stdin(self) -> bool:
        return not self._inherit_stdin

    async def handle_run_in_terminal(self, request: Request) -> dict[str, Any]:
        """debugpy reverse-request handler for `runInTerminal`."""
        cmd_args: list[str] = request.arguments.get("args", [])
        cwd = request.arguments.get("cwd")
        env = request.arguments.get("env")
        merged_env = {**os.environ, **(env or {})}
        # debugpy sets this itself in internalConsole mode; a pipe is
        # block-buffered, so without it a print() before input() would
        # only show once the buffer flushed. Harmless for non-Python.
        merged_env["PYTHONUNBUFFERED"] = "1"

        spawn_kwargs: dict[str, Any] = {}
        if os.name == "nt":
            import subprocess

            spawn_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            # New session: debugpy's launcher tries to make the debuggee
            # the terminal's foreground process group, which would steal
            # the TUI's terminal. With no controlling tty that is a no-op.
            spawn_kwargs["start_new_session"] = True

        log.info("Launching debuggee on pipes: %s", cmd_args)
        self._process = await asyncio.create_subprocess_exec(
            *cmd_args,
            cwd=cwd,
            env=merged_env,
            stdin=None if self._inherit_stdin else asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **spawn_kwargs,
        )
        assert self._process.stdout is not None and self._process.stderr is not None
        self._pump_tasks = [
            asyncio.create_task(self._pump(self._process.stdout, "stdout")),
            asyncio.create_task(self._pump(self._process.stderr, "stderr")),
        ]
        self._exit_task = asyncio.create_task(self._watch_exit())
        # No processId: debugpy only needs the launcher to connect back.
        return {}

    async def _watch_exit(self) -> None:
        """Release the stdin transport as soon as the program is gone, so
        it isn't left for garbage collection after the loop has closed."""
        assert self._process is not None
        await self._process.wait()
        self.close_stdin()

    async def _pump(self, stream: asyncio.StreamReader, category: str) -> None:
        # Chunked reads, not readline(): a prompt like "enter a value: "
        # has no newline and must reach the Console view before the
        # program blocks waiting for the answer.
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                tail = decoder.decode(b"", final=True)
                if tail:
                    self._on_output(tail, category)
                return
            text = decoder.decode(chunk)
            if text:
                self._on_output(text, category)

    def write_stdin(self, text: str) -> None:
        """Feed `text` to the debuggee's stdin. Raises RuntimeError when
        there is no writable stdin (inherited, closed, or program gone)."""
        proc = self._process
        if proc is None or proc.stdin is None:
            raise RuntimeError("the program's stdin is not available")
        if self._stdin_closed:
            raise RuntimeError("the program's stdin has been closed")
        if proc.returncode is not None:
            raise RuntimeError("the program has exited")
        proc.stdin.write(text.encode("utf-8"))

    def close_stdin(self) -> None:
        """Send EOF (the Ctrl+D of a terminal)."""
        proc = self._process
        if proc is None or proc.stdin is None or self._stdin_closed:
            return
        self._stdin_closed = True
        proc.stdin.close()

    async def close(self) -> None:
        """Release the pipes. The adapter owns the debuggee's lifetime
        (disconnect/terminate reaches it through the launcher), so this
        never kills; it just stops pumping once the process is gone."""
        self.close_stdin()
        tasks = [*self._pump_tasks]
        if self._exit_task is not None:
            tasks.append(self._exit_task)
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._pump_tasks = []
        self._exit_task = None

"""Owns a single tdb debug session for the MCP server process.

One MCP server process serves one stdio client, which drives one debug
session at a time. This class is the third in-process consumer of
`RpcHandlers` (after the TUI and FastAPI server); it mirrors the
launch / attach sequence in `server.runner.run_headless` minus the
uvicorn part (FastMCP owns the event loop instead).

Tool wrappers in `tdb.mcp.server` call `_call(action, params)` to
dispatch through `RpcHandlers.dispatch_table()`. The dispatcher's lock
policy — including the `pause` bypass — is preserved here so MCP
agents have the same interrupt semantics as HTTP clients.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from tdb._timeouts import DAP_INITIALIZED, DAP_STOP_ON_ENTRY
from tdb.server.app import NO_LOCK_ACTIONS
from tdb.server.event_handler import ServerEventHandler
from tdb.server.handlers import ControllerRef, RpcHandlers
from tdb.server.rpc_types import RpcResponse
from tdb.session.controller import DebugController

if TYPE_CHECKING:
    from tdb.languages.base import LanguageProfile

log = logging.getLogger(__name__)


class McpSession:
    """One debug session, lazily created by `launch()` or `attach()`.

    Before either is called, `_call` returns a clear "no session" error
    so the agent gets actionable guidance instead of an AttributeError.
    """

    def __init__(self) -> None:
        self._controller: DebugController | None = None
        self._handler: ServerEventHandler | None = None
        self._handlers: RpcHandlers | None = None

    @property
    def is_active(self) -> bool:
        return (
            self._handlers is not None
            and self._controller is not None
            and not self._controller.state.is_terminated
        )

    @property
    def handlers(self) -> RpcHandlers | None:
        return self._handlers

    async def launch(
        self,
        program: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        stop_on_entry: bool = False,
        just_my_code: bool = True,
        python: str | None = None,
        breakpoints: list[tuple[str, int]] | None = None,
        lang: str | None = None,
        adapter: str | None = None,
    ) -> str:
        """Start a debug session on a local program. Returns a status
        line summarizing where the debuggee landed (entry or first
        stop). Errors if a session is already active — call `quit`
        first to start a new one. `lang`/`adapter` select a non-Python
        LanguageProfile (mirrors the CLI's --lang/--adapter); omit both
        for Python (the default)."""
        if self.is_active:
            return (
                "A debug session is already active. Call `quit` before "
                "starting a new one."
            )

        profile = self._resolve_profile(program, lang, adapter, python)
        self._build_handlers(profile=profile)
        assert self._controller is not None and self._handler is not None

        # Mirror server/runner.py::run_headless. config persists across
        # restart in the HTTP server path; MCP is simpler — no restart
        # surface yet — so we just load step_mode and move on.
        from tdb.persist import load_config

        self._controller.step_mode = load_config().step_mode

        self._apply_cli_breakpoints(breakpoints)

        await self._controller.start(
            program=program,
            args=args,
            cwd=cwd or str(Path.cwd()),
            stop_on_entry=stop_on_entry,
            just_my_code=just_my_code,
            python=python,
        )
        return await self._finish_initialize(stop_on_entry=stop_on_entry)

    async def attach(
        self,
        host: str,
        port: int,
        breakpoints: list[tuple[str, int]] | None = None,
        path_mappings: list[tuple[str, str]] | None = None,
        program: str | None = None,
        lang: str | None = None,
        adapter: str | None = None,
    ) -> str:
        """Attach to a remote debuggee using the selected DAP adapter.

        Native Rust attach requires `program` to supply local symbols.
        `path_mappings` translates local and remote source roots.
        """
        if self.is_active:
            return (
                "A debug session is already active. Call `quit` before "
                "attaching to a new one."
            )

        profile = self._resolve_profile(program, lang, adapter)
        if profile.id == "rust":
            if program is None:
                raise ValueError("Rust remote attach requires a local program")
            program_path = Path(program).resolve()
            if not program_path.is_file():
                raise ValueError(
                    "Rust remote attach requires an existing local executable: "
                    f"{program}"
                )
            program = str(program_path)

        self._build_handlers(profile=profile)
        assert self._controller is not None

        self._apply_cli_breakpoints(breakpoints)

        await self._controller.remote_attach(
            host=host,
            port=port,
            path_mappings=path_mappings,
            program=program,
        )
        # Remote-attach produces a stopped event via the pre-arm pause
        # in controller.do_configure — same flow run_headless uses.
        return await self._finish_initialize(stop_on_entry=True, remote=(host, port))

    async def quit(self) -> str:
        """Tear down the current session. Safe to call when no session
        is active — returns a benign message in that case."""
        if self._controller is None:
            return "No active debug session."
        try:
            await self._controller.stop()
        except Exception:
            log.exception("Error stopping controller on quit")
        self._controller = None
        self._handler = None
        self._handlers = None
        return "Session stopped."

    async def _call(self, action: str, params: list) -> RpcResponse:
        """Dispatch an action through RpcHandlers with the same lock
        policy as the HTTP dispatcher — `pause` (and any other entry
        in NO_LOCK_ACTIONS) bypasses `session_lock` so it can interrupt
        an in-flight blocking step / continue / wait_for_stop."""
        if self._handlers is None:
            return RpcResponse.error(
                "No active debug session. Call `debug_launch` or `debug_attach` first."
            )
        table = self._handlers.dispatch_table()
        fn = table.get(action)
        if fn is None:
            return RpcResponse.error(f"Unknown action: {action}")
        try:
            if action in NO_LOCK_ACTIONS:
                return await fn(params)
            async with self._handlers.session_lock:
                return await fn(params)
        except Exception as e:
            log.exception("MCP action '%s' failed", action)
            return RpcResponse.error(str(e))

    # --- internals ----------------------------------------------------

    def _resolve_profile(
        self,
        program: str | None,
        lang: str | None,
        adapter: str | None,
        python: str | None = None,
    ) -> "LanguageProfile":
        """Mirror cli.py's _resolve_language for the MCP launch path.
        Always detects/resolves a profile — `lang`/`adapter` override
        auto-detection from `program`, but omitting both still runs
        `registry.detect(program)` so non-Python programs (e.g. an ELF
        binary) aren't silently launched under debugpy.
        `registry.detect(None)` returns "python", so attach-mode/no-program
        callers still get the Python profile.

        Raises ValueError if `python` (interpreter override) was
        supplied for a non-Python profile — mirrors cli.py's
        --python/--pv validation (cli.py:358-365), which rejects the
        same combination at the CLI layer."""
        from tdb.languages import registry
        from tdb.persist import load_config

        config = load_config()
        lang_id = lang or registry.detect(program)
        resolved_adapter = adapter or config.default_adapters.get(lang_id)
        profile = registry.resolve(
            lang_id,
            adapter=resolved_adapter,
            adapter_paths=config.adapters,
            program=program,
        )
        if profile.id != "python" and python is not None:
            raise ValueError(
                f"the 'python' option applies only to Python debuggees "
                f"(language is {profile.id})"
            )
        return profile

    def _build_handlers(self, profile: "LanguageProfile | None" = None) -> None:
        self._handler = ServerEventHandler()
        self._controller = DebugController(self._handler, profile=profile)
        self._handlers = RpcHandlers(ControllerRef(self._controller), self._handler)

    def _apply_cli_breakpoints(self, breakpoints: list[tuple[str, int]] | None) -> None:
        if not breakpoints or self._controller is None:
            return
        self._controller.state.install_cli_breakpoints(
            [(path, line, True) for path, line in breakpoints]
        )

    async def _finish_initialize(
        self,
        stop_on_entry: bool,
        remote: tuple[str, int] | None = None,
    ) -> str:
        """Mirror run_headless: wait for `initialized`, send
        configurationDone, then optionally wait for the first stop
        (stop_on_entry or remote-attach pre-arm pause)."""
        assert self._controller is not None and self._handler is not None
        await asyncio.wait_for(
            self._handler.initialized_event.wait(), timeout=DAP_INITIALIZED
        )
        await self._controller.do_configure()

        if stop_on_entry:
            stopped = await self._handler.wait_for_stop(timeout=DAP_STOP_ON_ENTRY)
            if not stopped:
                return (
                    f"Session started but no stop arrived within "
                    f"{DAP_STOP_ON_ENTRY:.0f}s."
                )
            await self._controller.fetch_stop_info()
            loc, line = self._controller.state.get_stop_location()
            tag = f"attached to {remote[0]}:{remote[1]}" if remote else "launched"
            return f"Session {tag}; stopped at {loc}:{line}"
        return "Session launched; debuggee is running."

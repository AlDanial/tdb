"""Headless debug server runner."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import uvicorn

from tdb.session.controller import DebugController
from .app import create_app
from .event_handler import ServerEventHandler
from .handlers import ControllerRef, RpcHandlers

log = logging.getLogger(__name__)


async def run_headless(
    program: str,
    args: list[str] | None = None,
    cwd: str | None = None,
    stop_on_entry: bool = False,
    just_my_code: bool = True,
    python: str | None = None,
    port: int = 8150,
    host: str = "127.0.0.1",
    cli_breakpoints: list[tuple[str, int]] | None = None,
) -> None:
    """Run the debug server in headless mode (no TUI).

    Starts the debugpy session and the FastAPI server on the same event loop.
    """
    handler = ServerEventHandler()
    controller = DebugController(handler)
    from tdb.persist import load_config

    controller.step_mode = load_config().step_mode

    # Apply CLI breakpoints
    if cli_breakpoints:
        from tdb.dap.types import SourceBreakpoint

        for bp_path, bp_line in cli_breakpoints:
            bps = controller.state.breakpoints.get(bp_path, [])
            if not any(bp.line == bp_line for bp in bps):
                bps.append(SourceBreakpoint(line=bp_line))
                controller.state.breakpoints[bp_path] = bps

    # Start the debug session
    await controller.start(
        program=program,
        args=args,
        cwd=cwd or str(Path.cwd()),
        stop_on_entry=stop_on_entry,
        just_my_code=just_my_code,
        python=python,
    )

    # Wait for initialized event, then configure
    from tdb._timeouts import DAP_INITIALIZED, DAP_STOP_ON_ENTRY

    await asyncio.wait_for(handler.initialized_event.wait(), timeout=DAP_INITIALIZED)
    await controller.do_configure()

    # If stop_on_entry, wait for the debuggee to actually stop. State
    # (is_running, stop_reason, current_thread_id) is set synchronously
    # by controller._on_stopped — no manual sync needed here.
    if stop_on_entry:
        await handler.wait_for_stop(timeout=DAP_STOP_ON_ENTRY)
        await controller.fetch_stop_info()

    log.info("Debug session ready (headless)")

    # Create and start the FastAPI server
    handlers = RpcHandlers(ControllerRef(controller), handler)
    fastapi_app = create_app(handlers)
    config = uvicorn.Config(
        fastapi_app,
        host=host,
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(config)

    print(f"tdb debug server listening on http://{host}:{port}/rpc")
    print(f"Debugging: {program}")

    await server.serve()

    # Clean up
    try:
        await controller.stop()
    except Exception:
        pass

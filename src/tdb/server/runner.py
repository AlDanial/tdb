"""Headless debug server runner."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn

from tdb.dap.client import DAPError
from tdb.languages.base import AdapterNotFoundError
from tdb.session.controller import DebugController
from .app import create_app
from .event_handler import ServerEventHandler
from .handlers import ControllerRef, RpcHandlers

if TYPE_CHECKING:
    from tdb.languages.base import LanguageProfile

log = logging.getLogger(__name__)


async def setup_headless_session(
    program: str | None,
    args: list[str] | None = None,
    cwd: str | None = None,
    stop_on_entry: bool = False,
    just_my_code: bool = True,
    python: str | None = None,
    cli_breakpoints: list[tuple[str, int, bool]] | None = None,
    attach_host: str | None = None,
    attach_port: int | None = None,
    path_mappings: list[tuple[str, str]] | None = None,
    profile: "LanguageProfile | None" = None,
    step_mode: str | None = None,
    pre_arm_pause: bool = True,
) -> tuple[DebugController, ServerEventHandler]:
    """Set up a headless debug session (no TUI, no server).

    Starts the debugpy session and waits for it to be ready. When
    `attach_host` + `attach_port` are set, attaches to a remote debugpy
    server instead of launching `program` locally. `path_mappings` is
    forwarded to debugpy for bidirectional path translation — see
    `dap/client.py::attach`.
    """
    is_remote = attach_host is not None and attach_port is not None
    if not is_remote and program is None:
        raise ValueError("program is required unless remote attach is used")

    handler = ServerEventHandler()
    controller = DebugController(handler, profile=profile)
    from tdb.persist import load_config

    controller.step_mode = (
        step_mode if step_mode is not None else load_config().step_mode
    )

    # Apply CLI breakpoints
    if cli_breakpoints:
        controller.state.install_cli_breakpoints(cli_breakpoints)

    # Start the debug session
    if is_remote:
        try:
            await controller.remote_attach(
                host=attach_host,
                port=attach_port,
                path_mappings=path_mappings,
                program=program,
                pre_arm_pause=pre_arm_pause,
            )
        except AdapterNotFoundError as exc:
            print(f"tdb: {exc.hint}", file=sys.stderr)
            sys.exit(2)
        except OSError as exc:
            # No server listening, route unreachable, DNS failure, etc.
            # Mirror the TUI's failure mode: clear message on stderr,
            # non-zero exit. Headless has no UI to keep alive on failure.
            print(
                f"tdb: cannot attach to {attach_host}:{attach_port} — "
                f"{exc.strerror or exc}",
                file=sys.stderr,
            )
            sys.exit(2)
    else:
        assert program is not None
        try:
            await controller.start(
                program=program,
                args=args,
                cwd=cwd or str(Path.cwd()),
                stop_on_entry=stop_on_entry,
                just_my_code=just_my_code,
                python=python,
            )
        except AdapterNotFoundError as exc:
            # Adapter executable missing (e.g. lldb-dap / gdb not
            # installed). Mirror the remote-attach OSError handling
            # above: print the install hint instead of a raw traceback.
            print(f"tdb: {exc.hint}", file=sys.stderr)
            sys.exit(2)

    # Wait for initialized event, then configure
    from tdb._timeouts import DAP_INITIALIZED, DAP_STOP_ON_ENTRY

    await asyncio.wait_for(handler.initialized_event.wait(), timeout=DAP_INITIALIZED)
    try:
        await controller.do_configure()
    except DAPError as exc:
        # E.g. a rejected pre-configuration command (`set substitute-path`
        # from --path-mapping). do_configure deliberately aborts rather
        # than binding breakpoints against the wrong source tree; mirror
        # the attach failure modes above instead of dumping a traceback.
        print(f"tdb: adapter configuration failed — {exc}", file=sys.stderr)
        sys.exit(2)

    # Wait for the debuggee to actually stop. State (is_running,
    # stop_reason, current_thread_id) is set synchronously by
    # controller._on_stopped — no manual sync needed here. Remote-attach
    # produces a stop via the pre-arm pause in controller.do_configure,
    # so wait there too.
    if stop_on_entry or is_remote:
        await handler.wait_for_stop(timeout=DAP_STOP_ON_ENTRY)
        await controller.fetch_stop_info()

    log.info("Debug session ready (headless)")

    return controller, handler


async def run_headless(
    program: str | None,
    args: list[str] | None = None,
    cwd: str | None = None,
    stop_on_entry: bool = False,
    just_my_code: bool = True,
    python: str | None = None,
    port: int = 8150,
    host: str = "127.0.0.1",
    cli_breakpoints: list[tuple[str, int, bool]] | None = None,
    attach_host: str | None = None,
    attach_port: int | None = None,
    path_mappings: list[tuple[str, str]] | None = None,
    profile: "LanguageProfile | None" = None,
    pre_arm_pause: bool = True,
) -> None:
    """Run the debug server in headless mode (no TUI).

    Starts the debugpy session and the FastAPI server on the same event loop.

    When `attach_host` + `attach_port` are set, attaches to a remote debugpy
    server instead of launching `program` locally. `path_mappings` is
    forwarded to debugpy for bidirectional path translation — see
    `dap/client.py::attach`.
    """
    controller, handler = await setup_headless_session(
        program,
        args=args,
        cwd=cwd,
        stop_on_entry=stop_on_entry,
        just_my_code=just_my_code,
        python=python,
        cli_breakpoints=cli_breakpoints,
        attach_host=attach_host,
        attach_port=attach_port,
        path_mappings=path_mappings,
        profile=profile,
        pre_arm_pause=pre_arm_pause,
    )

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
    if attach_host is not None and attach_port is not None:
        print(f"Debugging: remote {attach_host}:{attach_port}")
    else:
        print(f"Debugging: {program}")

    await server.serve()

    # Clean up
    try:
        await controller.stop()
    except Exception:
        pass

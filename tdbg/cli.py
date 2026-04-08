"""CLI argument parsing for tdbg."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tdbg",
        description="TUI Python Debugger",
    )
    parser.add_argument(
        "program",
        help="Python script to debug",
    )
    parser.add_argument(
        "args",
        nargs="*",
        help="Arguments to pass to the debugged program",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory for the debugged program",
    )
    parser.add_argument(
        "--stop-on-entry",
        action="store_true",
        help="Stop at the first line of the program",
    )
    parser.add_argument(
        "--no-just-my-code",
        action="store_true",
        help="Also debug library code",
    )
    parser.add_argument(
        "--python",
        default=None,
        help="Python interpreter to use for debugging",
    )
    parser.add_argument(
        "--keybindings",
        choices=["default", "vim", "emacs"],
        default="default",
        help="Keybinding scheme for code navigation",
    )
    parser.add_argument(
        "--external-terminal",
        action="store_true",
        help="Run debuggee in an external terminal (for TUI programs)",
    )
    parser.add_argument(
        "--server",
        action="store_true",
        help="Start JSON-RPC debug server alongside the TUI",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run as a headless JSON-RPC debug server (no TUI)",
    )
    parser.add_argument(
        "--server-port",
        type=int,
        default=8150,
        help="Port for the JSON-RPC debug server (default: 8150)",
    )

    args = parser.parse_args(argv)

    # --headless implies --server
    if args.headless:
        args.server = True

    # Resolve program path
    program_path = Path(args.program).resolve()
    if not program_path.exists():
        parser.error(f"File not found: {args.program}")
    args.program = str(program_path)

    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    import logging
    logging.basicConfig(
        filename="/tmp/tdbg.log",
        level=logging.DEBUG,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if args.headless:
        _run_headless(args)
    else:
        _run_tui(args)


def _run_headless(args: argparse.Namespace) -> None:
    """Run in headless mode: JSON-RPC server only, no TUI."""
    import asyncio
    from tdbg.server.runner import run_headless

    asyncio.run(run_headless(
        program=args.program,
        args=args.args,
        cwd=args.cwd,
        stop_on_entry=args.stop_on_entry,
        just_my_code=not args.no_just_my_code,
        python=args.python,
        port=args.server_port,
    ))


def _run_tui(args: argparse.Namespace) -> None:
    """Run with the TUI (optionally with the server alongside)."""
    from tdbg.app import TdbgApp

    app = TdbgApp(
        program=args.program,
        args=args.args,
        cwd=args.cwd,
        stop_on_entry=args.stop_on_entry,
        just_my_code=not args.no_just_my_code,
        python=args.python,
        external_terminal=args.external_terminal,
        server_port=args.server_port if args.server else None,
    )
    app.run()

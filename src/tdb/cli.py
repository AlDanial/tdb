"""CLI argument parsing for tdb."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tdb",
        description="TUI Python Debugger",
    )
    parser.add_argument(
        "-r", "--remote-attach",
        metavar="[HOST:]PORT",
        default=None,
        help="Attach to a remote debugpy server (e.g. 5678 or localhost:5678)",
    )
    parser.add_argument(
        "program",
        nargs="?",
        default=None,
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
        "--no-stop-on-entry",
        action="store_true",
        dest="no_stop_on_entry",
        help="Do not stop at the first line of the program (default: stop on entry)",
    )
    parser.add_argument(
        "--no-just-my-code",
        action="store_true",
        help="Also debug library code",
    )
    parser.add_argument(
        "--no-subprocess",
        action="store_true",
        help="Disable debugpy's subprocess tracking (use when debugging tdb itself)",
    )
    parser.add_argument(
        "--python",
        default=None,
        help="Python interpreter to use for debugging",
    )
    parser.add_argument(
        "--keybindings",
        choices=["default", "vim", "emacs"],
        default=None,
        help="Keybinding scheme for code navigation (saved to config)",
    )
    parser.add_argument(
        "--terminal",
        choices=[
            "xterm", "konsole", "gnome-terminal", "ghostty", "kitty",
            "iterm2", "warp", "wezterm", "terminator",
        ],
        default=None,
        help="Run debuggee in the named external terminal (for TUI programs)",
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
        "-k", "--breakpoint",
        action="append",
        default=[],
        metavar="FILE:LINE",
        help="Set a breakpoint at FILE:LINE (may be repeated)",
    )
    parser.add_argument(
        "--server-port",
        type=int,
        default=8150,
        help="Port for the JSON-RPC debug server (default: 8150)",
    )
    parser.add_argument(
        "--post-mortem",
        metavar="SNAPSHOT_FILE",
        default=None,
        help=argparse.SUPPRESS,  # invoked by tdb.exception_hook, not the user
    )

    args = parser.parse_args(argv)
    args.stop_on_entry = not args.no_stop_on_entry

    # --headless implies --server
    if args.headless:
        args.server = True

    # --post-mortem short-circuits everything else: no program needed.
    if args.post_mortem:
        return args

    # Parse --remote-attach into (host, port)
    args.attach_host = None
    args.attach_port = None
    if args.remote_attach:
        spec = args.remote_attach
        if ":" in spec:
            host_part, port_part = spec.rsplit(":", 1)
            args.attach_host = host_part or "127.0.0.1"
        else:
            port_part = spec
            args.attach_host = "127.0.0.1"
        try:
            args.attach_port = int(port_part)
        except ValueError:
            parser.error(f"Invalid port in --remote-attach: {spec}")

    # Validate: need either --remote-attach or a program
    if args.remote_attach is None and args.program is None:
        parser.error("either a program or --remote-attach is required")

    # Resolve program path (only when launching)
    if args.program and not args.remote_attach:
        program_path = Path(args.program).resolve()
        if not program_path.exists():
            parser.error(f"File not found: {args.program}")
        args.program = str(program_path)

    # Parse -k / --breakpoint specs into (resolved_path, line) tuples
    parsed_bps: list[tuple[str, int]] = []
    for spec in args.breakpoint:
        if ":" not in spec:
            parser.error(f"Invalid breakpoint format (expected FILE:LINE): {spec}")
        file_part, line_part = spec.rsplit(":", 1)
        try:
            line = int(line_part)
        except ValueError:
            parser.error(f"Invalid line number in breakpoint: {spec}")
        bp_path = Path(file_part).resolve()
        if not bp_path.is_file():
            parser.error(f"Breakpoint file not found: {file_part}")
        parsed_bps.append((str(bp_path), line))
    args.breakpoint = parsed_bps

    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    import logging
    import os
    from tdb.persist import CONFIG_DIR
    # Tests set TDB_LOG_DIR to keep their log noise out of the user's
    # config dir; production reads from CONFIG_DIR (XDG on Unix,
    # %APPDATA%/tdb on Windows).
    log_dir = Path(os.environ.get("TDB_LOG_DIR") or CONFIG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_dir / "tdb.log"),
        level=logging.DEBUG,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if args.post_mortem:
        _run_post_mortem(args)
    elif args.headless:
        _run_headless(args)
    else:
        _run_tui(args)


def _run_post_mortem(args: argparse.Namespace) -> None:
    """Load a snapshot written by tdb.exception_hook and display it."""
    import json
    from tdb.app import TdbApp
    from tdb.persist import load_keybinding_scheme

    with open(args.post_mortem) as f:
        snapshot = json.load(f)

    keybindings = args.keybindings or load_keybinding_scheme() or "vim"

    app = TdbApp(
        program="",
        keybindings=keybindings,
        post_mortem_snapshot=snapshot,
    )
    app.run()


def _run_headless(args: argparse.Namespace) -> None:
    """Run in headless mode: JSON-RPC server only, no TUI."""
    import asyncio
    from tdb.server.runner import run_headless

    asyncio.run(run_headless(
        program=args.program,
        args=args.args,
        cwd=args.cwd,
        stop_on_entry=args.stop_on_entry,
        just_my_code=not args.no_just_my_code,
        python=args.python,
        port=args.server_port,
        cli_breakpoints=args.breakpoint,
    ))


def _run_tui(args: argparse.Namespace) -> None:
    """Run with the TUI (optionally with the server alongside)."""
    from tdb.app import TdbApp
    from tdb.persist import load_keybinding_scheme, save_config

    # CLI flag overrides saved config; if neither, default to "vim"
    keybindings = args.keybindings
    if keybindings is None:
        keybindings = load_keybinding_scheme() or "vim"
    else:
        save_config(keybindings=keybindings)

    app = TdbApp(
        program=args.program or "",
        args=args.args,
        cwd=args.cwd,
        stop_on_entry=args.stop_on_entry,
        just_my_code=not args.no_just_my_code,
        python=args.python,
        terminal=args.terminal,
        keybindings=keybindings,
        cli_breakpoints=args.breakpoint,
        attach_host=args.attach_host,
        attach_port=args.attach_port,
        sub_process=not args.no_subprocess,
        server_port=args.server_port if args.server else None,
    )
    app.run()

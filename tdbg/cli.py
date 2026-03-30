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

    args = parser.parse_args(argv)

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

    from tdbg.app import TdbgApp

    app = TdbgApp(
        program=args.program,
        args=args.args,
        cwd=args.cwd,
        stop_on_entry=args.stop_on_entry,
        just_my_code=not args.no_just_my_code,
        python=args.python,
    )
    app.run()

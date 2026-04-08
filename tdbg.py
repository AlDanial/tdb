#!/usr/bin/env python3
#!/usr/bin/env python3 -Xfrozen_modules=off
"""Wrapper script for tdbg: launches the TUI debugger with --stop-on-entry by default.

Usage:
    python tdbg.py script.py [args...]
    python tdbg.py --no-stop-on-entry script.py
"""

import os
import sys
import argparse
from pathlib import Path

# os.environ['PYDEVD_DISABLE_FILE_VALIDATION'] = '1'


def main():
    parser = argparse.ArgumentParser(
        description="TUI Python Debugger",
        usage="%(prog)s [options] script.py [args...]",
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
        "--no-stop-on-entry",
        action="store_true",
        help="Don't stop at the first line (default: stop on entry)",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory for the debugged program",
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

    args = parser.parse_args()
#   args.no_just_my_code = True

    # Resolve program path
    program_path = Path(args.program).resolve()
    if not program_path.exists():
        parser.error(f"File not found: {args.program}")

    # Build the command
    venv_python = Path(__file__).resolve().parent / ".venv" / "bin" / "python"
    if not venv_python.exists():
        venv_python = Path(sys.executable)

    cmd = [str(venv_python), "-m", "tdbg"]
    if not args.no_stop_on_entry:
        cmd.append("--stop-on-entry")
    if args.no_just_my_code:
        cmd.append("--no-just-my-code")
    if args.cwd:
        cmd.extend(["--cwd", args.cwd])
    if args.python:
        cmd.extend(["--python", args.python])
    if args.external_terminal:
        cmd.append("--external-terminal")
    if args.headless:
        cmd.append("--headless")
    elif args.server:
        cmd.append("--server")
    if args.server or args.headless:
        cmd.extend(["--server-port", str(args.server_port)])
    cmd.append(str(program_path))
    if args.args:
        cmd.extend(args.args)

    venv_python_str = str(venv_python)
    os.execv(venv_python, cmd)


if __name__ == "__main__":
    main()

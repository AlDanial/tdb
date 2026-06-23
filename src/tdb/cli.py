"""CLI argument parsing for tdb."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


def _get_version() -> str:
    from tdb import __version__

    return __version__


def build_parser() -> argparse.ArgumentParser:
    """Construct the tdb ArgumentParser.

    Pure-declarative: all option definitions live here, no validation
    or interpretation. Validation happens in `_post_process` below.
    Split out from `parse_args` so each piece is independently
    testable and the file is grep-able by concern.
    """
    parser = argparse.ArgumentParser(
        prog="tdb",
        description="A Python debugger built with textual and debugpy.",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"tdb {_get_version()}",
    )
    parser.add_argument(
        "-r",
        "--remote-attach",
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
        "--pv",
        dest="python",
        action="store_const",
        const=".venv/bin/python",
        help="Shorthand for --python .venv/bin/python",
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
            "xterm",
            "konsole",
            "gnome-terminal",
            "ghostty",
            "kitty",
            "iterm2",
            "warp",
            "wezterm",
            "terminator",
        ],
        default=None,
        help="Run debuggee in the named external terminal (for TUI programs)",
    )
    parser.add_argument(
        "--local-root",
        action="append",
        default=[],
        metavar="PATH",
        help="Local directory containing a copy of code from the remote "
        "debuggee. Pair with --remote-root: each --local-root is zipped "
        "in CLI order with the matching --remote-root, so the two flags "
        "must be supplied in equal numbers. Used for remote-attach mode "
        "(-r); debugpy translates paths bidirectionally so tdb reads "
        "local files instead of requesting source over DAP.",
    )
    parser.add_argument(
        "--remote-root",
        action="append",
        default=[],
        metavar="PATH",
        help="Remote directory matched to --local-root. Must appear the "
        "same number of times as --local-root; pairs are taken in CLI "
        "order via zip().",
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
        "-k",
        "--breakpoint",
        action="append",
        default=[],
        metavar="FILE:LINE|LINE",
        help="Set a breakpoint at FILE:LINE, or just LINE for the program "
        "being debugged (may be repeated). Implies --no-stop-on-entry "
        "so the program runs to the first breakpoint instead of pausing "
        "at line 1.",
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
    parser.add_argument(
        "-d",
        "--doc",
        action="store_true",
        help="Display README.md in a markdown viewer and exit (no program needed)",
    )
    parser.add_argument(
        "--doc-text",
        action="store_true",
        help="Print README.md to stdout as wrapped plain text and exit "
        "(useful for `tdb --doc-text | less`, piping to a file, etc.)",
    )
    return parser


# --- Post-processing helpers ------------------------------------------------
# Each helper takes the parsed Namespace + parser (for parser.error) and
# either decorates the Namespace in place or exits via parser.error.
# Tests can exercise each one in isolation by constructing a Namespace
# directly.


def _apply_flag_implications(args: argparse.Namespace) -> None:
    """Fill in derived flags from primary ones.

    - `stop_on_entry` is derived from `--no-stop-on-entry`.
    - `-k` implies `--no-stop-on-entry`: a CLI breakpoint means "run
       to here", not "pause at line 1".
    - `--headless` implies `--server` (headless IS the server).
    """
    args.stop_on_entry = not args.no_stop_on_entry
    if args.breakpoint:
        args.stop_on_entry = False
    if args.headless:
        args.server = True


def _validate_terminal_choice(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> None:
    """Fail fast if `--terminal X` refers to an executable not on PATH.

    The terminal-choice names double as executable names; see
    `_TERMINAL_SPECS` in `tdb/session/terminal.py`.
    """
    if args.terminal and not shutil.which(args.terminal):
        parser.error(
            f"--terminal {args.terminal!r}: executable not found on PATH. "
            f"Install {args.terminal} or pick a different --terminal."
        )


def _parse_attach_spec(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> None:
    """Split `--remote-attach [HOST:]PORT` into `attach_host` / `attach_port`."""
    args.attach_host = None
    args.attach_port = None
    if not args.remote_attach:
        return
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


def _parse_path_mappings(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> None:
    """Pair --local-root with --remote-root via zip(), validate counts/usage.

    Stores `args.path_mappings` as `list[tuple[local, remote]]` (or empty).
    --remote-attach is required when either flag is used; counts must match.
    Local roots must be existing directories; remote roots are normalized
    (trailing-separator stripped) but otherwise opaque since they live on
    the debuggee's machine.
    """
    args.path_mappings = []
    locals_ = args.local_root or []
    remotes = args.remote_root or []
    if not locals_ and not remotes:
        return
    if not args.remote_attach:
        parser.error("--local-root / --remote-root require --remote-attach")
    if len(locals_) != len(remotes):
        parser.error(
            f"--local-root and --remote-root must be supplied in equal "
            f"numbers (got {len(locals_)} local, {len(remotes)} remote); "
            f"each pair is matched in CLI order"
        )
    pairs: list[tuple[str, str]] = []
    for local, remote in zip(locals_, remotes):
        local_path = Path(local).resolve()
        if not local_path.is_dir():
            parser.error(f"--local-root not a directory: {local}")
        # Normalize remote: forward slashes, no trailing separator. Modern
        # Windows accepts forward slashes so we don't need posixpath vs
        # ntpath gymnastics — one normalized form covers both.
        remote_norm = remote.replace("\\", "/").strip()
        if not remote_norm:
            parser.error(f"--remote-root cannot be empty: {remote}")
        if remote_norm != "/":
            had_trailing = remote_norm.endswith("/")
            remote_norm = remote_norm.rstrip("/")
            # Preserve Windows drive root (e.g. "C:/") — stripping the slash
            # would change semantics to drive-relative "C:".
            if (
                had_trailing
                and len(remote_norm) == 2
                and remote_norm[0].isalpha()
                and remote_norm[1] == ":"
            ):
                remote_norm += "/"
            # All-slashes input (e.g. "///") rstrips to empty → treat as "/".
            if not remote_norm:
                remote_norm = "/"
        pairs.append((str(local_path), remote_norm))
    args.path_mappings = pairs


def _resolve_program_path(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> None:
    """Resolve `program` to an absolute path + verify it exists.

    Requires either `program` or `--remote-attach`; either is fine but
    not neither. In remote-attach mode no local file is required.
    """
    if args.remote_attach is None and args.program is None:
        parser.error("either a program or --remote-attach is required")

    if args.program and not args.remote_attach:
        program_path = Path(args.program).resolve()
        if not program_path.exists():
            parser.error(f"File not found: {args.program}")
        args.program = str(program_path)


def _resolve_breakpoint_file(file_part: str, local_roots: list[str]) -> Path | None:
    """Find a `-k FILE:LINE` file on disk.

    Absolute or cwd-relative paths resolve directly. If those don't
    exist and `--local-root` directories were given, search each one
    in CLI order; first match wins. Returns None if nothing matches —
    caller turns that into a parser.error with the search path.
    """
    direct = Path(file_part).resolve()
    if direct.is_file():
        return direct
    if Path(file_part).is_absolute():
        return None
    for root in local_roots:
        candidate = Path(root) / file_part
        if candidate.is_file():
            return candidate.resolve()
    return None


def _parse_breakpoints(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> None:
    """Parse `-k FILE:LINE | LINE` specs into `[(abs_path, line), ...]`.

    A bare LINE targets `args.program` (rejected for remote-attach
    because there's no local program to anchor on). After parsing, the
    list still needs `_snap_breakpoints` to align lines to logical
    statement starts.
    """
    parsed_bps: list[tuple[str, int]] = []
    local_roots = [local for local, _ in args.path_mappings]
    for spec in args.breakpoint:
        if ":" in spec:
            file_part, line_part = spec.rsplit(":", 1)
            try:
                line = int(line_part)
            except ValueError:
                parser.error(f"Invalid line number in breakpoint: {spec}")
            bp_path = _resolve_breakpoint_file(file_part, local_roots)
            if bp_path is None:
                if local_roots:
                    roots_str = ", ".join(local_roots)
                    parser.error(
                        f"Breakpoint file not found: {file_part} "
                        f"(searched: cwd, {roots_str})"
                    )
                else:
                    parser.error(f"Breakpoint file not found: {file_part}")
            parsed_bps.append((str(bp_path), line))
        else:
            try:
                line = int(spec)
            except ValueError:
                parser.error(f"Invalid breakpoint (expected FILE:LINE or LINE): {spec}")
            if not args.program:
                parser.error(
                    f"Bare-line breakpoint -k {spec} requires a program "
                    "(not allowed with --remote-attach)"
                )
            parsed_bps.append((args.program, line))
    args.breakpoint = parsed_bps


def _snap_breakpoints(args: argparse.Namespace) -> None:
    """Snap each (path, line) BP to the nearest logical statement start.

    If a line doesn't map to any statement (e.g. line is before the
    first statement in the file), the BP is dropped with a warning to
    stderr — keeping an unhittable breakpoint would confuse the user.
    """
    from tdb.source_analysis import snap_breakpoint

    snapped_bps: list[tuple[str, int]] = []
    for bp_path, line in args.breakpoint:
        snapped = snap_breakpoint(bp_path, line)
        if snapped is None:
            print(
                f"warning: -k {bp_path}:{line} has no logical statement "
                f"at or before that line; dropping breakpoint",
                file=sys.stderr,
            )
            continue
        if snapped != line:
            print(
                f"warning: -k {bp_path}:{line} is not the start of a "
                f"logical statement; moved to line {snapped}",
                file=sys.stderr,
            )
        snapped_bps.append((bp_path, snapped))
    args.breakpoint = snapped_bps


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Public entry: build parser, parse argv, run post-processing.

    Short-circuit modes (`--doc`, `--doc-text`, `--post-mortem`) skip
    the launch-related validation since they don't run a debuggee.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    _apply_flag_implications(args)

    if args.doc or args.doc_text or args.post_mortem:
        return args

    _validate_terminal_choice(args, parser)
    _parse_attach_spec(args, parser)
    _parse_path_mappings(args, parser)
    _resolve_program_path(args, parser)
    _parse_breakpoints(args, parser)
    _snap_breakpoints(args)
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
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            filename=str(log_dir / "tdb.log"),
            level=logging.DEBUG,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )
    except OSError:
        # Read-only filesystem (or perm denied). Run without on-disk
        # logging; route everything to a NullHandler so stray log calls
        # don't fall through to lastResort and corrupt the TUI on stderr.
        root = logging.getLogger()
        root.addHandler(logging.NullHandler())
        root.setLevel(logging.CRITICAL + 1)

    if args.doc:
        _run_doc()
    elif args.doc_text:
        _run_doc_text()
    elif args.post_mortem:
        _run_post_mortem(args)
    elif args.headless:
        _run_headless(args)
    else:
        _run_tui(args)


def _run_doc() -> None:
    """Display the bundled README.md in a Textual MarkdownViewer."""
    from tdb.app_helpers import find_readme

    readme = find_readme()
    if readme is None:
        print("README.md not found in the installation.", file=sys.stderr)
        sys.exit(1)

    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.widgets import Footer, MarkdownViewer

    class _DocApp(App):
        TITLE = "tdb documentation"
        BINDINGS = [
            Binding("escape", "quit", "Quit", show=False),
            Binding("q", "quit", "Quit"),
        ]

        def compose(self) -> ComposeResult:
            yield MarkdownViewer(readme, show_table_of_contents=True)
            yield Footer()

    _DocApp().run()


def _run_doc_text() -> None:
    """Render the bundled README.md to stdout as wrapped plain text.

    Uses Rich's Markdown renderer so headings, lists, code blocks, and
    tables come out nicely formatted (boxed tables, indented code, etc.)
    and word-wrapped to the terminal width — pipe-friendly for `less`
    or redirection to a file.
    """
    import shutil
    from tdb.app_helpers import find_readme

    readme = find_readme()
    if readme is None:
        print("README.md not found in the installation.", file=sys.stderr)
        sys.exit(1)

    from rich.console import Console
    from rich.markdown import Markdown

    # When stdout is a tty, wrap to its width (capped so very wide
    # terminals don't end up with ragged-looking long paragraphs); when
    # piped to a file or pager, use a fixed reasonable width.
    if sys.stdout.isatty():
        cols = shutil.get_terminal_size((100, 24)).columns
        width = min(cols, 100)
    else:
        width = 100

    Console(width=width).print(Markdown(readme))


def _run_post_mortem(args: argparse.Namespace) -> None:
    """Load a snapshot written by tdb.exception_hook and display it."""
    import json
    from tdb.app import TdbApp
    from tdb.persist import load_config

    with open(args.post_mortem) as f:
        snapshot = json.load(f)

    config = load_config()
    if args.keybindings:
        config.keybindings = args.keybindings

    app = TdbApp(
        program="",
        config=config,
        post_mortem_snapshot=snapshot,
    )
    app.run()


def _run_headless(args: argparse.Namespace) -> None:
    """Run in headless mode: JSON-RPC server only, no TUI."""
    import asyncio
    from tdb.server.runner import run_headless

    asyncio.run(
        run_headless(
            program=args.program,
            args=args.args,
            cwd=args.cwd,
            stop_on_entry=args.stop_on_entry,
            just_my_code=not args.no_just_my_code,
            python=args.python,
            port=args.server_port,
            cli_breakpoints=args.breakpoint,
            attach_host=args.attach_host,
            attach_port=args.attach_port,
            path_mappings=args.path_mappings or None,
        )
    )


def _run_tui(args: argparse.Namespace) -> None:
    """Run with the TUI (optionally with the server alongside)."""
    from tdb.app import TdbApp
    from tdb.persist import load_config, save_config

    config = load_config()
    # --keybindings overrides saved value and writes it back so the next
    # run picks up the explicit choice without re-specifying the flag.
    if args.keybindings is not None:
        config.keybindings = args.keybindings
        save_config(config)

    app = TdbApp(
        program=args.program or "",
        args=args.args,
        cwd=args.cwd,
        stop_on_entry=args.stop_on_entry,
        just_my_code=not args.no_just_my_code,
        python=args.python,
        terminal=args.terminal,
        config=config,
        cli_breakpoints=args.breakpoint,
        attach_host=args.attach_host,
        attach_port=args.attach_port,
        path_mappings=args.path_mappings,
        sub_process=not args.no_subprocess,
        server_port=args.server_port if args.server else None,
    )
    app.run()
    # Fatal startup error (e.g. remote-attach connection refused). The
    # TUI has already torn down; surface the reason on stderr so the
    # user doesn't just see a blank terminal and a non-zero exit code.
    if app._startup_error:
        print(app._startup_error, file=sys.stderr)
        sys.exit(app.return_code or 2)

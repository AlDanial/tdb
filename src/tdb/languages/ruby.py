"""The Ruby language profile (the bundled rdbg DAP bridge)."""

from __future__ import annotations

import sys
from typing import Any

from tdb.dap.types import Capabilities
from tdb.languages.base import (
    AdapterQuirks,
    AdapterSpec,
    LanguageNotSupportedError,
    LanguageProfile,
    Presentation,
    ProfileCapabilities,
)
from tdb.languages.errors import parse_ruby_error


class RdbgAdapter(AdapterSpec):
    """Bundled bridge for the DAP endpoint exposed by ``debug``/``rdbg``.

    ``vscode-rdbg`` is a VS Code extension, not a standalone executable.
    tdb instead starts its own stdio-to-TCP bridge, which launches ``rdbg
    --open`` and relays its native DAP connection.
    """

    id = "rdbg"

    # rdbg suspends the debuggee when an `evaluate`d expression raises
    # (see AdapterQuirks.suppress_exception_breakpoints_during_evaluate);
    # the controller must clear the catch breakpoint around evaluate or a
    # typo'd inspect deadlocks the session.
    quirks = AdapterQuirks(suppress_exception_breakpoints_during_evaluate=True)

    def __init__(self, rdbg_executable: str | None = None) -> None:
        """
        Args:
            rdbg_executable: ``rdbg`` executable path.  When omitted, the
                bundled bridge resolves it from ``PATH``.
        """
        self._rdbg = rdbg_executable

    def command(self) -> list[str]:
        """Start tdb's bridge; it validates ``rdbg`` when launching."""
        command = [sys.executable, "-m", "tdb.adapters.ruby"]
        if self._rdbg:
            command.extend(["--rdbg", self._rdbg])
        return command

    def launch_body(
        self,
        *,
        program: str,
        args: list[str],
        cwd: str,
        env: dict[str, str] | None,
        stop_on_entry: bool,
        console: str,
        opts: dict[str, Any],
    ) -> dict[str, Any]:
        """Build the launch request body.

        Return the Ruby program launch configuration in DAP format.
        """
        body: dict[str, Any] = {
            "type": "rdbg",
            "request": "launch",
            "program": program,
            "args": args,
            "cwd": cwd,
            "stopOnEntry": stop_on_entry,
            "console": console,
        }

        if env:
            body["env"] = env

        # Ruby-specific options
        if opts.get("show_protocol_messages"):
            # Show DAP protocol messages for debugging
            body["showProtocolMessages"] = True

        if opts.get("use_bundler"):
            # Run through Bundler
            body["useBundler"] = True

        # Port for the debug listener
        if opts.get("debug_port"):
            body["debugPort"] = opts["debug_port"]

        return body

    def attach_body(
        self, *, host: str, port: int, opts: dict[str, Any]
    ) -> dict[str, Any]:
        """Build the attach request body.

        Return the configuration for connecting to a running Ruby process.
        """
        body: dict[str, Any] = {
            "type": "rdbg",
            "request": "attach",
            "host": host,
            "port": port,
        }

        if opts.get("path_mappings"):
            body["pathMappings"] = [
                {"localRoot": local, "remoteRoot": remote}
                for local, remote in opts["path_mappings"]
            ]

        return body

    def pick_exception_filters(self, caps: Capabilities) -> list[str]:
        """Select exception filters.

        Prefer the generic ``any`` filter when exposed by ``debug`` and fall
        back to the adapter defaults otherwise.
        """
        filters = []

        # Check available filters
        if caps.exception_breakpoint_filters:
            filter_names = [f.get("filter") for f in caps.exception_breakpoint_filters]

            # debug.gem exposes ``any`` and exception-class filters (for
            # example ``RuntimeError``), rather than debugpy's names.
            if "any" in filter_names:
                filters.append("any")

        # Fall back to the adapter defaults if no filters are found
        if not filters and caps.exception_breakpoint_filters:
            filters = [
                f["filter"]
                for f in caps.exception_breakpoint_filters
                if f.get("default")
            ]

        return filters


def build_ruby_profile(
    adapter: str | None = None, adapter_paths: dict[str, str] | None = None
) -> LanguageProfile:
    """Build the Ruby language profile.

    Args:
    adapter: Adapter name ("rdbg" only).
    adapter_paths: Mapping of adapter names to paths.
        Example: {"rdbg": "/path/to/rdbg"}

    Returns:
    A Ruby LanguageProfile instance.

    Raises:
        LanguageNotSupportedError: If an unknown adapter is specified.
    """
    if adapter not in (None, "rdbg"):
        raise LanguageNotSupportedError(
            f"unknown adapter {adapter!r} for ruby (known: rdbg)"
        )

    return LanguageProfile(
        id="ruby",
        display_name="Ruby",
        adapter=RdbgAdapter(rdbg_executable=(adapter_paths or {}).get("rdbg")),
        presentation=Presentation(
            lexer="ruby",  # Pygments lexer
            parse_error=parse_ruby_error,
            frame_placeholder="<main>",
        ),
        capabilities=ProfileCapabilities(
            compute_step_units=None,  # Statement stepping is not supported
            child_process_strategy=None,  # Child process tracking is not supported
            task_inspection=False,  # Asyncio task inspection is not supported
            pause_while_running=True,  # Pausing while running is supported
        ),
    )


RUBY_PROFILE = build_ruby_profile()

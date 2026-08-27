"""The Rust language profile.

Rust uses the native GDB/LLDB DAP adapters (launch, native remote
attach, and path mapping all live in the C/C++ base adapters). The
Rust subclasses add only concurrency-probe script injection.
"""

from __future__ import annotations

import sys
from importlib import resources
from typing import Any

from tdb.languages.base import (
    AdapterSpec,
    LanguageNotSupportedError,
    LanguageProfile,
    Presentation,
    ProfileCapabilities,
)
from tdb.languages.cpp import GdbDapAdapter, LldbDapAdapter, quote_debugger_arg
from tdb.languages.errors import parse_rust_error


def _gdb_source_filename(value: str) -> str:
    """Validate one filename for GDB's ``source`` command parser.

    ``source`` takes the rest of the line as a literal filename (tilde
    expansion only — no backslash unescaping, no quote stripping), so the
    path must be passed raw: escaping would corrupt paths containing
    spaces or Windows backslashes.
    """
    if "\n" in value or "\r" in value:
        raise LanguageNotSupportedError("GDB probe path contains a newline")
    return value


def _with_rust_backtrace(env: dict[str, str] | None) -> dict[str, str]:
    """Merge RUST_BACKTRACE=1 into the debuggee env (panic backtraces for
    the error modal) without clobbering a user-provided value."""
    merged = dict(env or {})
    merged.setdefault("RUST_BACKTRACE", "1")
    return merged


class RustGdbAdapter(GdbDapAdapter):
    def command(self) -> list[str]:
        command = super().command()
        script_path = resources.files("tdb.rust_concurrency.probes").joinpath(
            "gdb_script.py"
        )
        # `set width unlimited` keeps the probe's single-line JSON envelope
        # from being wrapped by gdb's filtered output stream. Splice the
        # probe options after the executable so any argv the base adapter
        # adds (today `-i dap`) is preserved.
        return (
            command[:1]
            + [
                "-iex",
                "set width unlimited",
                "-iex",
                f"source {_gdb_source_filename(str(script_path))}",
            ]
            + command[1:]
        )

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
        return super().launch_body(
            program=program,
            args=args,
            cwd=cwd,
            env=_with_rust_backtrace(env),
            stop_on_entry=stop_on_entry,
            console=console,
            opts=opts,
        )


class RustLldbAdapter(LldbDapAdapter):
    @staticmethod
    def _probe_init_commands() -> list[str]:
        script_path = resources.files("tdb.rust_concurrency.probes").joinpath(
            "lldb_script.py"
        )
        return [f"command script import {quote_debugger_arg(str(script_path))}"]

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
        body = super().launch_body(
            program=program,
            args=args,
            cwd=cwd,
            env=_with_rust_backtrace(env),
            stop_on_entry=stop_on_entry,
            console=console,
            opts=opts,
        )
        body["initCommands"] = (
            list(body.get("initCommands", ())) + self._probe_init_commands()
        )
        return body

    def attach_body(
        self, *, host: str, port: int, opts: dict[str, Any]
    ) -> dict[str, Any]:
        body = super().attach_body(host=host, port=port, opts=opts)
        body["initCommands"] = (
            list(opts.get("initCommands", ())) + self._probe_init_commands()
        )
        return body


def build_rust_profile(
    adapter: str | None = None,
    adapter_paths: dict[str, str] | None = None,
    program: str | None = None,
) -> LanguageProfile:
    default = "lldb-dap" if sys.platform == "darwin" else "gdb"
    adapter_id = adapter or default
    adapters: dict[str, type[AdapterSpec]] = {
        "gdb": RustGdbAdapter,
        "lldb-dap": RustLldbAdapter,
    }
    if adapter_id not in adapters:
        raise LanguageNotSupportedError(
            f"unknown adapter {adapter_id!r} for rust "
            f"(known: {', '.join(sorted(adapters))})"
        )
    executable = (adapter_paths or {}).get(adapter_id)
    return LanguageProfile(
        id="rust",
        display_name="Rust",
        adapter=adapters[adapter_id](executable=executable),
        presentation=Presentation(lexer="rust", parse_error=parse_rust_error),
        capabilities=ProfileCapabilities(
            pause_while_running=True,
            concurrency_inspection="rust",
        ),
    )

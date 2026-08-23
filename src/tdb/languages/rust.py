"""The Rust language profile.

Rust uses the native GDB/LLDB DAP adapters for local launch. Small
Rust-specific subclasses leave room for future Rust attach and probe
configuration without changing the C/C++ profile.
"""

from __future__ import annotations

import sys
from importlib import resources
from typing import Any

from tdb.languages.base import (
    AdapterQuirks,
    AdapterSpec,
    LanguageNotSupportedError,
    LanguageProfile,
    Presentation,
    ProfileCapabilities,
)
from tdb.languages.cpp import GdbDapAdapter, LldbDapAdapter


def _required_program(opts: dict[str, Any]) -> str:
    program = opts.get("program")
    if not isinstance(program, str) or not program:
        raise LanguageNotSupportedError(
            "Rust remote attach requires a local program with debug symbols"
        )
    return program


def _gdb_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class RustGdbAdapter(GdbDapAdapter):
    quirks = AdapterQuirks(attach_via_adapter=True)

    def command(self) -> list[str]:
        command = super().command()
        script_path = resources.files("tdb.rust_concurrency.probes").joinpath(
            "gdb_script.py"
        )
        return [
            command[0],
            "-iex",
            f"source {_gdb_string(str(script_path))}",
            "-i",
            "dap",
        ]

    def attach_body(
        self, *, host: str, port: int, opts: dict[str, Any]
    ) -> dict[str, Any]:
        return {"program": _required_program(opts), "target": f"{host}:{port}"}

    def pre_configuration_commands(
        self, path_mappings: list[tuple[str, str]]
    ) -> tuple[str, ...]:
        return tuple(
            f"set substitute-path {_gdb_string(remote)} {_gdb_string(local)}"
            for local, remote in path_mappings
        )


class RustLldbAdapter(LldbDapAdapter):
    quirks = AdapterQuirks(attach_via_adapter=True)

    def attach_body(
        self, *, host: str, port: int, opts: dict[str, Any]
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "program": _required_program(opts),
            "gdb-remote-host": host,
            "gdb-remote-port": port,
        }
        mappings = opts.get("path_mappings") or []
        if mappings:
            body["sourceMap"] = [[remote, local] for local, remote in mappings]
        return body


def build_rust_profile(
    adapter: str | None = None, adapter_paths: dict[str, str] | None = None
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
        presentation=Presentation(lexer="rust"),
        capabilities=ProfileCapabilities(
            pause_while_running=True,
            concurrency_inspection="rust",
        ),
    )

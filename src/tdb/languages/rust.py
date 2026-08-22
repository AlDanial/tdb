"""The Rust language profile.

Rust uses the native GDB/LLDB DAP adapters for local launch. Small
Rust-specific subclasses leave room for future Rust attach and probe
configuration without changing the C/C++ profile.
"""

from __future__ import annotations

import sys

from tdb.languages.base import (
    AdapterSpec,
    LanguageNotSupportedError,
    LanguageProfile,
    Presentation,
    ProfileCapabilities,
)
from tdb.languages.cpp import GdbDapAdapter, LldbDapAdapter


class RustGdbAdapter(GdbDapAdapter):
    pass


class RustLldbAdapter(LldbDapAdapter):
    pass


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

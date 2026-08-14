"""The C/C++ language profile.

Default adapter: `gdb -i dap` (GDB >= 14) via GdbDapAdapter.
Alternate: lldb-dap (ships with LLVM >= 17; debugs GCC- and
clang-built binaries alike — DWARF is compiler-neutral), selected
via `--adapter lldb-dap`.

Core-DAP capabilities only: no statement stepping (no C++ source
model), no task inspection, no child-process tracking.
"""

from __future__ import annotations

import shutil
from typing import Any

from tdb.languages.base import (
    AdapterNotFoundError,
    AdapterSpec,
    LanguageNotSupportedError,
    LanguageProfile,
    Presentation,
    ProfileCapabilities,
)


class LldbDapAdapter(AdapterSpec):
    id = "lldb-dap"

    def __init__(self, executable: str | None = None) -> None:
        self._executable = executable

    def command(self) -> list[str]:
        exe = self._executable or shutil.which("lldb-dap")
        if exe is None:
            raise AdapterNotFoundError(
                "lldb-dap not found on PATH — install LLVM >= 17 "
                '(package `lldb`), or set {"adapters": {"lldb-dap": '
                '"/path/to/lldb-dap"}} in tdb\'s config.json'
            )
        return [exe]

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
        body: dict[str, Any] = {
            "type": "lldb-dap",
            "request": "launch",
            "program": program,
            "args": args,
            "cwd": cwd,
            "stopOnEntry": stop_on_entry,
        }
        if env:
            # lldb-dap wants ["KEY=VALUE", ...], not a mapping.
            body["env"] = [f"{k}={v}" for k, v in env.items()]
        if console == "externalTerminal":
            body["runInTerminal"] = True
        return body

    def attach_body(
        self, *, host: str, port: int, opts: dict[str, Any]
    ) -> dict[str, Any]:
        raise LanguageNotSupportedError(
            "remote attach is not supported for lldb-dap yet"
        )


class GdbDapAdapter(AdapterSpec):
    """GDB's built-in DAP interpreter (`gdb -i dap`, GDB >= 14).

    Default C++ adapter: GDB's libstdc++ pretty-printers are more
    complete than LLDB's, which matters for heavily GCC codebases.
    """

    id = "gdb"

    def __init__(self, executable: str | None = None) -> None:
        self._executable = executable

    def command(self) -> list[str]:
        exe = self._executable or shutil.which("gdb")
        if exe is None:
            raise AdapterNotFoundError(
                "gdb not found on PATH — install GDB >= 14 (its DAP mode), "
                'or set {"adapters": {"gdb": "/path/to/gdb"}} in '
                "tdb's config.json"
            )
        return [exe, "-i", "dap"]

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
        if console == "externalTerminal":
            raise LanguageNotSupportedError(
                "--terminal is not supported with the gdb adapter (gdb's "
                "DAP mode has no terminal integration) — use "
                "`--adapter lldb-dap`"
            )
        body: dict[str, Any] = {
            "type": "gdb",
            "request": "launch",
            "program": program,
            "args": args,
            "cwd": cwd,
            # GDB's DAP name for stop-on-entry.
            "stopAtBeginningOfMainSubprogram": stop_on_entry,
        }
        if env:
            body["env"] = env  # GDB takes a mapping, unlike lldb-dap
        return body

    def attach_body(
        self, *, host: str, port: int, opts: dict[str, Any]
    ) -> dict[str, Any]:
        raise LanguageNotSupportedError(
            "remote attach is not supported for gdb -i dap yet"
        )


def build_cpp_profile(
    adapter: str | None = None, adapter_paths: dict[str, str] | None = None
) -> LanguageProfile:
    adapters: dict[str, type[AdapterSpec]] = {
        "lldb-dap": LldbDapAdapter,
        "gdb": GdbDapAdapter,
    }
    adapter_id = adapter or "gdb"
    if adapter_id not in adapters:
        raise LanguageNotSupportedError(
            f"unknown adapter {adapter_id!r} for cpp "
            f"(known: {', '.join(sorted(adapters))}; note: codelldb is "
            f"not packaged standalone — use lldb-dap)"
        )
    executable = (adapter_paths or {}).get(adapter_id)
    return LanguageProfile(
        id="cpp",
        display_name="C/C++",
        adapter=adapters[adapter_id](executable=executable),
        presentation=Presentation(lexer="cpp"),
        capabilities=ProfileCapabilities(),
    )

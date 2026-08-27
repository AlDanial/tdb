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
    AdapterQuirks,
    AdapterSpec,
    LanguageNotSupportedError,
    LanguageProfile,
    Presentation,
    ProfileCapabilities,
)


def _required_program(opts: dict[str, Any]) -> str:
    """Native remote attach drives gdbserver/lldb-server through a local
    adapter, which needs the local symbol-bearing copy of the remote
    executable."""
    program = opts.get("program")
    if not isinstance(program, str) or not program:
        raise LanguageNotSupportedError(
            "native remote attach requires a local program with debug symbols"
        )
    return program


def quote_debugger_arg(value: str) -> str:
    """Quote one argument for a gdb or lldb CLI command. Both parse
    double-quoted arguments with backslash escapes (gdb via buildargv,
    e.g. `set substitute-path`; lldb e.g. `command script import`).
    Shared by the rust and ocaml profiles — keep semantics in sync with
    both debuggers when changing."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class LldbDapAdapter(AdapterSpec):
    id = "lldb-dap"
    quirks = AdapterQuirks(attach_via_adapter=True, attach_requires_local_program=True)

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
        body: dict[str, Any] = {
            "program": _required_program(opts),
            "gdb-remote-host": host,
            "gdb-remote-port": port,
        }
        mappings = opts.get("path_mappings") or []
        if mappings:
            body["sourceMap"] = [[remote, local] for local, remote in mappings]
        return body


class GdbDapAdapter(AdapterSpec):
    """GDB's built-in DAP interpreter (`gdb -i dap`, GDB >= 14).

    Default C++ adapter: GDB's libstdc++ pretty-printers are more
    complete than LLDB's, which matters for heavily GCC codebases.
    """

    id = "gdb"
    quirks = AdapterQuirks(
        attach_via_adapter=True,
        attach_requires_local_program=True,
        resume_after_remote_attach=True,
    )

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
        # gdb-dap passes "target" to `target remote`.
        return {"program": _required_program(opts), "target": f"{host}:{port}"}

    def pre_configuration_commands(
        self, path_mappings: list[tuple[str, str]]
    ) -> tuple[str, ...]:
        return tuple(
            f"set substitute-path {quote_debugger_arg(remote)} {quote_debugger_arg(local)}"
            for local, remote in path_mappings
        )


def build_cpp_profile(
    adapter: str | None = None,
    adapter_paths: dict[str, str] | None = None,
    program: str | None = None,
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
        # Verified (Task 9, tests/integration/test_cpp_pause.py): DAP
        # `pause` reliably stops a never-stopped, actively-looping
        # debuggee on both gdb -i dap and lldb-dap.
        capabilities=ProfileCapabilities(pause_while_running=True),
    )

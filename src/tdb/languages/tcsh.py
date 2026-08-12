"""The tcsh language profile.

The adapter is tdb's own bundled module (python -m tdb.adapters.tcsh),
ported from the standalone tcsh-dap project. It debugs .csh/.tcsh
scripts with a stock tcsh by instrumenting temporary copies of the
sources and coordinating stops through private POSIX FIFOs.

Config twist (same as perl/bash): {"adapters": {"tcsh": "/path/tcsh"}}
names the tcsh interpreter to spawn, not the adapter binary.

The adapter code requires Python 3.11+ (asyncio.timeout) and Unix
FIFOs; command() gates both with an actionable error instead of
letting the subprocess die with a traceback.
"""

from __future__ import annotations

import shutil
import sys
from typing import Any

from tdb.languages.base import (
    AdapterNotFoundError,
    AdapterSpec,
    LanguageNotSupportedError,
    LanguageProfile,
    Presentation,
    ProfileCapabilities,
)


class TcshAdapter(AdapterSpec):
    id = "tcsh-tdb"

    def __init__(self, tcsh_executable: str | None = None) -> None:
        self._tcsh = tcsh_executable

    def command(self) -> list[str]:
        if sys.platform == "win32":
            raise AdapterNotFoundError(
                "tcsh debugging is not supported on native Windows — run tdb under WSL"
            )
        if sys.version_info < (3, 11):
            raise AdapterNotFoundError(
                "the tcsh adapter requires Python 3.11+ — upgrade the Python running tdb"
            )
        if self._tcsh is None and shutil.which("tcsh") is None:
            raise AdapterNotFoundError(
                "tcsh not found on PATH — install tcsh, or set "
                '{"adapters": {"tcsh": "/path/to/tcsh"}} in tdb\'s config'
            )
        return [sys.executable, "-m", "tdb.adapters.tcsh"]

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
            "type": "tcsh",
            "request": "launch",
            "program": program,
            "args": args,
            "cwd": cwd,
            "stopOnEntry": stop_on_entry,
        }
        if env:
            body["env"] = env
        if self._tcsh:
            body["tcshPath"] = self._tcsh
        return body

    def attach_body(
        self, *, host: str, port: int, opts: dict[str, Any]
    ) -> dict[str, Any]:
        raise LanguageNotSupportedError("tcsh does not support remote attach")

    def pick_exception_filters(self, caps) -> list[str]:
        return []


def build_tcsh_profile(
    adapter: str | None = None, adapter_paths: dict[str, str] | None = None
) -> LanguageProfile:
    if adapter not in (None, "tcsh-tdb"):
        raise LanguageNotSupportedError(
            f"unknown adapter {adapter!r} for tcsh (known: tcsh-tdb)"
        )
    return LanguageProfile(
        id="tcsh",
        display_name="Tcsh",
        adapter=TcshAdapter(tcsh_executable=(adapter_paths or {}).get("tcsh")),
        presentation=Presentation(lexer="tcsh", frame_placeholder="main"),
        capabilities=ProfileCapabilities(),
    )

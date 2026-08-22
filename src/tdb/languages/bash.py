"""The Bash language profile.

The adapter is tdb's own bundled module (python -m tdb.adapters.bash)
driving bash's DEBUG-trap machinery via a BASH_ENV-injected harness.
Config twist (same as perl): {"adapters": {"bash": "/path/bash"}} names
the bash interpreter to spawn, not the adapter binary.

Core-DAP capabilities only; no remote attach in v1 (attach_body is
never reachable: cli's remote-attach path is debugpy-specific and
--lang bash with -r is rejected upstream).
"""

from __future__ import annotations

import sys
from typing import Any

from tdb.languages.base import (
    AdapterSpec,
    LanguageNotSupportedError,
    LanguageProfile,
    Presentation,
    ProfileCapabilities,
)


class BashAdapter(AdapterSpec):
    id = "bash-tdb"

    def __init__(self, bash_executable: str | None = None) -> None:
        self._bash = bash_executable

    def command(self) -> list[str]:
        return [sys.executable, "-m", "tdb.adapters.bash"]

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
            "type": "bash",
            "request": "launch",
            "program": program,
            "args": args,
            "cwd": cwd,
            "stopOnEntry": stop_on_entry,
            "console": console,
        }
        if env:
            body["env"] = env
        if self._bash:
            body["bash"] = self._bash
        return body

    def attach_body(
        self, *, host: str, port: int, opts: dict[str, Any]
    ) -> dict[str, Any]:
        raise LanguageNotSupportedError("bash does not support remote attach")

    def pick_exception_filters(self, caps) -> list[str]:
        return []


def build_bash_profile(
    adapter: str | None = None,
    adapter_paths: dict[str, str] | None = None,
    program: str | None = None,
) -> LanguageProfile:
    if adapter not in (None, "bash-tdb"):
        raise LanguageNotSupportedError(
            f"unknown adapter {adapter!r} for bash (known: bash-tdb)"
        )
    return LanguageProfile(
        id="bash",
        display_name="Bash",
        adapter=BashAdapter(bash_executable=(adapter_paths or {}).get("bash")),
        presentation=Presentation(lexer="bash", frame_placeholder="main"),
        capabilities=ProfileCapabilities(pause_while_running=True),
    )

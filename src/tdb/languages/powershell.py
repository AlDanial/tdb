"""The PowerShell language profile.

The adapter is tdb's bundled proxy (python -m tdb.adapters.powershell)
in front of PowerShell Editor Services (PSES), the DAP server behind the
VS Code PowerShell extension. Config twist (same shape as perl/ruby):
{"adapters": {"pwsh": "/path/to/pwsh"}} names the interpreter and
{"adapters": {"pses": "/path/to/PowerShellEditorServices"}} names the
PSES module directory; neither selects an adapter binary. `command()`
nevertheless resolves both, so a missing pwsh/PSES raises
AdapterNotFoundError before the session starts (spec addendum 3.1) --
the proxy's own check at launch is only a backstop, and a launch error
from it is invisible in the TUI (tdb surfaces launch failures only after
the adapter's `initialized` event, which PSES never sends in that case).

Core-DAP capabilities plus --run. No --terminal, no attach in v1 (see
docs/superpowers/specs/2026-09-03-powershell-support-design.md).
"""

from __future__ import annotations

import sys
from typing import Any

from tdb.adapters.powershell.locate import find_pses, find_pwsh
from tdb.languages.base import (
    AdapterNotFoundError,
    AdapterQuirks,
    AdapterSpec,
    LanguageNotSupportedError,
    LanguageProfile,
    Presentation,
    ProfileCapabilities,
)
from tdb.languages.errors import parse_powershell_error


class PsesAdapter(AdapterSpec):
    id = "pses"
    quirks = AdapterQuirks()

    def __init__(
        self, pwsh_executable: str | None = None, pses_dir: str | None = None
    ) -> None:
        self._pwsh = pwsh_executable
        self._pses = pses_dir

    def command(self) -> list[str]:
        # The adapter itself is tdb's bundled proxy, which always exists --
        # but it is useless without pwsh and PSES, and a failure it reports
        # at launch time never reaches the TUI (see the module docstring).
        # Resolve both here so the CLI can print the hint and exit 2, the
        # same contract dlv/gdb get.
        try:
            find_pwsh(self._pwsh)
            find_pses(self._pses)
        except FileNotFoundError as e:
            raise AdapterNotFoundError(str(e)) from e
        return [sys.executable, "-m", "tdb.adapters.powershell"]

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
                "--terminal is not supported for PowerShell yet (PSES has "
                "no terminal integration in tdb's debug-only mode)"
            )
        body: dict[str, Any] = {
            "type": "powershell",
            "request": "launch",
            "program": program,
            "args": args,
            "cwd": cwd,
            "stopOnEntry": stop_on_entry,
            "console": console,
        }
        if env:
            body["env"] = env
        if self._pwsh:
            body["pwsh"] = self._pwsh
        if self._pses:
            body["pses"] = self._pses
        return body

    def attach_body(
        self, *, host: str, port: int, opts: dict[str, Any]
    ) -> dict[str, Any]:
        raise LanguageNotSupportedError("PowerShell does not support remote attach")

    def pick_exception_filters(self, caps) -> list[str]:
        return []


def build_powershell_profile(
    adapter: str | None = None,
    adapter_paths: dict[str, str] | None = None,
    program: str | None = None,
) -> LanguageProfile:
    if adapter not in (None, "pses"):
        raise LanguageNotSupportedError(
            f"unknown adapter {adapter!r} for powershell (known: pses)"
        )
    paths = adapter_paths or {}
    return LanguageProfile(
        id="powershell",
        display_name="PowerShell",
        adapter=PsesAdapter(
            pwsh_executable=paths.get("pwsh"), pses_dir=paths.get("pses")
        ),
        presentation=Presentation(
            lexer="powershell",
            parse_error=parse_powershell_error,
            frame_placeholder="<ScriptBlock>",
        ),
        capabilities=ProfileCapabilities(pause_while_running=True),
    )

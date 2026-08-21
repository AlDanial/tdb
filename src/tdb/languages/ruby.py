"""The Ruby language profile.

The adapter is tdb's bundled stdio<->socket DAP proxy
(python -m tdb.adapters.ruby) in front of the debug gem's `rdbg`,
which speaks DAP natively but only over a socket. AdapterNotFoundError
cannot happen for the adapter itself — a missing/too-old *rdbg* is
reported by the proxy at launch. Config twist (same shape as perl):
{"adapters": {"rdbg": "/path/to/rdbg"}} names the rdbg executable the
proxy should spawn.

Core-DAP capabilities only: no statement stepping, no task inspection,
no child-process tracking (fork support is a follow-on project).
Remote attach is DIRECT (attach_via_adapter=False): rdbg is a DAP
server, so tdb TCP-connects to it exactly like debugpy.
"""

from __future__ import annotations

import sys
from typing import Any

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
    id = "rdbg"
    quirks = AdapterQuirks()

    def __init__(self, rdbg_executable: str | None = None) -> None:
        self._rdbg = rdbg_executable

    def command(self) -> list[str]:
        return [sys.executable, "-m", "tdb.adapters.ruby"]

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
            "type": "ruby",
            "request": "launch",
            "program": program,
            "args": args,
            "cwd": cwd,
            "stopOnEntry": stop_on_entry,
            "console": console,
        }
        if env:
            body["env"] = env
        if self._rdbg:
            body["rdbg"] = self._rdbg
        return body

    def attach_body(
        self, *, host: str, port: int, opts: dict[str, Any]
    ) -> dict[str, Any]:
        # Direct TCP attach: the host/port were already used to open the
        # socket (dap/client.py connect); rdbg's attach request needs no
        # address. Path mapping would use rdbg's localfsMap argument,
        # whose format is unverified — refuse rather than misbehave.
        if opts.get("path_mappings"):
            raise LanguageNotSupportedError(
                "--local-root/--remote-root path mappings are not "
                "supported for ruby remote attach yet"
            )
        return {"type": "ruby", "request": "attach"}

    def pick_exception_filters(self, caps) -> list[str]:
        # rdbg's filters ("any", "RuntimeError") trigger on *rescued*
        # exceptions too — far too noisy as defaults.
        return []


def build_ruby_profile(
    adapter: str | None = None, adapter_paths: dict[str, str] | None = None
) -> LanguageProfile:
    if adapter not in (None, "rdbg"):
        raise LanguageNotSupportedError(
            f"unknown adapter {adapter!r} for ruby (known: rdbg)"
        )
    return LanguageProfile(
        id="ruby",
        display_name="Ruby",
        adapter=RdbgAdapter(rdbg_executable=(adapter_paths or {}).get("rdbg")),
        presentation=Presentation(
            lexer="ruby",
            parse_error=parse_ruby_error,
            frame_placeholder="<main>",
        ),
        capabilities=ProfileCapabilities(pause_while_running=True),
    )

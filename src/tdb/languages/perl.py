"""The Perl language profile.

The adapter is tdb's own bundled module (python -m tdb.adapters.perl)
driving stock perl5db, so AdapterNotFoundError cannot happen for the
adapter itself — a missing/old *perl interpreter* is reported by the
adapter at launch. Config twist: {"adapters": {"perl": "/path/perl"}}
names the perl interpreter to spawn, not the adapter binary.

Core-DAP capabilities only: no statement stepping, no task inspection,
no child-process tracking. Remote attach is adapter-mediated
(attach_via_adapter quirk): tdb spawns the adapter, the adapter dials
the Devel::TdbRemote listener inside the debuggee.
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
from tdb.languages.errors import parse_perl_error


class PerlAdapter(AdapterSpec):
    id = "perl-tdb"
    quirks = AdapterQuirks(attach_via_adapter=True)

    def __init__(self, perl_executable: str | None = None) -> None:
        self._perl = perl_executable

    def command(self) -> list[str]:
        return [sys.executable, "-m", "tdb.adapters.perl"]

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
            "type": "perl",
            "request": "launch",
            "program": program,
            "args": args,
            "cwd": cwd,
            "stopOnEntry": stop_on_entry,
        }
        if env:
            body["env"] = env
        if self._perl:
            body["perl"] = self._perl
        return body

    def attach_body(
        self, *, host: str, port: int, opts: dict[str, Any]
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": "perl",
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

    def pick_exception_filters(self, caps) -> list[str]:
        return []


def build_perl_profile(
    adapter: str | None = None, adapter_paths: dict[str, str] | None = None
) -> LanguageProfile:
    if adapter not in (None, "perl-tdb"):
        raise LanguageNotSupportedError(
            f"unknown adapter {adapter!r} for perl (known: perl-tdb)"
        )
    return LanguageProfile(
        id="perl",
        display_name="Perl",
        adapter=PerlAdapter(perl_executable=(adapter_paths or {}).get("perl")),
        presentation=Presentation(
            lexer="perl", parse_error=parse_perl_error, frame_placeholder="main"
        ),
        capabilities=ProfileCapabilities(),
    )

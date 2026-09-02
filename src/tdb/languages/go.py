"""The Go language profile (Delve DAP).

This module is built up across Tasks 2-4: detection helpers here,
DelveAdapter + build_go_profile in Task 3, error parsing and thread
classification in Task 4.
"""

from __future__ import annotations

import re
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

# Magic prefix of the build-info blob Go links into every binary
# (what `go version <binary>` locates). 16 bytes, version-stable.
_BUILDINFO_MAGIC = b"\xff Go buildinf:"
_CHUNK = 1024 * 1024  # scan in 1MB chunks
_SCAN_LIMIT = 16 * _CHUNK  # bounded: huge non-Go binaries stay cheap
_OVERLAP = len(_BUILDINFO_MAGIC) - 1


def is_go_binary(program: str) -> bool:
    """True when `program` is an executable with embedded Go buildinfo.

    Bounded scan of the first 16MB (the blob sits in an early data
    section in practice). Best-effort: unreadable files and misses
    beyond the limit return False — `--lang go` overrides (README).
    """
    scanned = 0
    tail = b""
    try:
        with open(program, "rb") as f:
            while scanned < _SCAN_LIMIT:
                chunk = f.read(_CHUNK)
                if not chunk:
                    break
                if _BUILDINFO_MAGIC in tail + chunk[:_OVERLAP] or (
                    _BUILDINFO_MAGIC in chunk
                ):
                    return True
                tail = chunk[-_OVERLAP:]
                scanned += len(chunk)
    except OSError:
        pass
    return False


class DelveAdapter(AdapterSpec):
    """Delve's native DAP server (`dlv dap`).

    Socket-only: dlv prints its listen line and serves one TCP
    connection (spawn_tcp mode, Task 1). One instance covers all four
    Delve modes — launch mode is constructor data or inferred from the
    program; a local-attach pid flips the attach quirk so the
    controller spawns dlv for attach too.
    """

    id = "dlv"
    connect_mode = "spawn_tcp"
    listen_regex = re.compile(r"DAP server listening at: (\S+):(\d+)")

    def __init__(
        self,
        executable: str | None = None,
        mode: str | None = None,
        attach_pid: int | None = None,
    ) -> None:
        self._executable = executable
        self._mode = mode  # "debug" | "exec" | "test" | None -> infer
        self._attach_pid = attach_pid
        if attach_pid is not None:
            # Local pid attach spawns dlv locally (like launch) and
            # sends the attach request through it.
            self.quirks = AdapterQuirks(attach_via_adapter=True)

    def command(self) -> list[str]:
        exe = self._executable or shutil.which("dlv")
        if exe is None:
            raise AdapterNotFoundError(
                "dlv (Delve) not found on PATH — "
                "`go install github.com/go-delve/delve/cmd/dlv@latest`, "
                'or set {"adapters": {"dlv": "/path/to/dlv"}} in '
                "tdb's config.json"
            )
        return [exe, "dap", "--listen=127.0.0.1:0"]

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
                "--terminal is not supported for Go yet (dlv dap does "
                "not route the debuggee to a caller-provided terminal)"
            )
        mode = self._mode or ("exec" if is_go_binary(program) else "debug")
        body: dict[str, Any] = {
            "type": "go",
            "request": "launch",
            "mode": mode,
            "program": program,
            "args": args,
            "cwd": cwd,
            "stopOnEntry": stop_on_entry,
        }
        if env:
            body["env"] = env
        return body

    def attach_body(
        self, *, host: str, port: int, opts: dict[str, Any]
    ) -> dict[str, Any]:
        if self._attach_pid is not None:
            # Stop on attach so the user gets control immediately —
            # the same UX debugpy's pre-armed pause gives Python attach.
            return {
                "mode": "local",
                "processId": self._attach_pid,
                "stopOnEntry": True,
            }
        # -r host:port — tdb connected straight to a user-run
        # `dlv dap --listen`; the attach request selects remote mode.
        return {"mode": "remote"}


def build_go_profile(
    adapter: str | None = None,
    adapter_paths: dict[str, str] | None = None,
    program: str | None = None,
    *,
    test: bool = False,
    attach_pid: int | None = None,
) -> LanguageProfile:
    adapter_id = adapter or "dlv"
    if adapter_id != "dlv":
        raise LanguageNotSupportedError(
            f"unknown adapter {adapter_id!r} for go (known: dlv)"
        )
    executable = (adapter_paths or {}).get("dlv")
    return LanguageProfile(
        id="go",
        display_name="Go",
        adapter=DelveAdapter(
            executable=executable,
            mode="test" if test else None,
            attach_pid=attach_pid,
        ),
        presentation=Presentation(lexer="go"),
        capabilities=ProfileCapabilities(
            pause_while_running=True,  # dlv dap honors DAP `pause` -> --run works
            concurrency_inspection="go",
        ),
    )

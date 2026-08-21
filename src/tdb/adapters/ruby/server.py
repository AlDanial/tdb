"""DAP proxy between tdb (stdio) and Ruby's rdbg (socket).

rdbg — the debug gem's CLI (>= 1.9) — speaks DAP natively, but only
over a UNIX/TCP socket, and its DAP `launch` handler hardcodes
nonstop mode (server_dap.rb: `@nonstop = true`). tdb expects a stdio
adapter it can spawn. This module bridges the two:

  tdb  --stdio-->  RubyDapServer  --socket-->  rdbg --open -- prog.rb

It is a store-and-forward pipe, not a debugger: every request without
a local handler is forwarded to rdbg with its seq renumbered, and
rdbg's events/responses flow back the same way. Locally handled:

  initialize — answered from static CAPABILITIES (rdbg isn't running yet)
  launch     — spawns rdbg, connects, then forwards the request AS an
               rdbg `attach` with nonstop=(not stopOnEntry): rdbg's
               DAP `attach` honors nonstop and emits stopped("pause")
               after configurationDone, which `launch` never does.
  disconnect / terminate — kill the rdbg process group (no orphans).

rdbg does NOT forward debuggee stdout/stderr as DAP output events; the
proxy pumps the child's pipes into `output` events itself, filtering
rdbg's own "DEBUGGER:" banner lines.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from tdb.dap.messages import parse_message
from tdb.dap.protocol import encode_message, read_message
from tdb.dap.reverse import ReverseRequester, ReverseRequestError

log = logging.getLogger(__name__)

# Mirrors what rdbg (debug gem 1.11) actually advertises, minus
# supportsStepBack: rdbg supports it but tdb has no step-back UI, so
# re-advertising it would be a lie to tdb's capability checks.
CAPABILITIES = {
    "supportsConfigurationDoneRequest": True,
    "supportsConditionalBreakpoints": True,
    "supportsCompletionsRequest": True,
    "supportsEvaluateForHovers": True,
    "supportsFunctionBreakpoints": True,
    "supportsExceptionFilterOptions": True,
    "supportsTerminateRequest": True,
    "supportTerminateDebuggee": True,
    "exceptionBreakpointFilters": [
        {
            "filter": "any",
            "label": "rescue any exception",
            "supportsCondition": True,
        },
        {
            "filter": "RuntimeError",
            "label": "rescue RuntimeError",
            "supportsCondition": True,
        },
    ],
}

MIN_DEBUG_GEM = (1, 9)

RDBG_HINT = (
    "rdbg not found on PATH — install Ruby's debug gem "
    '(`gem install debug`), or set {"adapters": {"rdbg": '
    '"/path/to/rdbg"}} in tdb\'s config.json'
)

# rdbg's own stderr chatter ("Debugger can attach via ...",
# "Connected.") — adapter noise, not program output.
_BANNER_PREFIX = "DEBUGGER: "

# rdbg greets DAP clients with a "Ruby REPL: ..." console output event.
_REPL_NOTICE = "Ruby REPL:"


class SeqTranslator:
    """Renumber seq/request_seq between the two sides of the proxy.

    Each side sees a gapless seq space owned by the proxy. A forwarded
    request remembers the originator's seq so the answering side's
    response can be restamped with it; responses to requests the proxy
    itself originated (its own initialize/terminate to rdbg) have no
    mapping and translate to None — exactly what the proxy wants, since
    it must swallow those.
    """

    def __init__(self) -> None:
        self._client_seq = 0  # last seq sent TO the client
        self._rdbg_seq = 0  # last seq sent TO rdbg
        self._from_client: dict[int, int] = {}  # rdbg-side seq -> client seq
        self._from_rdbg: dict[int, int] = {}  # client-side seq -> rdbg seq

    def next_client_seq(self) -> int:
        self._client_seq += 1
        return self._client_seq

    def next_rdbg_seq(self) -> int:
        self._rdbg_seq += 1
        return self._rdbg_seq

    def client_request_to_rdbg(self, msg: dict) -> dict:
        out = dict(msg)
        out["seq"] = self.next_rdbg_seq()
        self._from_client[out["seq"]] = msg["seq"]
        return out

    def rdbg_response_to_client(self, msg: dict) -> dict | None:
        orig = self._from_client.pop(msg.get("request_seq", -1), None)
        if orig is None:
            return None
        out = dict(msg)
        out["seq"] = self.next_client_seq()
        out["request_seq"] = orig
        return out

    def rdbg_event_to_client(self, msg: dict) -> dict:
        out = dict(msg)
        out["seq"] = self.next_client_seq()
        return out

    def rdbg_request_to_client(self, msg: dict) -> dict:
        out = dict(msg)
        out["seq"] = self.next_client_seq()
        self._from_rdbg[out["seq"]] = msg["seq"]
        return out

    def client_response_to_rdbg(self, msg: dict) -> dict | None:
        orig = self._from_rdbg.pop(msg.get("request_seq", -1), None)
        if orig is None:
            return None
        out = dict(msg)
        out["seq"] = self.next_rdbg_seq()
        out["request_seq"] = orig
        return out


@dataclass
class _Transport:
    rdbg_args: list[str]
    connect: Callable[[], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]
    cleanup: Callable[[], None] = lambda: None


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pick_transport() -> _Transport:
    """UNIX socket where possible; TCP on 127.0.0.1 otherwise.

    AF_UNIX paths are limited to ~107 bytes — a long TMPDIR silently
    breaks connect(), so fall back to TCP past a 90-char margin. No
    --cookie: rdbg's cookie check lives in its own protocol greeting,
    not DAP; binding to 127.0.0.1 is the actual boundary.
    """
    if os.name != "nt":
        sock_dir = tempfile.mkdtemp(prefix="tdb-rdbg-")
        sock_path = os.path.join(sock_dir, "s")
        if len(sock_path) < 90:

            def cleanup() -> None:
                shutil.rmtree(sock_dir, ignore_errors=True)

            return _Transport(
                ["--sock-path", sock_path],
                lambda: asyncio.open_unix_connection(sock_path),
                cleanup,
            )
        shutil.rmtree(sock_dir, ignore_errors=True)
    port = _free_port()
    return _Transport(
        ["--port", str(port), "--host", "127.0.0.1"],
        lambda: asyncio.open_connection("127.0.0.1", port),
    )


async def _rdbg_version(rdbg: str) -> tuple[int, int]:
    """Parse `rdbg --version` ("rdbg 1.11.1") into (major, minor)."""
    proc = await asyncio.create_subprocess_exec(
        rdbg,
        "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    m = re.search(rb"(\d+)\.(\d+)\.\d+", out)
    if not m:
        raise RuntimeError(f"could not parse `rdbg --version` output: {out!r}")
    return int(m.group(1)), int(m.group(2))

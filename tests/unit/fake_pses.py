"""A stand-in for `pwsh Start-EditorServices.ps1 ... -DebugServiceOnly
-DebugServicePipeName N`, for unit-testing the PowerShell proxy without
pwsh or PSES installed (POSIX only: the proxy spawns it as `pwsh`).

Speaks just enough DAP to exercise the proxy's rewrites:
  initialize        -> PSES's real capability list
  launch            -> success, then `initialized`; records arguments
  setBreakpoints    -> echoes each breakpoint as verified; records lists
  configurationDone -> prints the prompt echo + "hello from fake" on
                       stdout, then stops (reason "breakpoint") if any
                       breakpoint was ever set, else finishes the script
  continue          -> prints "after continue", exit sentinel 3, `terminated`
  next / stepIn / stepOut -> `stopped` reason "step"
  pause             -> `stopped` reason "step" (PSES really does this)
  evaluate          -> result "ctx=<context>:<expression>"
  stackTrace        -> one <Breakpoint> label frame (+ one real frame
                       when `levels` > 1)
  threads / scopes / variables -> minimal canned bodies
  disconnect        -> success; the process exits 0 shortly after
  terminate         -> "Method not found - terminate" (as PSES)

FAKE_PSES_MODE:
  "throw"           -> configurationDone prints a ConciseView block and
                       sends `terminated` without a sentinel
  "no-session-file" -> never writes the session file (proxy must time out)
  "die"             -> prints "boom: bad module" and exits 3 at once
  "old-version"     -> session file says powerShellVersion "5.1.0"
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

CAPS = {
    "supportsConfigurationDoneRequest": True,
    "supportsFunctionBreakpoints": True,
    "supportsConditionalBreakpoints": True,
    "supportsHitConditionalBreakpoints": True,
    "supportsSetVariable": True,
    "supportsDelayedStackTraceLoading": True,
    "supportsLogPoints": True,
    "supportsCancelRequest": True,
}

PROMPT = "PS /tmp/fake> . '/x/tdb_launch.ps1' '/x/s.ps1'"
SENTINEL = "\x1etdb-exit:3"
ERROR_BLOCK = [
    "Exception: /x/s.ps1:2",
    "Line |",
    '   2 |  throw "kaboom"',
    "     |  ~~~~~~~~~~~~~~",
    "     | kaboom",
]


def _arg(argv: list[str], flag: str) -> str | None:
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
    return None


def _out(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


class Fake:
    def __init__(self, log_path: str | None) -> None:
        self.seq = 0
        self.bps_seen = False
        self.log = open(log_path, "a") if log_path else None
        self.script_path = "/x/s.ps1"

    def record(self, msg: dict) -> None:
        if self.log:
            self.log.write(json.dumps(msg) + "\n")
            self.log.flush()

    def _send(self, w: asyncio.StreamWriter, msg: dict) -> None:
        self.seq += 1
        msg["seq"] = self.seq
        body = json.dumps(msg).encode()
        w.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)

    def resp(self, w, req, body=None, ok=True, message=None):
        m = {
            "type": "response",
            "request_seq": req["seq"],
            "command": req["command"],
            "success": ok,
        }
        if body is not None:
            m["body"] = body
        if message:
            m["message"] = message
        self._send(w, m)

    def event(self, w, name, body=None):
        m = {"type": "event", "event": name}
        if body is not None:
            m["body"] = body
        self._send(w, m)

    def stopped(self, w, reason, line):
        self.line = line
        self.event(
            w, "stopped", {"reason": reason, "threadId": 1, "allThreadsStopped": True}
        )

    async def handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        mode = os.environ.get("FAKE_PSES_MODE", "")
        self.line = 0
        while True:
            header = b""
            while not header.endswith(b"\r\n\r\n"):
                chunk = await r.read(1)
                if not chunk:
                    return
                header += chunk
            n = int(header.split(b":")[1])
            req = json.loads(await r.readexactly(n))
            self.record(req)
            if req.get("type") != "request":
                continue
            cmd = req["command"]
            args = req.get("arguments") or {}
            if cmd == "initialize":
                self.resp(w, req, CAPS)
            elif cmd == "launch":
                self.resp(w, req, {})
                self.event(w, "initialized", {})
            elif cmd == "setBreakpoints":
                bps = args.get("breakpoints") or []
                self.bps_seen = self.bps_seen or bool(bps)
                self.resp(
                    w,
                    req,
                    {
                        "breakpoints": [
                            {
                                "id": i,
                                "verified": True,
                                "line": b["line"],
                                "source": args.get("source"),
                            }
                            for i, b in enumerate(bps)
                        ]
                    },
                )
            elif cmd == "configurationDone":
                self.resp(w, req, {})
                await w.drain()
                _out(PROMPT)
                _out("hello from fake")
                if mode == "throw":
                    for ln in ERROR_BLOCK:
                        _out(ln)
                    self.event(w, "terminated")
                elif self.bps_seen:
                    self.stopped(w, "breakpoint", 6)
                else:
                    _out(SENTINEL)
                    self.event(w, "terminated")
            elif cmd == "continue":
                self.resp(w, req, {})
                await w.drain()
                _out("after continue")
                _out(SENTINEL)
                self.event(w, "terminated")
            elif cmd in ("next", "stepIn", "stepOut", "pause"):
                self.resp(w, req, {})
                self.stopped(w, "step", self.line + 1)
            elif cmd == "evaluate":
                self.resp(
                    w,
                    req,
                    {
                        "result": f"ctx={args.get('context')}:{args.get('expression')}",
                        "variablesReference": 0,
                    },
                )
            elif cmd == "stackTrace":
                frames = [
                    {
                        "id": 0,
                        "name": "<Breakpoint>",
                        "presentationHint": "label",
                        "source": {"path": self.script_path},
                        "line": self.line,
                        "column": 1,
                    }
                ]
                if int(args.get("levels") or 1) > 1:
                    frames.append(
                        {
                            "id": 1,
                            "name": "<ScriptBlock>",
                            "source": {"path": self.script_path},
                            "line": self.line,
                            "column": 0,
                        }
                    )
                self.resp(w, req, {"stackFrames": frames, "totalFrames": len(frames)})
            elif cmd == "threads":
                self.resp(
                    w,
                    req,
                    {"threads": [{"id": 1, "name": "PowerShell Pipeline Thread"}]},
                )
            elif cmd == "scopes":
                self.resp(
                    w,
                    req,
                    {
                        "scopes": [
                            {
                                "name": "Local",
                                "variablesReference": 75,
                                "expensive": False,
                            }
                        ]
                    },
                )
            elif cmd == "variables":
                self.resp(
                    w,
                    req,
                    {
                        "variables": [
                            {"name": "$x", "value": "1", "variablesReference": 0}
                        ]
                    },
                )
            elif cmd == "disconnect":
                self.resp(w, req, {})
                await w.drain()
                await asyncio.sleep(0.1)
                os._exit(0)
            elif cmd == "terminate":
                self.resp(
                    w, req, None, ok=False, message="Method not found - terminate"
                )
            else:
                self.resp(w, req, {})
            await w.drain()


async def main(argv: list[str]) -> int:
    mode = os.environ.get("FAKE_PSES_MODE", "")
    if mode == "die":
        _out("boom: bad module")
        return 3
    session = _arg(argv, "-SessionDetailsPath")
    assert session, "fake_pses: -SessionDetailsPath missing"
    sock = str(Path(session).parent / "sock")
    fake = Fake(os.environ.get("FAKE_PSES_LOG"))
    server = await asyncio.start_unix_server(fake.handle, path=sock)
    if mode != "no-session-file":
        version = "5.1.0" if mode == "old-version" else "7.6.5"
        Path(session).write_text(
            json.dumps(
                {
                    "status": "started",
                    "debugServiceTransport": "NamedPipe",
                    "debugServicePipeName": sock,
                    "powerShellVersion": version,
                }
            )
        )
    async with server:
        await server.serve_forever()
    return 0


def make_fake_pwsh(tmp_path: Path) -> tuple[str, Path]:
    """Write a `pwsh` sh-shim that runs this module, plus a stub PSES dir."""
    shim = tmp_path / "pwsh"
    shim.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{Path(__file__).resolve()}" "$@"\n'
    )
    shim.chmod(0o755)
    pses = tmp_path / "PowerShellEditorServices"
    pses.mkdir()
    (pses / "Start-EditorServices.ps1").write_text("# stub\n")
    return str(shim), pses


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))

"""DAP stdio server for the Perl adapter.

One request at a time is dispatched from the read loop; handlers are
`_on_<command>` methods collected into `self.handlers` so later tasks
extend the surface by adding methods. Events may be emitted at any
time via send_event (the session driver calls it from its own task).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable

from tdb.dap.messages import Event, Request, Response, parse_message
from tdb.dap.protocol import encode_message, read_message

from .refs import RefRegistry
from .session import PerlProtocolError, PerlSession

log = logging.getLogger(__name__)

CAPABILITIES = {
    "supportsConfigurationDoneRequest": True,
    "supportsConditionalBreakpoints": True,
    "supportsTerminateRequest": True,
}


class PerlDapServer:
    def __init__(self, reader: asyncio.StreamReader, writer: Any) -> None:
        self._reader = reader
        self._writer = writer
        self._seq = 0
        self._done = asyncio.Event()
        self.session: PerlSession | None = None
        self.current_stop: dict | None = None
        self._launch_request: Request | None = None
        self._stop_on_entry = True
        self._classifying = False
        self._pause_pending = False
        self.breakpoint_lines: dict[str, set[int]] = {}
        self.refs = RefRegistry()
        self.handlers: dict[str, Callable[[Request], Awaitable[None]]] = {}
        for name in dir(self):
            if name.startswith("_on_"):
                self.handlers[name[4:]] = getattr(self, name)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _write(self, msg: dict) -> None:
        self._writer.write(encode_message(msg))

    def send_response(self, request: Request, body: dict | None = None) -> None:
        self._write(
            Response(
                seq=self._next_seq(),
                request_seq=request.seq,
                command=request.command,
                success=True,
                body=body or {},
            ).to_dict()
        )

    def send_error(self, request: Request, message: str) -> None:
        self._write(
            Response(
                seq=self._next_seq(),
                request_seq=request.seq,
                command=request.command,
                success=False,
                message=message,
            ).to_dict()
        )

    def send_event(self, event: str, body: dict | None = None) -> None:
        self._write(Event(seq=self._next_seq(), event=event, body=body or {}).to_dict())

    async def run(self) -> None:
        while not self._done.is_set():
            try:
                raw = await read_message(self._reader)
            except (ConnectionError, asyncio.IncompleteReadError, EOFError):
                break
            msg = parse_message(raw)
            if not isinstance(msg, Request):
                continue
            handler = self.handlers.get(msg.command)
            if handler is None:
                self.send_error(msg, f"unsupported command: {msg.command}")
                continue
            try:
                await handler(msg)
            except Exception as e:
                log.exception("handler %s failed", msg.command)
                self.send_error(msg, str(e))
            await self._writer.drain()
        await self._writer.drain()

    async def _on_initialize(self, request: Request) -> None:
        self.send_response(request, CAPABILITIES)

    async def _on_disconnect(self, request: Request) -> None:
        if self.session is not None:
            await self.session.stop()
            self.session = None
        self.send_response(request)
        self._done.set()

    async def _on_terminate(self, request: Request) -> None:
        if self.session is not None:
            await self.session.stop()
            self.session = None
        self.send_response(request)
        self._done.set()

    async def _on_launch(self, request: Request) -> None:
        args = request.arguments
        perl = args.get("perl") or "perl"
        preflight = await asyncio.create_subprocess_exec(
            perl,
            "-e",
            "require v5.18",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await preflight.communicate()
        if preflight.returncode != 0:
            self.send_error(
                request,
                f"perl >= 5.18 not usable ({perl!r}): "
                f"{err.decode(errors='replace').strip() or 'not found'} — "
                'install perl or set {"adapters": {"perl": "/path/to/perl"}} '
                "in tdb's config.json",
            )
            return
        program = args.get("program", "")
        if not os.path.isfile(program):
            self.send_error(request, f"program not found: {program}")
            return
        self._stop_on_entry = bool(args.get("stopOnEntry", True))
        self.session = PerlSession(
            on_output=self._forward_output, on_stop=self._on_unsolicited_stop
        )
        try:
            await self.session.launch(
                program=program,
                args=list(args.get("args") or []),
                cwd=args.get("cwd") or os.getcwd(),
                env=args.get("env"),
                perl=perl,
            )
        except PerlProtocolError as e:
            try:
                await self.session.stop()
            except Exception:
                log.exception("session.stop() failed while tearing down failed launch")
            self.session = None
            self.send_error(request, f"{e} [{e.tail}]")
            return
        self._launch_request = request
        self.send_event("initialized")
        # response is sent by _on_configurationDone (DAP ordering)

    async def _on_configurationDone(self, request: Request) -> None:
        self.send_response(request)
        if self._launch_request is not None:
            self.send_response(self._launch_request)
            self._launch_request = None
            if self._stop_on_entry:
                await self._emit_stopped("entry")
            else:
                self.session.resume("c")

    async def _emit_stopped(self, reason: str) -> None:
        self.refs.reset()
        try:
            self.current_stop = await self.session.helper(
                "Devel::TdbHelper::location()"
            )
        except PerlProtocolError as e:
            log.error("location() failed after stop: %s", e)
            self.current_stop = None
        self.send_event(
            "stopped",
            {"reason": reason, "threadId": 1, "allThreadsStopped": True},
        )

    def _forward_output(self, text: str, category: str) -> None:
        if category == "__eof__":
            self.send_event("terminated")
            self.send_event("exited", {"exitCode": 0})
            return
        self.send_event("output", {"category": category, "output": text})

    def _on_unsolicited_stop(self) -> None:
        self._classifying = True
        asyncio.ensure_future(self._classify_and_emit_stop())

    async def _classify_and_emit_stop(self) -> None:
        self.refs.reset()
        try:
            if self.session is None:
                return
            try:
                loc = await self.session.helper("Devel::TdbHelper::location()")
            except PerlProtocolError:
                loc = None
            if loc is None or loc.get("file") == "?":
                # perl5db's "ended" state: the debuggee ran to completion and
                # perl5db parked at a live prompt without closing the socket
                # (no user frames left, so location() can't report one).
                self.current_stop = None
                self.send_event("terminated")
                self.send_event("exited", {"exitCode": 0})
                return
            self.current_stop = loc
            had_pause_pending = self._pause_pending
            self._pause_pending = False
            if loc.get("line") in self.breakpoint_lines.get(loc.get("file"), set()):
                reason = "breakpoint"
            elif had_pause_pending:
                reason = "pause"
            else:
                reason = "step"
            self.send_event(
                "stopped",
                {"reason": reason, "threadId": 1, "allThreadsStopped": True},
            )
            await self._writer.drain()
        finally:
            self._classifying = False

    async def _on_threads(self, request: Request) -> None:
        self.send_response(request, {"threads": [{"id": 1, "name": "main"}]})

    async def _resume(self, request: Request, cmd: str) -> None:
        if self.session is None or not self.session.stopped or self._classifying:
            self.send_error(request, "debuggee is not stopped")
            return
        self.current_stop = None
        self.send_response(request)
        self.send_event("continued", {"threadId": 1, "allThreadsContinued": True})
        self.session.resume(cmd)

    async def _on_continue(self, request: Request) -> None:
        await self._resume(request, "c")

    async def _on_next(self, request: Request) -> None:
        await self._resume(request, "n")

    async def _on_stepIn(self, request: Request) -> None:
        await self._resume(request, "s")

    async def _on_stepOut(self, request: Request) -> None:
        await self._resume(request, "r")

    async def _on_pause(self, request: Request) -> None:
        if self.session is None:
            self.send_error(request, "no session")
            return
        if self.session.stopped:
            self.send_response(request)
            return
        if not self.session.interrupt():
            self.send_error(request, "pause is not available for this session")
            return
        # TOCTOU: between the not-stopped check above and interrupt()
        # returning, a natural stop (e.g. a breakpoint) can land and start
        # being classified. If so, that stop already satisfies the user's
        # "be stopped" intent, so don't arm _pause_pending -- doing so would
        # mislabel the *next* stop as a pause instead of the one that
        # actually happened.
        if self.session.stopped or self._classifying:
            self.send_response(request)
            return
        self._pause_pending = True
        self.send_response(request)

    async def _on_source(self, request: Request) -> None:
        path = request.arguments.get("source", {}).get("path", "")
        payload = await self.session.helper(
            f"Devel::TdbHelper::source({self._perl_str(path)})"
        )
        if not payload.get("text"):
            self.send_error(request, f"no compiled source for {path}")
            return
        self.send_response(request, {"content": payload["text"]})

    async def _on_setBreakpoints(self, request: Request) -> None:
        if self.session is None or not self.session.stopped:
            self.send_error(request, "cannot set breakpoints while running")
            return
        path = request.arguments.get("source", {}).get("path", "")
        wanted = request.arguments.get("breakpoints", [])
        old_lines = self.breakpoint_lines.get(path, set())
        if old_lines:
            # `B <line>` is scoped to perl5db's CURRENT file, which is
            # whichever frame the debugger last stopped in -- not
            # necessarily `path`. Switch to `path` first so deletions
            # land in the right per-file breakpoint table. If perl5db
            # never loaded `path` (nothing to delete -- no breakpoint
            # could have been set there in the first place), `f` prints
            # "No file matching '<path>' is loaded." and we skip the
            # deletions rather than delete from whatever file `f` left
            # current.
            f_events = await self.session.command(f"f {path}")
            if not any(e[0] == "text" and "No file matching" in e[1] for e in f_events):
                for old_line in old_lines:
                    await self.session.command(f"B {old_line}")
        try:
            # Devel::TdbHelper::breakable() guards internally against
            # files perl hasn't compiled yet (see helpers.pl) so this is
            # safe to call speculatively even if `path` isn't loaded --
            # it reports {"lines": [], "unloaded": 1} without touching
            # perl5db's per-file line table, instead of the empty-set
            # fallback below silently masking a real protocol error.
            breakable = set(
                (
                    await self.session.helper(
                        f"Devel::TdbHelper::breakable({self._perl_str(path)})"
                    )
                )["lines"]
            )
        except PerlProtocolError:
            breakable = set()
        results = []
        actual_lines: set[int] = set()
        for bp in wanted:
            line = bp["line"]
            target = line
            if breakable and line not in breakable:
                later = sorted(n for n in breakable if n > line)
                target = later[0] if later else None
            if target is None:
                results.append({"verified": False, "line": line})
                continue
            cond = bp.get("condition")
            cmd = f"b {path}:{target}" + (f" {cond}" if cond else "")
            events = await self.session.command(cmd)
            failed = any(e[0] == "text" and "not breakable" in e[1] for e in events)
            if failed:
                results.append({"verified": False, "line": line})
            else:
                results.append({"verified": True, "line": target})
                actual_lines.add(target)
        self.breakpoint_lines[path] = actual_lines
        self.send_response(request, {"breakpoints": results})

    @staticmethod
    def _perl_str(s: str) -> str:
        return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"

    async def _on_stackTrace(self, request: Request) -> None:
        payload = await self.session.helper("Devel::TdbHelper::stack()")
        frames = []
        for i, f in enumerate(payload["frames"]):
            frames.append(
                {
                    "id": i,
                    "name": f.get("sub") or "main",
                    "line": f["line"],
                    "column": 1,
                    "source": {"path": f["file"]},
                }
            )
        self.send_response(request, {"stackFrames": frames, "totalFrames": len(frames)})

    async def _on_scopes(self, request: Request) -> None:
        frame = request.arguments.get("frameId", 0)
        payload = await self.session.helper(f"Devel::TdbHelper::scopes({frame})")
        scopes = [
            {
                "name": s["name"],
                "variablesReference": self.refs.add_scope(frame, s["kind"]),
                "expensive": False,
            }
            for s in payload["scopes"]
        ]
        self.send_response(request, {"scopes": scopes})

    async def _on_variables(self, request: Request) -> None:
        ref = request.arguments.get("variablesReference", 0)
        entry = self.refs.get(ref)
        if entry is None:
            self.send_error(request, f"stale variablesReference {ref}")
            return
        if entry["kind"] == "scope":
            payload = await self.session.helper(
                f"Devel::TdbHelper::vars({entry['frame']}, "
                f"{self._perl_str(entry['scope'])})"
            )
        else:
            payload = await self.session.helper(
                f"Devel::TdbHelper::expand({entry['helper_id']})"
            )
        variables = []
        if payload.get("degraded"):
            variables.append(
                {
                    "name": "<lexicals>",
                    "value": payload["degraded"],
                    "variablesReference": 0,
                }
            )
        for v in payload.get("vars", []):
            variables.append(
                {
                    "name": v["name"],
                    "value": v["value"],
                    "variablesReference": (
                        self.refs.add_object(v["id"]) if v["id"] else 0
                    ),
                }
            )
        self.send_response(request, {"variables": variables})

    async def _on_evaluate(self, request: Request) -> None:
        expr = request.arguments.get("expression", "").replace("\n", " ")
        # Leading `;` keeps this from being mistaken for perl5db's own
        # `{ command }` pre-prompt-action syntax (a bare leading `{` is
        # captured by perl5db itself and deferred rather than eval'd now
        # -- confirmed via manual perl5db probe, Task 11). The `;` is a
        # no-op statement separator so the wrapped Perl is unaffected.
        cmd = (
            ";{ local $@; my $r = [ eval { " + expr + " } ]; "
            "Devel::TdbHelper::emit_eval($r, $@) }"
        )
        try:
            payload = await self.session.helper(cmd)
        except PerlProtocolError as e:
            self.send_error(request, str(e))
            return
        self.send_response(
            request,
            {
                "result": payload["value"],
                "variablesReference": (
                    self.refs.add_object(payload["id"]) if payload.get("id") else 0
                ),
            },
        )

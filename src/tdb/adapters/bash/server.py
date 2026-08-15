"""DAP stdio server for the Bash adapter.

Same dispatch shape as adapters/perl/server.py: _on_<command> methods
collected into self.handlers; the launch response is deferred until
configurationDone (DAP ordering). Much simpler than perl: the harness
delivers clean stop events, so there is no stop-classification pass.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable

from tdb.dap.messages import Event, Request, Response, parse_message
from tdb.dap.protocol import encode_message, read_message
from tdb.dap.reverse import ReverseRequester

from .declares import BashVar
from .session import BashProtocolError, BashSession

log = logging.getLogger(__name__)

CAPABILITIES = {
    "supportsConfigurationDoneRequest": True,
    "supportsConditionalBreakpoints": True,
    "supportsTerminateRequest": True,
}


class BashDapServer:
    def __init__(self, reader: asyncio.StreamReader, writer: Any) -> None:
        self._reader = reader
        self._writer = writer
        self._seq = 0
        self._done = asyncio.Event()
        self.session: BashSession | None = None
        self._start_request: Request | None = None
        self._stop_on_entry = True
        self._launched = False  # True once launch() succeeded;
        # gates exited/terminated events
        # breakpoints per local path, applied via session.set_breakpoint;
        # kept to rebuild the table on setBreakpoints (clearall + re-set)
        self._breakpoints: dict[str, list[dict]] = {}
        # variablesReference registry: ref -> ("scope", frame, kind) or
        # ("children", [(key, value), ...]); reset at every stop
        self._refs: dict[int, tuple] = {}
        self._next_ref = 1
        self._stack_cache: list[dict] | None = None
        self._reverse = ReverseRequester(self._write, self._next_seq)
        self._client_supports_run_in_terminal = False
        # Strong ref: asyncio only holds a weak reference to a bare
        # ensure_future() task, so it can be garbage-collected mid-flight
        # if nothing else keeps it alive. An externalTerminal launch runs
        # in the background so run()'s read loop can service the
        # runInTerminal reverse-request reply (see _on_launch).
        self._launch_task: asyncio.Future | None = None
        self.handlers: dict[str, Callable[[Request], Awaitable[None]]] = {}
        for name in dir(self):
            if name.startswith("_on_"):
                self.handlers[name[4:]] = getattr(self, name)

    # ---- plumbing (identical shape to the perl server) ----
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
            if self._reverse.route(msg):
                await self._writer.drain()
                continue
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

    # ---- session callbacks ----
    def _forward_output(self, text: str, category: str) -> None:
        self.send_event("output", {"category": category, "output": text})

    def _on_session_stop(self, reason: str, path: str, line: int) -> None:
        self._refs = {}
        self._next_ref = 1
        self._stack_cache = None
        self.send_event(
            "stopped",
            {
                "reason": reason,
                "threadId": 1,
                "allThreadsStopped": True,
            },
        )

    def _on_session_exit(self, code: int) -> None:
        if not self._launched:
            return  # pre-handshake death is reported via the launch error
        self.send_event("exited", {"exitCode": code})
        self.send_event("terminated")

    # ---- lifecycle ----
    async def _on_initialize(self, request: Request) -> None:
        self._client_supports_run_in_terminal = bool(
            request.arguments.get("supportsRunInTerminalRequest")
        )
        self.send_response(request, CAPABILITIES)

    async def _cancel_launch_task(self) -> None:
        """Cancel and await an in-flight externalTerminal `_finish_launch`
        background task before disconnect/terminate tear down the session.

        Without this, a still-running launch continuation can assign
        `self.session = BashSession(...)` (the first thing `_finish_launch`
        does, synchronously, before any await) AFTER disconnect/terminate's
        own `if self.session is not None` check already ran and found
        nothing to stop -- leaking a live (or about-to-be-live) debuggee
        with nothing left to ever call session.stop() on it. Cancelling and
        awaiting first makes the eventual `self.session is not None` check
        below see any session the task managed to create before it was
        cancelled, so it still gets torn down.
        """
        task, self._launch_task = self._launch_task, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("launch task raised during cancellation")

    async def _on_launch(self, request: Request) -> None:
        args = request.arguments
        program = args.get("program", "")
        if not os.path.isfile(program):
            self.send_error(request, f"program not found: {program}")
            return
        run_in_terminal = None
        if args.get("console") == "externalTerminal":
            if not self._client_supports_run_in_terminal:
                self.send_error(
                    request,
                    "externalTerminal launch requires a client that "
                    "supports the runInTerminal reverse request",
                )
                return

            async def run_in_terminal(cmd, cwd, env):
                await self._reverse.request(
                    "runInTerminal",
                    {
                        "kind": "external",
                        "title": "tdb bash debuggee",
                        "cwd": cwd,
                        "args": cmd,
                        "env": env,
                    },
                )

        self._stop_on_entry = bool(args.get("stopOnEntry", True))
        if run_in_terminal is not None:
            # session.launch() awaits self._reverse.request("runInTerminal",
            # ...), whose reply can only be read/routed by THIS coroutine's
            # own run() loop -- but run() is what's calling _on_launch, and
            # it awaits handler(msg) synchronously before reading the next
            # message. Awaiting inline here would deadlock: the reply can
            # never arrive because nothing is reading stdin anymore. Run
            # the rest of launch as a background task (strong ref, per the
            # repo's task-GC pitfall) so _on_launch returns immediately,
            # letting run() go back to reading -- including the eventual
            # runInTerminal response, which self._reverse.route(msg)
            # handles directly in run() without going through a handler.
            self._launch_task = asyncio.ensure_future(
                self._finish_launch(request, program, args, run_in_terminal)
            )
            return
        await self._finish_launch(request, program, args, run_in_terminal)

    async def _finish_launch(
        self,
        request: Request,
        program: str,
        args: dict,
        run_in_terminal,
    ) -> None:
        self.session = BashSession(
            self._forward_output, self._on_session_stop, self._on_session_exit
        )
        try:
            await self.session.launch(
                program=program,
                args=list(args.get("args") or []),
                cwd=args.get("cwd") or os.getcwd(),
                env=args.get("env"),
                bash=args.get("bash") or "bash",
                run_in_terminal=run_in_terminal,
            )
        except Exception as e:
            # Deliberately broad (mirrors run()'s own blanket `except
            # Exception` net around handler dispatch): BashProtocolError
            # and ReverseRequestError are the expected failures, but
            # session.launch() can also raise e.g. bare
            # asyncio.TimeoutError (ReverseRequester.request()'s own
            # wait_for) or OSError. In the terminal-mode (background-task)
            # case this coroutine is NOT awaited inline by run() -- there
            # is no other except-Exception net standing behind this one --
            # so anything narrower here would leave the client's launch
            # request unanswered forever and silently strand the exception
            # on self._launch_task.
            #
            # asyncio.CancelledError deliberately propagates past this
            # (BaseException, not Exception, on py>=3.8): that's
            # _on_disconnect/_on_terminate cancelling an in-flight launch,
            # not a launch failure, and they do their own cleanup.
            try:
                await self.session.stop()
            except Exception:
                log.exception("session.stop() failed while tearing down failed launch")
            self.session = None
            if not isinstance(e, BashProtocolError):
                log.exception("launch failed unexpectedly")
            self.send_error(request, str(e))
            await self._writer.drain()
            return
        self._launched = True
        self._start_request = request
        self.send_event("initialized")
        await self._writer.drain()
        # response is sent by _on_configurationDone (DAP ordering)

    async def _on_configurationDone(self, request: Request) -> None:
        if self.session is None or self._start_request is None:
            self.send_error(request, "no launch in progress")
            return
        self.send_response(self._start_request)
        self._start_request = None
        self.send_response(request)
        self.session.resume("step" if self._stop_on_entry else "continue")

    async def _on_disconnect(self, request: Request) -> None:
        await self._cancel_launch_task()
        if self.session is not None:
            await self.session.stop()
            self.session = None
        self.send_response(request)
        self._done.set()

    async def _on_terminate(self, request: Request) -> None:
        await self._on_disconnect(request)

    # ---- breakpoints ----
    async def _on_setBreakpoints(self, request: Request) -> None:
        if self.session is None:
            self.send_error(request, "no session")
            return
        path = request.arguments.get("source", {}).get("path", "")
        wanted = request.arguments.get("breakpoints", [])
        self._breakpoints[path] = wanted
        if self.session.stopped:
            # rebuild the whole table (simplest correct thing: the harness
            # table is tiny). clearall + re-set every file's breakpoints.
            await self.session.clear_breakpoints()
            for p, bps in self._breakpoints.items():
                for bp in bps:
                    await self.session.set_breakpoint(
                        p, bp["line"], bp.get("condition") or ""
                    )
        else:
            self.session.clear_breakpoints_nowait()
            for p, bps in self._breakpoints.items():
                for bp in bps:
                    self.session.set_breakpoint_nowait(
                        p, bp["line"], bp.get("condition") or ""
                    )
        self.send_response(
            request,
            {"breakpoints": [{"verified": True, "line": bp["line"]} for bp in wanted]},
        )

    # ---- execution ----
    def _not_ready(self) -> str | None:
        if self.session is None:
            return "no session"
        if not self.session.stopped:
            return "debuggee is not stopped"
        return None

    async def _resume(self, request: Request, mode: str) -> None:
        reason = self._not_ready()
        if reason is not None:
            self.send_error(request, reason)
            return
        self.send_response(request)
        self.send_event("continued", {"threadId": 1, "allThreadsContinued": True})
        self.session.resume(mode)

    async def _on_continue(self, request: Request) -> None:
        await self._resume(request, "continue")

    async def _on_next(self, request: Request) -> None:
        await self._resume(request, "next")

    async def _on_stepIn(self, request: Request) -> None:
        await self._resume(request, "step")

    async def _on_stepOut(self, request: Request) -> None:
        await self._resume(request, "finish")

    async def _on_pause(self, request: Request) -> None:
        if self.session is None:
            self.send_error(request, "no session")
            return
        if self.session.stopped:
            self.send_response(request)
            return
        self.session.pause()
        self.send_response(request)

    async def _on_threads(self, request: Request) -> None:
        self.send_response(request, {"threads": [{"id": 1, "name": "main"}]})

    # ---- inspection ----
    async def _on_stackTrace(self, request: Request) -> None:
        reason = self._not_ready()
        if reason is not None:
            self.send_error(request, reason)
            return
        if self._stack_cache is None:
            self._stack_cache = await self.session.stack()
        frames = [
            {
                "id": i,
                "name": f["func"],
                "line": f["line"],
                "column": 1,
                "source": {"path": f["file"]},
            }
            for i, f in enumerate(self._stack_cache)
        ]
        self.send_response(request, {"stackFrames": frames, "totalFrames": len(frames)})

    def _add_ref(self, entry: tuple) -> int:
        ref = self._next_ref
        self._next_ref += 1
        self._refs[ref] = entry
        return ref

    async def _on_scopes(self, request: Request) -> None:
        reason = self._not_ready()
        if reason is not None:
            self.send_error(request, reason)
            return
        frame = request.arguments.get("frameId", 0)
        # frameId sanity guard: beyond the last known stack frame is a
        # stale/bogus reference (e.g. a client-cached frameId from before a
        # resume). When there's no cached stack yet, fall back to the
        # unchanged default of treating frame 0 as valid.
        if self._stack_cache is not None and not (0 <= frame < len(self._stack_cache)):
            self.send_error(request, f"invalid frameId: {frame}")
            return
        scopes = []
        if frame == 0:
            scopes.append(
                {
                    "name": "Locals",
                    "expensive": False,
                    "variablesReference": self._add_ref(("scope", "locals")),
                }
            )
        scopes.append(
            {
                "name": "Globals",
                "expensive": False,
                "variablesReference": self._add_ref(("scope", "globals")),
            }
        )
        scopes.append(
            {
                "name": "Environment",
                "expensive": False,
                "variablesReference": self._add_ref(("scope", "environment")),
            }
        )
        self.send_response(request, {"scopes": scopes})

    def _var_to_dap(self, v: BashVar) -> dict:
        ref = 0
        if v.children is not None:
            ref = self._add_ref(("children", v.children))
        return {"name": v.name, "value": v.value, "variablesReference": ref}

    async def _on_variables(self, request: Request) -> None:
        reason = self._not_ready()
        if reason is not None:
            self.send_error(request, reason)
            return
        entry = self._refs.get(request.arguments.get("variablesReference", 0))
        if entry is None:
            self.send_error(request, "stale variablesReference")
            return
        if entry[0] == "scope":
            if entry[1] == "locals":
                vars_ = await self.session.locals()
            elif entry[1] == "environment":
                vars_ = await self.session.environment_vars()
            else:
                vars_ = await self.session.globals_vars()
            body = [self._var_to_dap(v) for v in vars_]
        else:
            body = [
                {"name": k, "value": val, "variablesReference": 0}
                for k, val in entry[1]
            ]
        self.send_response(request, {"variables": body})

    async def _on_evaluate(self, request: Request) -> None:
        reason = self._not_ready()
        if reason is not None:
            self.send_error(request, reason)
            return
        expr = request.arguments.get("expression", "")
        try:
            rc, out = await self.session.evaluate(expr)
        except BashProtocolError as e:
            self.send_error(request, str(e))
            return
        result = out if rc == 0 else f"{out}\n[exit status {rc}]".strip()
        self.send_response(request, {"result": result, "variablesReference": 0})

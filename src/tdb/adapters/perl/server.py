"""DAP stdio server for the Perl adapter.

One request at a time is dispatched from the read loop; handlers are
`_on_<command>` methods collected into `self.handlers` so later tasks
extend the surface by adding methods. Events may be emitted at any
time via send_event (the session driver calls it from its own task).

Exception: an externalTerminal `launch` runs on a background task (see
`_on_launch`/`_finish_launch`) because it must itself await a reverse
request whose response can only be read by this same read loop --
awaiting it inline would deadlock the adapter against its own client.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable

from tdb.dap.messages import Event, Request, Response, parse_message
from tdb.dap.protocol import encode_message, read_message
from tdb.dap.reverse import ReverseRequester
from tdb.dap.types import DEFERRED_VERIFICATION_MESSAGE

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
        self._classify_task: asyncio.Future | None = None
        # Strong ref for the same reason as _classify_task: asyncio only
        # holds a weak reference to a bare ensure_future() task.
        self._eof_task: asyncio.Future | None = None
        # Set just before _classify_and_emit_stop's "?" branch sends `q` to
        # force a real child exit -- tells _forward_output's "__eof__"
        # branch that the resulting socket close is expected, not a fresh
        # unsolicited termination (see both call sites for detail).
        self._eof_terminated = False
        self.current_stop: dict | None = None
        self._start_request: Request | None = None
        self._stop_on_entry = True
        self._classifying = False
        self._pause_pending = False
        # (file, line) the debuggee was stopped at when the user issued
        # the last step command (n/s/r) -- None after a continue or once
        # consumed. Lets _classify_and_emit_stop coalesce the several
        # same-line stops one source statement generates during perl's
        # compile phase (see _coalesce_compile_phase_step).
        self._step_origin: tuple | None = None
        # (local, remote) pairs, longest-prefix-first translation. Empty
        # by default (no-op) unless attach carries pathMappings.
        self._path_map: list[tuple[str, str]] = []
        # Canonical keying: breakpoint_lines is keyed by REMOTE path --
        # the same space perl5db itself reports in via location()/
        # stack(), so _classify_and_emit_stop can compare loc["file"]
        # against it directly with no further translation.
        self.breakpoint_lines: dict[str, set[int]] = {}
        # setBreakpoints requests deferred because perl was still
        # compiling `remote_path` when they arrived (see
        # _on_setBreakpoints and Background 8 in the plan): remote_path
        # -> (local_path, wanted). Flushed once phase() reports 'RUN'.
        self._pending_breakpoints: dict[str, tuple[str, list[dict]]] = {}
        self.refs = RefRegistry()
        self._reverse = ReverseRequester(self._write, self._next_seq)
        self._client_supports_run_in_terminal = False
        # Strong ref for the same reason as _classify_task/_eof_task: an
        # externalTerminal launch runs in the background so run()'s read
        # loop can service the runInTerminal reverse-request reply (see
        # _on_launch).
        self._launch_task: asyncio.Future | None = None
        self.handlers: dict[str, Callable[[Request], Awaitable[None]]] = {}
        for name in dir(self):
            if name.startswith("_on_"):
                self.handlers[name[4:]] = getattr(self, name)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _to_remote(self, path: str) -> str:
        """Local (caller-facing) path -> remote (perl5db-facing) path."""
        return self._translate_path(path, self._path_map)

    def _to_local(self, path: str) -> str:
        """Remote (perl5db-facing) path -> local (caller-facing) path."""
        return self._translate_path(
            path, [(remote, local) for local, remote in self._path_map]
        )

    @staticmethod
    def _translate_path(path: str, mapping: list[tuple[str, str]]) -> str:
        if not mapping:
            return path
        best: tuple[str, str] | None = None
        for src, dst in mapping:
            if path == src or path.startswith(src + "/") or path.startswith(src + "\\"):
                if best is None or len(src) > len(best[0]):
                    best = (src, dst)
        if best is None:
            return path
        src, dst = best
        return dst + path[len(src) :]

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

    async def _on_initialize(self, request: Request) -> None:
        self._client_supports_run_in_terminal = bool(
            request.arguments.get("supportsRunInTerminalRequest")
        )
        self.send_response(request, CAPABILITIES)

    async def _cancel_launch_task(self) -> None:
        """Cancel and await an in-flight externalTerminal `_finish_launch`
        background task before disconnect/terminate tear down the session.

        Without this, a still-running launch continuation can assign
        `self.session = PerlSession(...)` (the first thing `_finish_launch`
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

    async def _on_disconnect(self, request: Request) -> None:
        await self._cancel_launch_task()
        if self.session is not None:
            await self.session.stop()
            self.session = None
        self.send_response(request)
        self._done.set()

    async def _on_terminate(self, request: Request) -> None:
        await self._cancel_launch_task()
        if self.session is not None:
            await self.session.stop()
            self.session = None
        self.send_response(request)
        self._done.set()

    async def _on_launch(self, request: Request) -> None:
        args = request.arguments
        perl = args.get("perl") or "perl"
        try:
            preflight = await asyncio.create_subprocess_exec(
                perl,
                "-e",
                "require v5.18",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            self.send_error(
                request,
                f"perl >= 5.18 not usable ({perl!r}): {e} — "
                'install perl or set {"adapters": {"perl": "/path/to/perl"}} '
                "in tdb's config.json",
            )
            return
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
                        "title": "tdb perl debuggee",
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
                self._finish_launch(request, program, perl, args, run_in_terminal)
            )
            return
        await self._finish_launch(request, program, perl, args, run_in_terminal)

    async def _finish_launch(
        self,
        request: Request,
        program: str,
        perl: str,
        args: dict,
        run_in_terminal,
    ) -> None:
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
                run_in_terminal=run_in_terminal,
            )
        except Exception as e:
            # Deliberately broad (mirrors run()'s own blanket `except
            # Exception` net around handler dispatch): PerlProtocolError
            # and ReverseRequestError are the expected failures, but
            # session.launch() can also raise e.g. bare
            # asyncio.TimeoutError (ReverseRequester.request()'s own
            # wait_for) or OSError (tempfile.mkdtemp). In the terminal-mode
            # (background-task) case this coroutine is NOT awaited inline
            # by run() -- there is no other except-Exception net standing
            # behind this one -- so anything narrower here would leave the
            # client's launch request unanswered forever and silently
            # strand the exception on self._launch_task.
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
            if isinstance(e, PerlProtocolError):
                self.send_error(request, f"{e} [{e.tail}]")
            else:
                log.exception("launch failed unexpectedly")
                self.send_error(request, str(e))
            await self._writer.drain()
            return
        self._start_request = request
        self.send_event("initialized")
        await self._writer.drain()
        # response is sent by _on_configurationDone (DAP ordering)

    async def _on_attach(self, request: Request) -> None:
        host = request.arguments.get("host", "127.0.0.1")
        port = request.arguments.get("port", 0)
        self._path_map = [
            (
                pm["localRoot"].rstrip("/\\"),
                pm["remoteRoot"].rstrip("/\\"),
            )
            for pm in request.arguments.get("pathMappings") or []
        ]
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), 10.0
            )
        except (OSError, asyncio.TimeoutError) as e:
            self.send_error(
                request,
                f"cannot connect to {host}:{port} ({e}) — has the program "
                "called Devel::TdbRemote::listen() and wait_for_client(), "
                "and is the port reachable?",
            )
            return
        self.session = PerlSession(
            on_output=self._forward_output, on_stop=self._on_unsolicited_stop
        )
        try:
            await self.session.attach_socket(reader, writer)
            loc = await self.session.helper("Devel::TdbHelper::location()")
        except PerlProtocolError as e:
            try:
                await self.session.stop()
            finally:
                self.session = None
            self.send_error(request, f"attach handshake failed: {e} [{e.tail}]")
            return
        if loc.get("version") != 1:
            self.send_error(
                request,
                f"protocol mismatch (debuggee helpers v{loc.get('version')}, "
                "adapter expects v1) — update Devel/TdbRemote.pm and "
                "helpers.pl on the remote host",
            )
            return
        # TdbRemote::wait_for_client() arms $DB::single right before its
        # own `return;` statement, so the very first trap DB::DB hits is
        # literally that `return;` line -- still inside
        # Devel::TdbRemote, not yet back in the caller. Step once more
        # (blocking, via the command queue so it can't race an
        # unsolicited-stop classification) so the entry stop we report
        # below lands on genuine user code, one statement past
        # wait_for_client() -- the same auto-step-out-to-caller pattern
        # used by the live breakpoint hook.
        try:
            await self.session.command("n")
        except PerlProtocolError as e:
            self.send_error(request, f"attach step-out failed: {e} [{e.tail}]")
            return
        self._stop_on_entry = True
        self._start_request = request
        self.send_event("initialized")

    async def _on_configurationDone(self, request: Request) -> None:
        self.send_response(request)
        if self._start_request is not None:
            self.send_response(self._start_request)
            self._start_request = None
            if self._stop_on_entry:
                await self._emit_stopped("entry")
            else:
                # Mirrors _on_continue's guard: always settle out of the
                # compile phase before the real "c". Beyond the deferred-
                # breakpoint race (Background 8), a bare "c" from the
                # compile-phase entry prompt only clears single-step for
                # the current BEGIN/require frame -- perl5db restores it
                # from its frame stack when that frame returns, so the
                # debuggee traps again on the next compile-time statement
                # and the spurious "step" stop opens the TUI in run mode.
                hit = await self._settle_to_run_phase()
                if hit is not None:
                    await self._report_pending_hit(hit)
                    return
                self.session.resume("c")

    async def _emit_stopped(self, reason: str) -> None:
        self.refs.reset()
        self.current_stop = await self._location_after_settling()
        if self._pending_breakpoints:
            await self._flush_pending_if_run()
        self.send_event(
            "stopped",
            {"reason": reason, "threadId": 1, "allThreadsStopped": True},
        )

    async def _location_after_settling(self) -> dict | None:
        """`Devel::TdbHelper::location()`, auto-stepping past any stop
        that lands inside our own compile-phase shim.

        TdbCompile.pm's DB::DB wrapper self-uninstalls the instant
        ${^GLOBAL_PHASE} leaves 'START' (see that module's own header
        comment: leaving it installed with a `goto`-passthrough for the
        rest of the process's life was tried and reliably OOM'd perl).
        Self-uninstalling leaks exactly one stop at the
        reassignment/goto line, inside TdbCompile.pm itself, during
        that START->RUN transition. That stop is normally hidden by
        `_user_frames` in helpers.pl (which skips frames whose package
        is Devel::TdbCompile); this is the belt-and-suspenders backstop
        on the adapter side -- if a stray trap ever DOES surface here
        regardless, auto-step past it (the same auto-step-out-to-caller
        pattern _on_attach already uses for Devel::TdbRemote) rather
        than showing it as a user stop. Bounded so a persistent leak
        can't hang tdb -- it would instead fall through to the
        pre-existing "location() gave us nothing usable" handling.
        """
        loc = None
        for _ in range(5):
            try:
                loc = await self.session.helper("Devel::TdbHelper::location()")
            except PerlProtocolError as e:
                log.error("location() failed after stop: %s", e)
                return None
            file = loc.get("file") or ""
            # `shim` is helpers.pl's report that the PHYSICAL stop is
            # inside Devel::TdbCompile even though the file/line shown
            # belong to the caller (_user_frames hides shim frames, which
            # also hid them from the plain file-name check here -- the
            # self-uninstall leak then surfaced as a duplicate stop on
            # the first RUN-phase line). Falsy/absent on older
            # helpers.pl: degrades to the file-name check alone.
            if "TdbCompile.pm" not in file and not loc.get("shim"):
                return loc
            log.warning("stray stop inside TdbCompile.pm; stepping past it")
            try:
                await self.session.command("n")
            except PerlProtocolError as e:
                log.error("step-past-shim failed: %s", e)
                return None
        return loc

    async def _current_phase(self) -> str | None:
        """`${^GLOBAL_PHASE}` at the debuggee's current stop, or None if
        it can't be determined (e.g. an older debuggee-side helpers.pl
        without phase() -- degrade to "don't defer", matching
        pre-compile-phase behavior)."""
        if self.session is None:
            return None
        try:
            return (await self.session.helper("Devel::TdbHelper::phase()"))["phase"]
        except PerlProtocolError:
            return None

    async def _settle_to_run_phase(self) -> dict | None:
        """Advance a debuggee that's still in perl's compile phase up to
        the RUN-phase transition before letting a real `c` (continue)
        run, then flush breakpoints deferred while compiling (Background
        8). A bare `c` clears perl5db's single-step flag; with no real
        breakpoint armed yet in the target file (that's the whole point
        of the deferral -- calling `b`/breakable() on a partially
        compiled file risks poisoning its line table, see helpers.pl's
        breakable()), it would run straight to program completion,
        racing past wherever those breakpoints should have fired.
        Single-stepping (`n`) instead keeps landing on the shim's own
        traps (compile-time statements in the target file, then the
        RUN-phase transition itself) without that risk. ${^GLOBAL_PHASE}
        flips to RUN only once the WHOLE top-level file has finished
        compiling -- even the parts execution hasn't reached yet -- so
        by the time this loop exits it's safe to apply the deferred
        breakpoints for real. Bounded so a pathological compile phase
        can't hang tdb.

        A pending breakpoint that lands on a compile-time statement (a
        line inside a BEGIN block, for instance) will never be revisited
        once RUN phase applies it for real -- that BEGIN block has
        already finished executing by then. So this loop also checks
        each new position it steps to against the pending breakpoints
        for that file and stops early on a match, returning a dict
        describing the hit (None if it settles into RUN phase without
        one). Order matters: each iteration steps FIRST and only THEN
        checks the new location -- on re-entry after a hit the debuggee
        is still sitting on the line that fired, so checking before
        stepping would re-fire the same line forever. The position the
        loop starts from (whatever line the caller was already stopped
        at) is therefore never itself tested.
        """
        for _ in range(1000):
            phase = await self._current_phase()
            if phase != "START":
                break
            try:
                await self.session.command("n")
            except PerlProtocolError as e:
                log.error("compile-phase settle step failed: %s", e)
                break
            hit = await self._check_compile_phase_hit()
            if hit is not None:
                return hit
        await self._flush_pending_if_run()
        return None

    async def _check_compile_phase_hit(self) -> dict | None:
        """After a compile-phase settle step, check whether the debuggee's
        new location exactly matches a pending breakpoint's line in the
        same file. EXACT line equality only -- no snap-forward ("first
        statement at or after the requested line"): a compile-time
        statement appearing LATER in the file (e.g. a BEGIN block near
        the bottom) must never falsely fire a breakpoint meant for an
        earlier runtime line. `hitCondition` is not supported here (only
        `condition`, evaluated fail-closed on error -- see
        _eval_condition); a plain miss (wrong line, or a condition that
        isn't true) returns None so the settle loop keeps stepping.
        """
        loc = await self._location_after_settling()
        if loc is None:
            return None
        remote_path = loc.get("file")
        entry = self._pending_breakpoints.get(remote_path)
        if entry is None:
            return None
        local_path, wanted = entry
        line = loc.get("line")
        for bp in wanted:
            if bp.get("line") != line:
                continue
            cond = bp.get("condition")
            if cond and not await self._eval_condition(cond):
                continue
            return {
                "loc": loc,
                "remote_path": remote_path,
                "local_path": local_path,
                "bp": bp,
            }
        return None

    async def _eval_condition(self, cond: str) -> bool:
        """Evaluate a pending breakpoint's condition at the debuggee's
        current compile-phase stop. Mirrors perl5db's own conditional-
        breakpoint semantics (_DB__determine_if_we_should_break's
        `$DB::signal |= 1 if do {$stop}`): a condition that dies aborts
        the `if` before the assignment runs, so the breakpoint does NOT
        fire -- fail-CLOSED. Confirmed empirically against a live
        session: a real runtime `b file:line die 'x'` breakpoint never
        fires and the debuggee runs to completion. helper() raises
        PerlProtocolError on any payload carrying an "error" key
        (cond_result always includes one when the eval died), so that
        exception is exactly the die-during-condition case -- treat it
        the same as a false condition.
        """
        cmd = (
            ";{ local $@; my $r = eval { (" + cond + ") ? 1 : 0 }; "
            "Devel::TdbHelper::cond_result($r, $@) }"
        )
        try:
            payload = await self.session.helper(cmd)
        except PerlProtocolError:
            return False
        return bool(payload.get("ok"))

    async def _report_pending_hit(self, hit: dict) -> None:
        """Report an early stop from _settle_to_run_phase the same way a
        real breakpoint stop is reported: a `breakpoint` "changed" event
        (verified:true, same shape _flush_pending_if_run already sends)
        followed by a `stopped` event. Mirrors _emit_stopped's
        refs/current_stop handling so the stack/variables views work at
        this compile-phase stop. The pending entry is deliberately left
        in self._pending_breakpoints (requirement 7): a BEGIN line never
        executes again, so the eventual RUN-phase flush applying it for
        real is harmless (it just arms a breakpoint that can never hit)
        and _on_breakpoint_event's verified-state update is idempotent.
        """
        self.refs.reset()
        self.current_stop = hit["loc"]
        bp = hit["bp"]
        self.send_event(
            "breakpoint",
            {
                "reason": "changed",
                "breakpoint": {
                    "verified": True,
                    "line": bp["line"],
                    "source": {"path": hit["local_path"]},
                },
            },
        )
        self.send_event(
            "stopped",
            {"reason": "breakpoint", "threadId": 1, "allThreadsStopped": True},
        )

    async def _flush_pending_if_run(self) -> None:
        if not self._pending_breakpoints or self.session is None:
            return
        phase = await self._current_phase()
        if phase == "START":
            return
        pending, self._pending_breakpoints = self._pending_breakpoints, {}
        for remote_path, (local_path, wanted) in pending.items():
            results = await self._apply_breakpoints(remote_path, wanted)
            for r in results:
                self.send_event(
                    "breakpoint",
                    {
                        "reason": "changed",
                        "breakpoint": {
                            "verified": r["verified"],
                            "line": r["line"],
                            "source": {"path": local_path},
                        },
                    },
                )

    def _forward_output(self, text: str, category: str) -> None:
        if category == "__eof__":
            if self._eof_terminated:
                # _classify_and_emit_stop's "?" branch already force-quit
                # perl5db (via `q`) and emitted terminated/exited itself --
                # THIS eof is that quit closing the socket, not a fresh
                # unsolicited termination. Emitting again would double-fire
                # both events.
                self._eof_terminated = False
                return
            # Capture the session reference now: this is an unsolicited
            # socket close (e.g. the debuggee hard-crashed), so nothing
            # guarantees `self.session` is still set by the time the
            # spawned task below actually runs.
            session = self.session
            self._eof_task = asyncio.ensure_future(self._emit_termination(session))
            return
        self.send_event("output", {"category": category, "output": text})

    async def _emit_termination(self, session: PerlSession | None) -> None:
        exit_code = await session.wait_exit_code() if session is not None else 0
        self.send_event("terminated")
        self.send_event("exited", {"exitCode": exit_code})

    def _on_unsolicited_stop(self) -> None:
        self._classifying = True
        # Strong ref: asyncio only holds a weak reference to a bare
        # ensure_future() task, so it can be garbage-collected mid-flight
        # if nothing else keeps it alive (see repo pitfall notes).
        self._classify_task = asyncio.ensure_future(self._classify_and_emit_stop())

    async def _classify_and_emit_stop(self) -> None:
        self.refs.reset()
        try:
            if self.session is None:
                return
            loc = await self._location_after_settling()
            loc = await self._coalesce_compile_phase_step(loc)
            if loc is None or loc.get("file") == "?":
                # perl5db's "ended" state: the debuggee ran to completion and
                # perl5db parked at a live prompt without closing the socket
                # (no user frames left, so location() can't report one). The
                # underlying child process is genuinely still ALIVE here,
                # blocked at that prompt (confirmed by probing real
                # perl5db: `o inhibit_exit` defaults to on) -- it needs an
                # explicit `q` before it actually exits and a real return
                # code becomes available.
                self.current_stop = None
                session = self.session
                # Finding 1 (task-4 review): `loc is None` means location()
                # RAISED (timeout, malformed/missing JSON, mid-flight EOF)
                # -- an inconclusive/error state, not proof the debuggee
                # ended. It can just as easily mean the debuggee is
                # legitimately stopped at a real breakpoint/pause, so it
                # must NOT trigger `q` (that would kill a live session).
                # Only a CONFIRMED "?" location does. `loc is None` keeps
                # reporting termination (unchanged pre-existing behavior)
                # with exit code 0 -- we genuinely don't know it.
                #
                # NOTE for a later task: any future compile-phase stop must
                # report a real file from location(), never "?" -- otherwise
                # it would trip this same "ended" branch.
                #
                # session.pid is None in externalTerminal launch mode too
                # (self._process is never set there -- see session.py) even
                # though we still own the debuggee's lifecycle via
                # debuggee_pid, so both must be checked here or a terminal
                # launch would be misclassified as attach mode: `q` never
                # sent, exit code always reported as 0.
                if loc is not None and (
                    session.pid is not None
                    or getattr(session, "debuggee_pid", None) is not None
                ):
                    # Owned child (launch mode, in-process or terminal) AND
                    # a confirmed "?" location.
                    # `q` closes the debug socket, which the read loop will
                    # also observe as EOF and route through
                    # _forward_output's "__eof__" branch -- set
                    # _eof_terminated first (before any await lets that
                    # run) so it's a no-op there instead of a second
                    # terminated/exited pair.
                    self._eof_terminated = True
                    try:
                        await session.command("q")
                    except PerlProtocolError:
                        pass  # expected: the connection closes, no prompt follows
                    if not session.eof:
                        # Finding 2 (task-4 review): `q` did NOT actually
                        # close the connection (timed out waiting for a
                        # prompt, or perl5db replied without dying) -- no
                        # __eof__ is coming to consume the flag. Clear it
                        # now so it can't dangle and silently swallow the
                        # NEXT, genuinely unrelated __eof__ termination.
                        self._eof_terminated = False
                    exit_code = await session.wait_exit_code()
                else:
                    # Attach mode: no owned child, and forcing `q` would
                    # kill the user's live remote process out from under
                    # them -- never do that here.
                    exit_code = 0
                self.send_event("terminated")
                self.send_event("exited", {"exitCode": exit_code})
                return
            self.current_stop = loc
            if self._pending_breakpoints:
                await self._flush_pending_if_run()
            had_pause_pending = self._pause_pending
            self._pause_pending = False
            # loc["file"] comes straight from perl5db's location() -- it's
            # already in REMOTE-path space, the same space breakpoint_lines
            # is keyed in (see __init__ comment), so no translation needed
            # here.
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

    async def _coalesce_compile_phase_step(self, loc: dict | None) -> dict | None:
        """Collapse the several same-line stops one source statement
        generates during perl's compile phase into a single user-visible
        step.

        With the Devel::TdbCompile shim armed, perl5db traps every
        compile-time internal operation of the target file: a single
        `use` statement expands into loading the module and invoking its
        import logic, so one displayed line yields several traps in a
        row, all reporting the same file and line. Reporting each as its
        own DAP stop meant one user `next` appeared to do nothing --
        advancing past a `use` line took several presses.

        So: when classifying the stop that answers a user step command
        (n/s/r -- self._step_origin records where it started), and the
        debuggee is still in phase START but sitting on the SAME file
        and line it started from, keep issuing internal `n` commands
        until the line changes. Guards, in order:

        - Never during RUN phase: a one-line loop body legitimately
          re-stops on the same line there, and skipping its iterations
          would be wrong. Compile-phase-only, exactly as recommended in
          the root-cause analysis (a one-line loop INSIDE a BEGIN block
          would still be coalesced -- accepted trade-off).
        - Never past a breakpoint or pending pause: those stops carry
          meaning even on an unchanged line, so they surface as-is.
        - Bounded, so a malformed or debugger-specific stop sequence
          can't spin forever; on hitting the bound the current location
          is reported as a normal stop (visible, not hung).

        Consumes _step_origin (one coalesce per user step) and returns
        the final location the classification should report.
        """
        origin, self._step_origin = self._step_origin, None
        if origin is None or loc is None or self.session is None:
            return loc
        for _ in range(50):
            if (loc.get("file"), loc.get("line")) != origin:
                break
            if self._pause_pending:
                break
            if loc.get("line") in self.breakpoint_lines.get(loc.get("file"), set()):
                break
            if await self._current_phase() != "START":
                break
            try:
                await self.session.command("n")
            except PerlProtocolError as e:
                log.error("compile-phase step coalescing failed: %s", e)
                break
            loc = await self._location_after_settling()
            if loc is None:
                break
        return loc

    async def _on_threads(self, request: Request) -> None:
        self.send_response(request, {"threads": [{"id": 1, "name": "main"}]})

    def _not_ready(self) -> str | None:
        """Error string when data/control requests can't be safely served.

        Covers three cases: no active session, debuggee still running, or a
        stop that's mid-classification (self._classifying) -- during
        classification perl5db is still being probed by
        _classify_and_emit_stop, and issuing further DB commands
        concurrently could race it. Returns None when it's safe to proceed.
        """
        if self.session is None:
            return "no session"
        if not self.session.stopped or self._classifying:
            return "debuggee is not stopped"
        return None

    async def _resume(self, request: Request, cmd: str) -> None:
        reason = self._not_ready()
        if reason is not None:
            self.send_error(request, reason)
            return
        # Step commands (n/s/r) remember where they started so
        # _classify_and_emit_stop can coalesce compile-phase stops that
        # land back on this same line; `c` must never coalesce (a
        # breakpoint in a one-line loop legitimately re-fires on the
        # same line), so it clears the origin instead.
        if cmd != "c" and self.current_stop is not None:
            self._step_origin = (
                self.current_stop.get("file"),
                self.current_stop.get("line"),
            )
        else:
            self._step_origin = None
        self.current_stop = None
        self.send_response(request)
        self.send_event("continued", {"threadId": 1, "allThreadsContinued": True})
        self.session.resume(cmd)

    async def _on_continue(self, request: Request) -> None:
        # Only `continue` needs the settle-past-compile-phase guard
        # (_settle_to_run_phase): it's the one resume command with no
        # bound on how far it runs, so it's the one that can race past
        # breakpoints deferred while perl was still compiling (Background
        # 8). next/stepIn/stepOut always land on the very next statement
        # -- they can't skip over the deferred file's own later lines --
        # so a stop reaching RUN phase there is caught by
        # _classify_and_emit_stop's reactive flush instead.
        #
        # Settle even with NO pending breakpoints: a bare "c" from a
        # compile-phase stop only clears single-step for the current
        # BEGIN/require frame -- perl5db restores it from its frame
        # stack when that frame returns, so `c` would spuriously
        # re-stop on the next compile-time statement instead of
        # running. (In RUN phase the settle loop is a single phase()
        # probe and falls straight through.)
        if self._not_ready() is None:
            hit = await self._settle_to_run_phase()
            if hit is not None:
                self.current_stop = None
                self.send_response(request)
                self.send_event(
                    "continued", {"threadId": 1, "allThreadsContinued": True}
                )
                await self._report_pending_hit(hit)
                return
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
        reason = self._not_ready()
        if reason is not None:
            self.send_error(request, reason)
            return
        path = request.arguments.get("source", {}).get("path", "")
        remote_path = self._to_remote(path)
        payload = await self.session.helper(
            f"Devel::TdbHelper::source({self._perl_str(remote_path)})"
        )
        if not payload.get("text"):
            self.send_error(request, f"no compiled source for {path}")
            return
        self.send_response(request, {"content": payload["text"]})

    async def _on_setBreakpoints(self, request: Request) -> None:
        reason = self._not_ready()
        if reason is not None:
            self.send_error(request, reason)
            return
        path = request.arguments.get("source", {}).get("path", "")
        remote_path = self._to_remote(path)
        wanted = request.arguments.get("breakpoints", [])
        # Background 8 / requirement 5: while perl is still compiling
        # `remote_path` (phase 'START'), its line table is only
        # partially populated -- breakable() would report a partial
        # table and `b file:line` answers "not breakable" for lines
        # after wherever compilation has reached so far, AND calling
        # breakable() on a not-yet-fully-compiled file risks the
        # line-table-poisoning hazard documented on that helper (merely
        # dereferencing its perl line-table array autovivifies it).
        # Defer instead of ever calling either: store the request,
        # answer unverified now, and apply for real once
        # phase() reports 'RUN' (the whole top-level file has finished
        # compiling by then, even lines execution hasn't reached yet --
        # see _settle_to_run_phase). Attach mode never sees phase
        # 'START' here (the debuggee is already running by the time tdb
        # attaches), so this is a no-op there.
        if await self._current_phase() == "START":
            self._pending_breakpoints[remote_path] = (path, wanted)
            # DEFERRED_VERIFICATION_MESSAGE tells controller.py's
            # _warn_unbound_breakpoints this verified:false is a deferral,
            # not a genuine bind failure, so it doesn't fire its
            # missing-debug-info warning here; _flush_pending_if_run sends
            # the real answer as a `breakpoint` "changed" event once the
            # compile phase settles (see requirement 5).
            results = [
                {
                    "verified": False,
                    "line": bp["line"],
                    "message": DEFERRED_VERIFICATION_MESSAGE,
                }
                for bp in wanted
            ]
            self.send_response(request, {"breakpoints": results})
            return
        # A later, non-deferred request for this same file supersedes
        # any earlier deferred one still waiting to be flushed.
        self._pending_breakpoints.pop(remote_path, None)
        results = await self._apply_breakpoints(remote_path, wanted)
        self.send_response(request, {"breakpoints": results})

    async def _apply_breakpoints(
        self, remote_path: str, wanted: list[dict]
    ) -> list[dict]:
        """The real `b`/`B` exchange with perl5db -- safe only once
        `remote_path` is known to be fully compiled (see
        _on_setBreakpoints and _flush_pending_if_run, its two callers)."""
        # breakpoint_lines is keyed by REMOTE path (see __init__ comment).
        old_lines = self.breakpoint_lines.get(remote_path, set())
        if old_lines:
            # `B <line>` is scoped to perl5db's CURRENT file, which is
            # whichever frame the debugger last stopped in -- not
            # necessarily `remote_path`. Switch to `remote_path` first so
            # deletions land in the right per-file breakpoint table. If
            # perl5db never loaded `remote_path` (nothing to delete -- no
            # breakpoint could have been set there in the first place),
            # `f` prints "No file matching '<path>' is loaded." and we
            # skip the deletions rather than delete from whatever file
            # `f` left current.
            f_events = await self.session.command(f"f {remote_path}")
            if not any(e[0] == "text" and "No file matching" in e[1] for e in f_events):
                for old_line in old_lines:
                    await self.session.command(f"B {old_line}")
        try:
            # Devel::TdbHelper::breakable() guards internally against
            # files perl hasn't compiled yet (see helpers.pl) so this is
            # safe to call speculatively even if `remote_path` isn't
            # loaded -- it reports {"lines": [], "unloaded": 1} without
            # touching perl5db's per-file line table, instead of the
            # empty-set fallback below silently masking a real protocol
            # error.
            breakable = set(
                (
                    await self.session.helper(
                        f"Devel::TdbHelper::breakable({self._perl_str(remote_path)})"
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
            cmd = f"b {remote_path}:{target}" + (f" {cond}" if cond else "")
            events = await self.session.command(cmd)
            failed = any(e[0] == "text" and "not breakable" in e[1] for e in events)
            if failed:
                results.append({"verified": False, "line": line})
            else:
                results.append({"verified": True, "line": target})
                actual_lines.add(target)
        self.breakpoint_lines[remote_path] = actual_lines
        return results

    @staticmethod
    def _perl_str(s: str) -> str:
        return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"

    async def _on_stackTrace(self, request: Request) -> None:
        reason = self._not_ready()
        if reason is not None:
            self.send_error(request, reason)
            return
        payload = await self.session.helper("Devel::TdbHelper::stack()")
        frames = []
        for i, f in enumerate(payload["frames"]):
            frames.append(
                {
                    "id": i,
                    "name": f.get("sub") or "main",
                    "line": f["line"],
                    "column": 1,
                    "source": {"path": self._to_local(f["file"])},
                }
            )
        self.send_response(request, {"stackFrames": frames, "totalFrames": len(frames)})

    async def _on_scopes(self, request: Request) -> None:
        reason = self._not_ready()
        if reason is not None:
            self.send_error(request, reason)
            return
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
        reason = self._not_ready()
        if reason is not None:
            self.send_error(request, reason)
            return
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
        reason = self._not_ready()
        if reason is not None:
            self.send_error(request, reason)
            return
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

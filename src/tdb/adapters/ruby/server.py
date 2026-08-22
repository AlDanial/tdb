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


class RubyDapServer:
    """Store-and-forward proxy; see module docstring."""

    def __init__(self, reader: asyncio.StreamReader, writer: Any) -> None:
        self._reader = reader
        self._writer = writer
        self._seqs = SeqTranslator()
        self._done = asyncio.Event()
        self._proc: asyncio.subprocess.Process | None = None
        self._rdbg_writer: asyncio.StreamWriter | None = None
        self._transport: _Transport | None = None
        self._client_init_args: dict = {}
        self._client_supports_run_in_terminal = False
        self._stop_on_entry = True
        self._entry_stop_pending = False
        self._start_client_seq: int | None = None
        self._launched = False
        self._sent_exited = False
        self._sent_terminated = False
        self._reverse = ReverseRequester(self._write_client, self._seqs.next_client_seq)
        # Last thread rdbg reported stopped — used to default `frameId`
        # on evaluate/completions requests (see _default_frame_id): rdbg's
        # DAP evaluate hard-fails ("can't evaluate") without a frameId,
        # unlike debugpy, which treats it as optional.
        self._last_stopped_thread_id: int | None = None
        # Proxy-originated rdbg requests awaiting a reply (rdbg-side seq
        # -> future), e.g. the synthetic stackTrace above. Distinct from
        # _seqs' client<->rdbg maps: these have no client-side seq at all.
        self._proxy_requests: dict[int, asyncio.Future] = {}
        # Strong refs: asyncio only weakly references bare tasks (repo
        # pitfall) — a GC'd pump silently loses program output.
        self._tasks: set[asyncio.Future] = set()
        self._pump_tasks: list[asyncio.Future] = []
        self._launch_task: asyncio.Future | None = None
        self.handlers: dict[str, Callable[[dict], Awaitable[None]]] = {}
        for name in dir(self):
            if name.startswith("_on_"):
                self.handlers[name[4:]] = getattr(self, name)

    # ---- plumbing ----
    def _write_client(self, msg: dict) -> None:
        self._writer.write(encode_message(msg))

    def _write_rdbg(self, msg: dict) -> None:
        if self._rdbg_writer is not None:
            self._rdbg_writer.write(encode_message(msg))

    def _spawn_task(self, coro) -> asyncio.Future:
        t = asyncio.ensure_future(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    def send_response(self, request: dict, body: dict | None = None) -> None:
        msg: dict = {
            "seq": self._seqs.next_client_seq(),
            "type": "response",
            "request_seq": request["seq"],
            "command": request["command"],
            "success": True,
        }
        if body:
            msg["body"] = body
        self._write_client(msg)

    def send_error(self, request: dict, message: str) -> None:
        self._write_client(
            {
                "seq": self._seqs.next_client_seq(),
                "type": "response",
                "request_seq": request["seq"],
                "command": request["command"],
                "success": False,
                "message": message,
            }
        )

    def send_event(self, event: str, body: dict | None = None) -> None:
        msg: dict = {
            "seq": self._seqs.next_client_seq(),
            "type": "event",
            "event": event,
        }
        if body:
            msg["body"] = body
        self._write_client(msg)

    # ---- main loop (client stdio side) ----
    async def run(self) -> None:
        try:
            while not self._done.is_set():
                try:
                    msg = await read_message(self._reader)
                except (ConnectionError, asyncio.IncompleteReadError, EOFError):
                    break
                await self._dispatch_client_message(msg)
                await self._writer.drain()
        finally:
            await self._cancel_launch_task()
            await self._ensure_rdbg_dead()
            if self._transport is not None:
                self._transport.cleanup()
            await self._writer.drain()

    async def _dispatch_client_message(self, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == "response":
            if self._reverse.route(parse_message(msg)):
                return
            fwd = self._seqs.client_response_to_rdbg(msg)
            if fwd is not None:
                self._write_rdbg(fwd)
            return
        if mtype != "request":
            return
        handler = self.handlers.get(msg["command"])
        if handler is not None:
            try:
                await handler(msg)
            except Exception as e:
                log.exception("handler %s failed", msg["command"])
                self.send_error(msg, str(e))
            return
        if self._rdbg_writer is None:
            self.send_error(msg, "no debug session")
            return
        self._write_rdbg(self._seqs.client_request_to_rdbg(msg))

    # ---- rdbg socket side ----
    async def _pump_rdbg(self, reader: asyncio.StreamReader) -> None:
        while True:
            try:
                msg = await read_message(reader)
            except (ConnectionError, asyncio.IncompleteReadError, EOFError):
                return
            mtype = msg.get("type")
            if mtype == "event":
                if self._note_and_filter_event(msg):
                    continue
                self._write_client(self._seqs.rdbg_event_to_client(msg))
            elif mtype == "response":
                pending = self._proxy_requests.pop(msg.get("request_seq", -1), None)
                if pending is not None:
                    if not pending.done():
                        pending.set_result(msg)
                    continue
                out = self._seqs.rdbg_response_to_client(msg)
                if out is None:
                    continue  # reply to a proxy-originated request
                if out["request_seq"] == self._start_client_seq:
                    # the client sent `launch`; rdbg answered the
                    # translated `attach` — restamp the command so the
                    # client's launch future matches.
                    out["command"] = "launch"
                self._write_client(out)
            elif mtype == "request":
                self._write_client(self._seqs.rdbg_request_to_client(msg))
            await self._writer.drain()

    def _note_and_filter_event(self, msg: dict) -> bool:
        """Track exit/stop state; True -> swallow the event."""
        event = msg.get("event")
        body = msg.get("body") or {}
        if event == "output":
            if body.get("category") == "console" and str(
                body.get("output", "")
            ).startswith(_REPL_NOTICE):
                return True
        elif event == "stopped":
            self._last_stopped_thread_id = body.get("threadId")
            if self._entry_stop_pending:
                # rdbg reports the post-configurationDone entry stop as
                # "pause"; tdb (like debugpy) expects "entry".
                self._entry_stop_pending = False
                body["reason"] = "entry"
                msg["body"] = body
        elif event == "exited":
            self._sent_exited = True
        elif event == "terminated":
            self._sent_terminated = True
        return False

    async def _pump_output(self, stream: asyncio.StreamReader, category: str) -> None:
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace")
            if category == "stderr" and text.startswith(_BANNER_PREFIX):
                continue
            self.send_event("output", {"category": category, "output": text})
            await self._writer.drain()

    async def _watch_exit(self) -> None:
        assert self._proc is not None
        code = await self._proc.wait()
        # let the pipe pumps drain the output tail before exit events
        if self._pump_tasks:
            await asyncio.wait(self._pump_tasks, timeout=2.0)
        if not self._launched:
            return  # pre-handshake death is reported via the launch error
        if not self._sent_exited:
            self.send_event("exited", {"exitCode": code})
        if not self._sent_terminated:
            self.send_event("terminated")
        await self._writer.drain()

    # ---- lifecycle handlers ----
    async def _on_initialize(self, request: dict) -> None:
        self._client_init_args = dict(request.get("arguments") or {})
        self._client_supports_run_in_terminal = bool(
            self._client_init_args.get("supportsRunInTerminalRequest")
        )
        self.send_response(request, CAPABILITIES)

    async def _on_launch(self, request: dict) -> None:
        args = request.get("arguments") or {}
        program = args.get("program", "")
        if not os.path.isfile(program):
            self.send_error(request, f"program not found: {program}")
            return
        rdbg = args.get("rdbg") or shutil.which("rdbg")
        if rdbg is None:
            self.send_error(request, RDBG_HINT)
            return
        try:
            version = await _rdbg_version(rdbg)
        except (OSError, RuntimeError) as e:
            self.send_error(request, f"cannot run {rdbg!r}: {e} — {RDBG_HINT}")
            return
        if version < MIN_DEBUG_GEM:
            self.send_error(
                request,
                f"debug gem {version[0]}.{version[1]} is too old — tdb "
                f"needs >= {MIN_DEBUG_GEM[0]}.{MIN_DEBUG_GEM[1]} "
                f"(`gem install debug`)",
            )
            return
        self._stop_on_entry = bool(args.get("stopOnEntry", True))
        self._transport = pick_transport()
        cmd = [
            rdbg,
            "--open",
            *self._transport.rdbg_args,
            "--",
            program,
            *[str(a) for a in (args.get("args") or [])],
        ]
        if args.get("console") == "externalTerminal":
            if not self._client_supports_run_in_terminal:
                self.send_error(
                    request,
                    "externalTerminal launch requires a client that "
                    "supports the runInTerminal reverse request",
                )
                return
            # session-launch awaits the runInTerminal reply, which only
            # run()'s read loop can route — but run() is what's calling
            # this handler and it awaits handlers inline. Awaiting here
            # would deadlock; run the rest as a background task (strong
            # ref, per the repo's task-GC pitfall) so run() goes back to
            # reading. Same shape as the bash server's _on_launch.
            self._launch_task = asyncio.ensure_future(
                self._finish_launch(request, cmd, args, terminal=True)
            )
            return
        await self._finish_launch(request, cmd, args, terminal=False)

    async def _finish_launch(
        self, request: dict, cmd: list[str], args: dict, *, terminal: bool
    ) -> None:
        cwd = args.get("cwd") or os.getcwd()
        env = {**os.environ, **(args.get("env") or {})}
        try:
            if terminal:
                await self._reverse.request(
                    "runInTerminal",
                    {
                        "kind": "external",
                        "title": "tdb ruby debuggee",
                        "cwd": cwd,
                        "args": cmd,
                        "env": args.get("env") or {},
                    },
                )
            else:
                popen_kwargs: dict[str, Any] = {}
                if os.name == "nt":
                    # Ctrl-C isolation, same as the perl/bash spawn path
                    popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    popen_kwargs["start_new_session"] = True
                # stdin=DEVNULL: left unset, rdbg would inherit *our*
                # stdin — the pipe the client is actively writing DAP
                # requests into. Under rapid back-to-back launches that
                # race let rdbg's startup occasionally consume/contend
                # for bytes never meant for it, wedging its DAP socket
                # thread before it ever answered `initialize` (client
                # then hung ~30s on the "initialized" event).
                self._proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=cwd,
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **popen_kwargs,
                )
            reader, writer = await self._connect_with_retry()
        except asyncio.CancelledError:
            raise  # disconnect/terminate cancelling us — they clean up
        except Exception as e:
            detail = await self._collect_early_stderr()
            await self._ensure_rdbg_dead()
            if not isinstance(
                e, (OSError, TimeoutError, RuntimeError, ReverseRequestError)
            ):
                log.exception("ruby launch failed unexpectedly")
            self.send_error(request, f"{e}\n{detail}".strip())
            await self._writer.drain()
            return
        self._rdbg_writer = writer
        self._launched = True
        self._entry_stop_pending = self._stop_on_entry
        self._start_client_seq = request["seq"]
        rdbg_pump_task = self._spawn_task(self._pump_rdbg(reader))
        if self._proc is not None:
            # _watch_exit's synthesize decision reads _sent_exited/
            # _sent_terminated, which only the rdbg-socket pump sets — it
            # must be in this wait set too, not just the stdout/stderr
            # pumps, or a child-exit callback that wins the race against
            # the socket-readable callback can see stale flags and emit a
            # duplicate/misreported exited+terminated pair. Spawn (above)
            # and register it here BEFORE _watch_exit starts so the list
            # it reads is already complete.
            self._pump_tasks = [
                self._spawn_task(self._pump_output(self._proc.stdout, "stdout")),
                self._spawn_task(self._pump_output(self._proc.stderr, "stderr")),
                rdbg_pump_task,
            ]
            self._spawn_task(self._watch_exit())
        # rdbg needs its own initialize first. Proxy-originated (no
        # client mapping) -> its response is swallowed by the translator;
        # rdbg's `initialized` event passes through to the client and
        # triggers its setBreakpoints/configurationDone sequence.
        self._write_rdbg(
            {
                "seq": self._seqs.next_rdbg_seq(),
                "type": "request",
                "command": "initialize",
                "arguments": dict(self._client_init_args),
            }
        )
        # Forward the client's launch AS an rdbg `attach` (see module
        # docstring): nonstop honors stopOnEntry, localfs=true because
        # rdbg runs on this same machine.
        fwd = self._seqs.client_request_to_rdbg(request)
        fwd["command"] = "attach"
        fwd["arguments"] = {
            "localfs": True,
            "nonstop": not self._stop_on_entry,
        }
        self._write_rdbg(fwd)
        await self._writer.drain()

    async def _connect_with_retry(
        self, timeout: float = 30.0
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        assert self._transport is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            try:
                return await self._transport.connect()
            except (ConnectionError, FileNotFoundError, OSError):
                if self._proc is not None and self._proc.returncode is not None:
                    raise RuntimeError(
                        f"rdbg exited with code {self._proc.returncode} "
                        f"before accepting a connection"
                    )
                if loop.time() > deadline:
                    raise TimeoutError("timed out waiting for rdbg's DAP socket")
                await asyncio.sleep(0.1)

    async def _collect_early_stderr(self) -> str:
        """Salvage rdbg's stderr for a failed-launch message (pumps have
        not started yet on this path)."""
        if self._proc is None or self._proc.stderr is None:
            return ""
        try:
            data = await asyncio.wait_for(self._proc.stderr.read(4096), 0.5)
        except (asyncio.TimeoutError, OSError):
            return ""
        lines = [
            ln
            for ln in data.decode("utf-8", "replace").splitlines()
            if not ln.startswith(_BANNER_PREFIX)
        ]
        return "\n".join(lines)

    # ---- request default-framing (evaluate/completions) ----
    async def _rdbg_request(
        self, command: str, arguments: dict, timeout: float = 5.0
    ) -> dict | None:
        """Issue a proxy-originated request to rdbg and await its reply.

        Distinct from the client<->rdbg forwarding path: this has no
        client-side seq to restamp back to, so the response is routed
        through `_proxy_requests` instead of `SeqTranslator`.
        """
        if self._rdbg_writer is None:
            return None
        seq = self._seqs.next_rdbg_seq()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._proxy_requests[seq] = fut
        self._write_rdbg(
            {"seq": seq, "type": "request", "command": command, "arguments": arguments}
        )
        try:
            await self._rdbg_writer.drain()
            return await asyncio.wait_for(fut, timeout)
        except (asyncio.TimeoutError, ConnectionError, OSError):
            return None
        finally:
            self._proxy_requests.pop(seq, None)

    async def _default_frame_id(self) -> int | None:
        """Top frameId of the last-stopped thread, fetched on demand.

        rdbg's DAP evaluate/completions hard-fail without a frameId
        ("can't evaluate") even for frame-independent expressions —
        debugpy treats frameId as optional and defaults to the topmost
        frame of the current thread. This restores that parity so a
        client that (per the DAP spec) omits frameId still works.
        """
        if self._last_stopped_thread_id is None:
            return None
        resp = await self._rdbg_request(
            "stackTrace",
            {"threadId": self._last_stopped_thread_id, "startFrame": 0, "levels": 1},
        )
        if resp is None or not resp.get("success"):
            return None
        frames = (resp.get("body") or {}).get("stackFrames") or []
        return frames[0]["id"] if frames else None

    async def _forward_with_default_frame(self, request: dict) -> None:
        """Passthrough for evaluate/completions, filling in `frameId`
        when the client omitted it (see `_default_frame_id`)."""
        if self._rdbg_writer is None:
            self.send_error(request, "no debug session")
            return
        args = dict(request.get("arguments") or {})
        if "frameId" not in args:
            frame_id = await self._default_frame_id()
            if frame_id is not None:
                args["frameId"] = frame_id
                request = {**request, "arguments": args}
        self._write_rdbg(self._seqs.client_request_to_rdbg(request))

    async def _on_evaluate(self, request: dict) -> None:
        await self._forward_with_default_frame(request)

    async def _on_completions(self, request: dict) -> None:
        await self._forward_with_default_frame(request)

    # ---- teardown ----
    async def _cancel_launch_task(self) -> None:
        """Cancel an in-flight externalTerminal launch continuation before
        teardown (same rationale as the bash server's method of the same
        name: the continuation could assign session state after our
        checks already ran)."""
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

    def _kill_rdbg_group(self, sig_kill: bool = False) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        try:
            if os.name == "nt":
                proc.kill() if sig_kill else proc.terminate()
            else:
                os.killpg(proc.pid, signal.SIGKILL if sig_kill else signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass

    async def _ensure_rdbg_dead(self, grace: float = 2.0) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        self._kill_rdbg_group()
        try:
            await asyncio.wait_for(proc.wait(), grace)
        except asyncio.TimeoutError:
            self._kill_rdbg_group(sig_kill=True)
            await proc.wait()

    async def _on_disconnect(self, request: dict) -> None:
        await self._cancel_launch_task()
        if self._rdbg_writer is not None:
            # graceful first (kills a terminal-mode debuggee the proxy
            # has no process handle for); proxy-originated -> swallowed
            self._write_rdbg(
                {
                    "seq": self._seqs.next_rdbg_seq(),
                    "type": "request",
                    "command": "terminate",
                    "arguments": {},
                }
            )
        await self._ensure_rdbg_dead()
        self.send_response(request)
        self._done.set()

    async def _on_terminate(self, request: dict) -> None:
        await self._cancel_launch_task()
        if self._rdbg_writer is not None:
            self._write_rdbg(
                {
                    "seq": self._seqs.next_rdbg_seq(),
                    "type": "request",
                    "command": "terminate",
                    "arguments": {},
                }
            )
        await self._ensure_rdbg_dead()
        self.send_response(request)

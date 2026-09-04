"""DAP proxy between tdb (stdio) and PowerShell Editor Services (PSES).

PSES — the DAP server inside the VS Code PowerShell extension — runs
in-process in pwsh and, in debug-only named-pipe mode, serves DAP on a
UNIX socket (Windows: a named pipe) whose path it announces in a
session-details JSON file. tdb expects a stdio adapter it can spawn.
This module bridges the two:

  tdb --stdio--> PowerShellDapServer --socket--> pwsh Start-EditorServices.ps1
                                     <--stdout-- (script output)

Store-and-forward pipe with seq renumbering (tdb.adapters.seqs). PSES
quirks handled here (all probe-verified, see the design spec):

  initialize        answered from static CAPABILITIES (PSES isn't up yet)
  launch            spawns pwsh, waits for the session file, connects,
                    then forwards a launch of tdb_launch.ps1 (which runs
                    the user's script with `&` and prints an exit-code
                    sentinel — PSES never sends `exited`); every user arg
                    is single-quoted because PSES joins args unquoted
  env               set on the pwsh process (PSES ignores the launch field)
  stopOnEntry       PSES ignores it: emulated with a breakpoint on the
                    launcher's `& $Script` line plus a `stepIn`, which lands
                    on the user script's first executable statement (a line-1
                    breakpoint on the script itself binds to a function body
                    when line 1 is a `function`, so it cannot be used)
  pause             PSES reports the stop as "step": rewritten to "pause"
  evaluate          "repl" prints to stdout with an empty result: context
                    rewritten to "watch"
  terminate         unsupported by PSES: answered here by killing pwsh
  terminated        PSES sends it but pwsh lives on: the proxy kills pwsh,
                    then emits exited(code) + terminated in that order
  stdout            pumped into `output` events; the echoed prompt line
                    and the exit sentinel are dropped; ConciseView error
                    blocks are tagged "stderr" for the fatal-error modal
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable

from tdb import _timeouts
from tdb.adapters.powershell.locate import find_pses, find_pwsh
from tdb.adapters.powershell.output import (
    LAUNCHER,
    OutputClassifier,
    parse_exit_sentinel,
    quote_ps_arg,
)
from tdb.adapters.seqs import SeqTranslator
from tdb.dap.protocol import encode_message, read_message

log = logging.getLogger(__name__)

# What PSES 4.7 advertises, plus supportsTerminateRequest (the proxy
# implements terminate itself).
CAPABILITIES = {
    "supportsConfigurationDoneRequest": True,
    "supportsFunctionBreakpoints": True,
    "supportsConditionalBreakpoints": True,
    "supportsHitConditionalBreakpoints": True,
    "supportsSetVariable": True,
    "supportsDelayedStackTraceLoading": True,
    "supportsLogPoints": True,
    "supportsCancelRequest": True,
    "supportsTerminateRequest": True,
}

_LAUNCHER_CALL = "& $Script @ScriptArgs"


def _launcher_call_line(default: int = 12) -> int:
    """Line in tdb_launch.ps1 that invokes the user's script.

    stopOnEntry breaks here and steps in, which puts the client on the
    script's first executable statement whatever it is.
    """
    try:
        for i, line in enumerate(LAUNCHER.read_text().splitlines(), 1):
            if _LAUNCHER_CALL in line:
                return i
    except OSError:  # pragma: no cover - the launcher ships with the package
        pass
    return default


LAUNCHER_CALL_LINE = _launcher_call_line()

MIN_PWSH = (7, 0)
_SESSION_TIMEOUT_ENV = "TDB_PSES_SESSION_TIMEOUT"  # tests shorten the wait
_RESUME_COMMANDS = {"continue", "next", "stepIn", "stepOut"}


def build_pwsh_command(
    pwsh: str, pses_dir: Path, session_file: Path, log_dir: Path, pipe_name: str
) -> list[str]:
    from tdb import __version__

    return [
        pwsh,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(pses_dir / "Start-EditorServices.ps1"),
        "-HostName",
        "tdb",
        "-HostProfileId",
        "tdb",
        "-HostVersion",
        __version__,
        "-BundledModulesPath",
        str(pses_dir.parent),
        "-LogPath",
        str(log_dir),
        "-LogLevel",
        "None",
        "-SessionDetailsPath",
        str(session_file),
        "-DebugServiceOnly",
        "-DebugServicePipeName",
        pipe_name,
    ]


async def _connect_windows_pipe(
    name: str,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Windows named-pipe client. UNVERIFIED (no Windows CI yet)."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await loop.create_pipe_connection(lambda: protocol, name)  # type: ignore[attr-defined]
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    return reader, writer


async def connect_debug_service(
    details: dict,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """The one platform seam: UNIX socket on POSIX, named pipe on Windows."""
    name = details["debugServicePipeName"]
    if sys.platform == "win32":
        return await _connect_windows_pipe(name)
    return await asyncio.open_unix_connection(name)


def _make_pdeathsig() -> Callable[[], None] | None:
    """Linux only: make the child die with us even if we are SIGKILLed.

    `start_new_session` detaches pwsh from our process group, so an abrupt
    proxy death (SIGKILL, a crashed host) would otherwise orphan the PSES
    host. PR_SET_PDEATHSIG (1) asks the kernel to SIGKILL the child when its
    parent goes. No portable equivalent elsewhere; teardown still handles the
    orderly case.
    """
    if not sys.platform.startswith("linux"):
        return None
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
    except (OSError, AttributeError):  # pragma: no cover - exotic libc
        return None

    def _set() -> None:  # pragma: no cover - runs in the forked child
        try:
            libc.prctl(1, signal.SIGKILL)
        except Exception:
            pass

    return _set


_pdeathsig = _make_pdeathsig()


def _parse_version(text: str | None) -> tuple[int, int]:
    try:
        major, minor = (text or "").split(".")[:2]
        return int(major), int(minor)
    except ValueError:
        return (0, 0)


class PowerShellDapServer:
    """Store-and-forward proxy; see module docstring."""

    def __init__(self, reader: asyncio.StreamReader, writer: Any) -> None:
        self._reader = reader
        self._writer = writer
        self._seqs = SeqTranslator()
        self._done = asyncio.Event()
        self._proc: asyncio.subprocess.Process | None = None
        self._up_writer: asyncio.StreamWriter | None = None
        # Detached-but-not-yet-awaited socket writer (see _detach_upstream).
        self._dead_up_writer: asyncio.StreamWriter | None = None
        self._finishing = False  # a _finish_session task is in flight
        self._shutting_down = False  # run()'s teardown owns the kill now
        self._workdir: str | None = None
        self._client_init_args: dict = {}
        self._launched = False
        self._sent_exited = False
        self._sent_terminated = False
        self._terminated_seen = False  # PSES said the script ended
        self._exit_code: int | None = None  # from the launcher's sentinel
        self._classifier = OutputClassifier()
        # stopOnEntry emulation / stop-reason rewrites
        self._stop_on_entry = False
        self._program = ""  # the user's script, absolute (diagnostics)
        self._entry_pending = False  # armed: the launcher breakpoint is set
        self._entry_stepping = False  # stepped in: the next stop is the entry
        self._pause_pending = False
        # proxy-originated upstream requests awaiting a reply
        self._proxy_requests: dict[int, asyncio.Future] = {}
        # strong refs (asyncio keeps only weak refs to bare tasks)
        self._tasks: set[asyncio.Future] = set()
        self._pump_tasks: list[asyncio.Future] = []
        self._watch_exit_task: asyncio.Future | None = None
        self.handlers: dict[str, Callable[[dict], Awaitable[None]]] = {}
        for name in dir(self):
            if name.startswith("_on_"):
                self.handlers[name[4:]] = getattr(self, name)

    # ---- plumbing ----
    def _write_client(self, msg: dict) -> None:
        self._writer.write(encode_message(msg))

    def _write_up(self, msg: dict) -> None:
        if self._up_writer is not None:
            self._up_writer.write(encode_message(msg))

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

    def _up_send(self, command: str, arguments: dict) -> None:
        """Fire-and-forget proxy-originated request to PSES.

        No future is registered: the reply has no client mapping, so the
        seq translator drops it. Safe from inside `_pump_up` (the only
        reader of PSES's responses), unlike `_up_request`.
        """
        self._write_up(
            {
                "seq": self._seqs.next_upstream_seq(),
                "type": "request",
                "command": command,
                "arguments": arguments,
            }
        )

    async def _up_request(
        self, command: str, arguments: dict, timeout: float = 5.0
    ) -> dict | None:
        """Proxy-originated request to PSES; awaits its reply.

        Only the `_pump_up` loop reads PSES's responses, so this must never
        be awaited from inside that loop (it would wait forever on a reply
        the same coroutine is responsible for delivering).
        """
        if self._up_writer is None:
            return None
        seq = self._seqs.next_upstream_seq()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._proxy_requests[seq] = fut
        try:
            self._write_up(
                {
                    "seq": seq,
                    "type": "request",
                    "command": command,
                    "arguments": arguments,
                }
            )
            await self._up_writer.drain()
            return await asyncio.wait_for(fut, timeout)
        except (asyncio.TimeoutError, ConnectionError, OSError):
            return None
        finally:
            self._proxy_requests.pop(seq, None)

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
            self._shutting_down = True
            await self._ensure_pwsh_dead()
            await self._await_watch_exit()
            await self._close_upstream()
            await self._drain_tasks()
            self._cleanup_workdir()
            await self._writer.drain()

    async def _dispatch_client_message(self, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == "response":
            fwd = self._seqs.client_response_to_upstream(msg)
            if fwd is not None:
                self._write_up(fwd)
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
        if self._up_writer is None:
            self.send_error(msg, "no debug session")
            return
        if msg["command"] in _RESUME_COMMANDS:
            self._pause_pending = False
        self._write_up(self._seqs.client_request_to_upstream(msg))

    # ---- PSES socket side ----
    async def _pump_up(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                try:
                    msg = await read_message(reader)
                except (
                    ConnectionError,
                    asyncio.IncompleteReadError,
                    EOFError,
                    ValueError,
                ):
                    return
                mtype = msg.get("type")
                if mtype == "event":
                    if await self._note_and_filter_event(msg):
                        continue
                    self._write_client(self._seqs.upstream_event_to_client(msg))
                elif mtype == "response":
                    pending = self._proxy_requests.pop(msg.get("request_seq", -1), None)
                    if pending is not None:
                        if not pending.done():
                            pending.set_result(msg)
                        continue
                    out = self._seqs.upstream_response_to_client(msg)
                    if out is None:
                        continue
                    self._write_client(out)
                elif mtype == "request":
                    self._write_client(self._seqs.upstream_request_to_client(msg))
                await self._writer.drain()
        finally:
            # The debug socket is gone. PSES may have died without ever
            # sending `terminated` while the pwsh host survives, so nothing
            # else would end the session: forget the writer (later client
            # requests then get "no debug session" instead of vanishing into
            # a dead socket) and kill pwsh, which lets _watch_exit emit
            # exited(code) + terminated in order. Sync + fire-and-forget:
            # this runs under cancellation too, and _pump_up must never
            # await an upstream reply only it could deliver.
            self._detach_upstream()
            if self._launched and not self._sent_terminated:
                self._start_finish_session()

    async def _note_and_filter_event(self, msg: dict) -> bool:
        """Track session state; True -> swallow the event.

        Runs inside `_pump_up`, the only reader of PSES's responses: nothing
        here may `await self._up_request(...)` (it would deadlock). The
        `stopped` rewrites below write upstream with `_up_send`
        (fire-and-forget) for that reason.
        """
        event = msg.get("event")
        if event == "terminated":
            # PSES: the script ended, but pwsh is still alive. Kill it;
            # _watch_exit then emits exited(code) + terminated in order,
            # after the stdout pump has drained (the sentinel arrives on
            # a different pipe than this event and may still be in flight).
            self._terminated_seen = True
            self._start_finish_session()
            return True
        if event == "exited":
            return True  # never observed from PSES; _watch_exit owns it
        if event == "stopped":
            body = dict(msg.get("body") or {})
            if self._entry_pending:
                # The launcher breakpoint fired, just before the user's
                # script runs. Swallow this stop, drop the breakpoint and
                # step into the script; the resulting step stop is what the
                # client sees as the entry stop. Fire-and-forget — we are
                # inside `_pump_up` — but PSES handles requests in order
                # while stopped, so both land before anything else.
                self._entry_pending = False
                self._entry_stepping = True
                self._pause_pending = False
                self._up_send(
                    "setBreakpoints",
                    {"source": {"path": str(LAUNCHER)}, "breakpoints": []},
                )
                self._up_send("stepIn", {"threadId": body.get("threadId", 1)})
                return True
            if self._entry_stepping:
                self._entry_stepping = False
                body["reason"] = "entry"
                self._pause_pending = False
            elif self._pause_pending:
                if body.get("reason") == "step":
                    body["reason"] = "pause"
                self._pause_pending = False
            msg["body"] = body
        return False

    def _start_finish_session(self) -> None:
        """Spawn _finish_session once. Callers may run inside `_pump_up`, so
        this must stay synchronous and fire-and-forget."""
        if self._finishing or self._shutting_down:
            return
        self._finishing = True
        self._spawn_task(self._finish_session())

    async def _finish_session(self) -> None:
        # A no-op when the socket is already gone (_up_request returns None
        # immediately once _up_writer has been detached).
        await self._up_request("disconnect", {}, timeout=2.0)
        await self._ensure_pwsh_dead()

    async def _pump_stdout(self, stream: asyncio.StreamReader) -> None:
        while True:
            try:
                line = await stream.readline()
            except (ValueError, ConnectionError):
                # Overlong line (LimitOverrunError) or a broken pipe: stop
                # pumping rather than spinning on a stream we cannot read.
                log.exception("pwsh stdout pump failed")
                return
            if not line:
                return
            text = line.decode("utf-8", errors="replace")
            code = parse_exit_sentinel(text)
            if code is not None:
                self._exit_code = code
                continue
            category = self._classifier.classify(text)
            if category is None:
                continue
            self.send_event("output", {"category": category, "output": text})
            await self._writer.drain()

    async def _watch_exit(self) -> None:
        assert self._proc is not None
        rc = await self._proc.wait()
        if self._pump_tasks:
            await asyncio.wait(self._pump_tasks, timeout=2.0)
        if not self._launched:
            return
        if self._exit_code is not None:
            code = self._exit_code
        elif self._terminated_seen:
            code = 1  # script ended without reaching the launcher's sentinel
        else:
            code = rc
        if not self._sent_exited:
            self._sent_exited = True
            self.send_event("exited", {"exitCode": code})
        if not self._sent_terminated:
            self._sent_terminated = True
            self.send_event("terminated")
        await self._writer.drain()

    # ---- lifecycle handlers ----
    async def _on_initialize(self, request: dict) -> None:
        self._client_init_args = dict(request.get("arguments") or {})
        self.send_response(request, CAPABILITIES)

    async def _on_launch(self, request: dict) -> None:
        args = request.get("arguments") or {}
        program = os.path.abspath(args.get("program", ""))
        if not os.path.isfile(program):
            self.send_error(request, f"program not found: {program}")
            return
        try:
            pwsh = find_pwsh(args.get("pwsh"))
            pses_dir = find_pses(args.get("pses"))
        except FileNotFoundError as e:
            self.send_error(request, str(e))
            return
        self._program = program
        self._stop_on_entry = bool(args.get("stopOnEntry", False))
        cwd = args.get("cwd") or os.getcwd()
        env = {**os.environ, **(args.get("env") or {}), "NO_COLOR": "1", "TERM": "dumb"}

        self._workdir = tempfile.mkdtemp(prefix="tdb-pses-")
        session_file = Path(self._workdir) / "session.json"
        log_dir = Path(self._workdir) / "log"
        log_dir.mkdir()
        pipe_name = f"tdb-pses-{os.getpid()}-{secrets.token_hex(4)}"
        cmd = build_pwsh_command(pwsh, pses_dir, session_file, log_dir, pipe_name)

        popen_kwargs: dict[str, Any] = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
            if _pdeathsig is not None:
                popen_kwargs["preexec_fn"] = _pdeathsig
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                **popen_kwargs,
            )
        except OSError as e:
            self.send_error(request, f"cannot start {pwsh}: {e}")
            return
        assert self._proc.stdout is not None
        early: list[str] = []
        try:
            details = await self._await_session_file(session_file, early)
            version = _parse_version(details.get("powerShellVersion"))
            if version < MIN_PWSH:
                raise RuntimeError(
                    f"PowerShell {details.get('powerShellVersion')} is too old — "
                    f"tdb needs pwsh >= {MIN_PWSH[0]}.{MIN_PWSH[1]}"
                )
            reader, writer = await self._connect_with_retry(details)
        except Exception as e:
            await self._ensure_pwsh_dead()
            tail = await self._drain_early_stdout(early)
            self.send_error(request, f"{e}\n{tail}".strip())
            await self._writer.drain()
            return
        self._up_writer = writer
        self._launched = True
        self._entry_pending = self._stop_on_entry
        up_pump = self._spawn_task(self._pump_up(reader))
        self._pump_tasks = [
            self._spawn_task(self._pump_stdout(self._proc.stdout)),
            up_pump,
        ]
        # Any prompt-echo/early lines read while waiting for the session
        # file were already consumed; replay them through the classifier.
        for line in early:
            category = self._classifier.classify(line)
            if category is not None:
                self.send_event("output", {"category": category, "output": line})
        self._watch_exit_task = self._spawn_task(self._watch_exit())
        # PSES needs its own initialize first (proxy-originated; its response
        # is swallowed). It SILENTLY DROPS any request that arrives before it
        # has answered — so wait for the reply, then send the rewritten
        # launch. Safe to await here: `_on_launch` runs on the client loop,
        # and `_pump_up` (started above) is what delivers the reply.
        if (
            await self._up_request("initialize", dict(self._client_init_args), 10.0)
            is None
        ):
            await self._ensure_pwsh_dead()
            self.send_error(
                request,
                "PowerShell Editor Services did not answer `initialize` within "
                "10s — the debug session could not be started",
            )
            await self._writer.drain()
            return
        fwd = self._seqs.client_request_to_upstream(request)
        fwd["arguments"] = {
            "script": str(LAUNCHER),
            "args": [
                quote_ps_arg(program),
                *(quote_ps_arg(str(a)) for a in args.get("args") or []),
            ],
            "cwd": cwd,
        }
        self._write_up(fwd)
        await self._writer.drain()

    async def _await_session_file(self, session_file: Path, early: list[str]) -> dict:
        """Poll for PSES's session file while draining pwsh's stdout into
        `early` (so a dying pwsh's message is available for the error)."""
        assert self._proc is not None and self._proc.stdout is not None
        timeout = float(os.environ.get(_SESSION_TIMEOUT_ENV, _timeouts.ADAPTER_LISTEN))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        stdout = self._proc.stdout
        while True:
            if session_file.exists():
                try:
                    details = json.loads(session_file.read_text())
                except (OSError, ValueError):
                    details = None
                if details and details.get("status") == "started":
                    return details
            if self._proc.returncode is not None:
                raise RuntimeError(
                    f"pwsh exited with code {self._proc.returncode} before PSES started"
                )
            if loop.time() > deadline:
                # pwsh's stdout may already be at EOF while asyncio's child
                # watcher has yet to reap it; give the returncode one last
                # chance so the clearer "exited before PSES started" message
                # wins over a bare timeout.
                if stdout.at_eof():
                    await asyncio.sleep(0.1)
                    if self._proc.returncode is not None:
                        raise RuntimeError(
                            f"pwsh exited with code {self._proc.returncode} "
                            "before PSES started"
                        )
                raise TimeoutError(
                    f"PSES did not write its session file within {timeout:.0f}s"
                )
            try:
                line = await asyncio.wait_for(stdout.readline(), 0.1)
            except asyncio.TimeoutError:
                continue
            if line:
                early.append(line.decode("utf-8", errors="replace"))
            else:
                # EOF: readline() now returns instantly, so pace the poll
                # until the child watcher publishes the return code.
                await asyncio.sleep(0.05)

    async def _connect_with_retry(self, details: dict, timeout: float = 10.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            try:
                return await connect_debug_service(details)
            except (ConnectionError, FileNotFoundError, OSError):
                if self._proc is not None and self._proc.returncode is not None:
                    raise RuntimeError(
                        f"pwsh exited with code {self._proc.returncode} "
                        "before accepting a connection"
                    )
                if loop.time() > deadline:
                    raise TimeoutError("timed out connecting to PSES's debug socket")
                await asyncio.sleep(0.1)

    async def _drain_early_stdout(self, early: list[str]) -> str:
        if self._proc is not None and self._proc.stdout is not None:
            try:
                rest = await asyncio.wait_for(self._proc.stdout.read(), 0.5)
                early.append(rest.decode("utf-8", errors="replace"))
            except (asyncio.TimeoutError, OSError, ValueError):
                pass
        return "".join(early)[-2000:].strip()

    # ---- teardown ----
    def _kill_group(self, sig_kill: bool = False) -> None:
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

    async def _ensure_pwsh_dead(self, grace: float = 2.0) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        self._kill_group()
        try:
            await asyncio.wait_for(proc.wait(), grace)
        except asyncio.TimeoutError:
            self._kill_group(sig_kill=True)
            await proc.wait()

    async def _await_watch_exit(self, timeout: float = 4.0) -> None:
        task = self._watch_exit_task
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(task, timeout)
        except asyncio.TimeoutError:
            log.warning("_watch_exit did not finish within %.1fs of teardown", timeout)
        except Exception:
            log.exception("_watch_exit failed during teardown")

    def _detach_upstream(self) -> None:
        """Close the PSES socket and forget it: `_up_writer` back to None so
        `_dispatch_client_message` answers "no debug session" rather than
        writing into a dead socket. Synchronous, so it is safe from a
        cancelled task's `finally`."""
        writer, self._up_writer = self._up_writer, None
        if writer is None:
            return
        self._dead_up_writer = writer
        try:
            writer.close()
        except (ConnectionError, OSError):
            pass

    async def _close_upstream(self) -> None:
        """Close the PSES socket (if `_pump_up` has not already) and await the
        transport, so it is not GC'd open."""
        self._detach_upstream()
        writer, self._dead_up_writer = self._dead_up_writer, None
        if writer is None:
            return
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass

    async def _drain_tasks(self, timeout: float = 2.0) -> None:
        """Let the pumps notice the closed socket, then cancel stragglers so
        the loop never shuts down with pending tasks."""
        pending = {t for t in self._tasks if not t.done()}
        if not pending:
            return
        done, pending = await asyncio.wait(pending, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                log.error("background task failed", exc_info=task.exception())

    def _cleanup_workdir(self) -> None:
        if self._workdir:
            shutil.rmtree(self._workdir, ignore_errors=True)
            self._workdir = None

    async def _on_disconnect(self, request: dict) -> None:
        if self._up_writer is not None:
            await self._up_request("disconnect", {}, timeout=2.0)
        await self._ensure_pwsh_dead()
        self.send_response(request)
        self._done.set()

    async def _on_terminate(self, request: dict) -> None:
        if self._up_writer is not None:
            await self._up_request("disconnect", {}, timeout=2.0)
        await self._ensure_pwsh_dead()
        self.send_response(request)

    # ---- rewrites ----
    async def _on_configurationDone(self, request: dict) -> None:
        """Arm the stopOnEntry breakpoint on the launcher, then hand over.

        PSES drops requests sent before it answers, so the breakpoint is
        awaited (safe: this runs on the client loop, not `_pump_up`) before
        `configurationDone` lets the script run.
        """
        if self._up_writer is None:
            self.send_error(request, "no debug session")
            return
        if self._entry_pending:
            resp = await self._up_request(
                "setBreakpoints",
                {
                    "source": {"path": str(LAUNCHER)},
                    "breakpoints": [{"line": LAUNCHER_CALL_LINE}],
                },
            )
            if resp is None or not resp.get("success"):
                # No entry breakpoint: run on rather than mislabel the next
                # stop (a real breakpoint) as the entry stop.
                log.warning("could not arm the stopOnEntry breakpoint: %s", resp)
                self._entry_pending = False
        self._write_up(self._seqs.client_request_to_upstream(request))

    async def _on_pause(self, request: dict) -> None:
        if self._up_writer is None:
            self.send_error(request, "no debug session")
            return
        self._pause_pending = True
        self._write_up(self._seqs.client_request_to_upstream(request))

    async def _on_evaluate(self, request: dict) -> None:
        if self._up_writer is None:
            self.send_error(request, "no debug session")
            return
        args = dict(request.get("arguments") or {})
        if args.get("context", "repl") == "repl":
            args["context"] = "watch"
        self._write_up(
            self._seqs.client_request_to_upstream({**request, "arguments": args})
        )

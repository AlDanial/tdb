"""perl5db session driver.

Owns the debuggee child process (launch mode) or an adopted socket
(attach mode), the stream parser, and the strict one-command-at-a-time
queue. perl5db is a human REPL: a command's response ends at the next
prompt; a prompt with NO command pending means the program stopped.
"""

from __future__ import annotations

import asyncio
import importlib.resources
import logging
import os
import shlex
import shutil
import signal
import tempfile
from typing import Awaitable, Callable

from tdb.adapters.perl.protocol import StreamParser

log = logging.getLogger(__name__)

RunInTerminal = Callable[[list[str], str, dict[str, str]], Awaitable[None]]


class PerlProtocolError(Exception):
    def __init__(self, message: str, tail: str = "") -> None:
        super().__init__(message)
        self.tail = tail


def helpers_path() -> str:
    ref = importlib.resources.files("tdb.adapters.perl") / "helpers.pl"
    return str(ref)


def compile_shim_path() -> str:
    ref = importlib.resources.files("tdb.adapters.perl") / "Devel" / "TdbCompile.pm"
    return str(ref)


class PerlSession:
    def __init__(
        self,
        on_output: Callable[[str, str], None],
        on_stop: Callable[[], None],
    ) -> None:
        self._on_output = on_output
        self._on_stop = on_stop
        self._parser = StreamParser()
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._pump_tasks: list[asyncio.Task] = []
        # Events collected for the in-flight command; None => no command
        # pending (running or idle-at-prompt).
        self._collect: list[tuple] | None = None
        self._prompt_evt = asyncio.Event()
        self._tail = b""
        self.stopped = False
        self._eof = False
        self.debuggee_pid: int | None = None
        self._exit_status_path: str | None = None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    @property
    def eof(self) -> bool:
        """True once the debug socket has genuinely closed (read loop hit
        EOF). Lets callers that force a `command("q")` to try to end the
        session distinguish "it actually closed the connection" from "it
        timed out / replied without dying" -- the two need different
        follow-up handling (see server.py's `_eof_terminated` guard)."""
        return self._eof

    async def wait_exit_code(self, timeout: float = 2.0) -> int:
        """Best-effort real exit code of the owned child (launch mode only).

        Attach mode has no owned child (`self._process` is None) -- always
        0 there. In launch mode, perl5db parks at a live "?" prompt (or the
        debug socket can close on a hard crash) slightly before the OS has
        actually reaped the child, so this waits briefly rather than
        assuming `returncode` is already populated. Bounded so a child that,
        for whatever reason, never exits can't block the event loop
        indefinitely -- falls back to 0 in that case.
        """
        if self._process is None and self._exit_status_path is not None:
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                try:
                    text = open(self._exit_status_path).read().strip()
                except OSError:
                    text = ""
                if text:
                    return int(text)
                await asyncio.sleep(0.05)
            return -1
        if self._process is None:
            return 0
        if self._process.returncode is not None:
            return self._process.returncode
        try:
            await asyncio.wait_for(self._process.wait(), timeout)
        except asyncio.TimeoutError:
            return 0
        return self._process.returncode if self._process.returncode is not None else 0

    async def launch(
        self,
        program: str,
        args: list[str],
        cwd: str,
        env: dict | None,
        perl: str = "perl",
        run_in_terminal: RunInTerminal | None = None,
    ) -> None:
        server_ready = asyncio.get_running_loop().create_future()

        async def _on_connect(reader, writer):
            if not server_ready.done():
                server_ready.set_result((reader, writer))

        server = await asyncio.start_server(_on_connect, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        child_env = dict(env or os.environ)
        child_env["PERLDB_OPTS"] = f"RemotePort=127.0.0.1:{port}"
        # Devel::TdbCompile (this adapter's own compile-phase shim --
        # see that module's header) arms perl5db to trap during
        # perl's compile phase, so BEGIN blocks become steppable. It
        # only traps statements belonging to `program`, filtered by
        # comparing against exactly the string perl reports in
        # `caller` for it -- which is argv's program path VERBATIM,
        # not resolved/canonicalized (confirmed empirically against
        # perl 5.40.1). This MUST match `program` below exactly, or
        # the filter silently never matches and the whole feature is
        # a no-op.
        #
        # Defensive existence check: as of this writing pyproject.toml's
        # package-data does NOT list Devel/TdbCompile.pm (only
        # helpers.pl and TdbRemote.pm), so a built wheel installed
        # non-editable ships without this file. Passing -MDevel::TdbCompile
        # unconditionally in that case makes perl abort at startup
        # ("Can't locate Devel/TdbCompile.pm in @INC ... BEGIN failed") --
        # launch mode would be entirely broken, not merely missing
        # BEGIN-block stepping. Omit the -I/-M pair and degrade to a
        # normal launch instead. The packaging fix (adding the file to
        # pyproject.toml's package-data) is still required for the
        # feature to actually work out of a wheel -- this only prevents
        # a missing file from taking the whole adapter down.
        adapters_dir = os.path.dirname(helpers_path())
        shim_path = compile_shim_path()
        compile_shim_available = os.path.isfile(shim_path)
        argv = [perl, "-d"]
        if compile_shim_available:
            child_env["TDB_COMPILE_FILE"] = program
            argv += [f"-I{adapters_dir}", "-MDevel::TdbCompile"]
        else:
            log.warning(
                "Devel::TdbCompile shim not found at %s -- compile-phase "
                "(BEGIN-block) debugging is unavailable for this launch; "
                "continuing with a normal launch.",
                shim_path,
            )
        argv += [program, *args]
        timeout = 30.0 if run_in_terminal is not None else 15.0
        if run_in_terminal is not None:
            # The debuggee is spawned by the client inside a terminal
            # emulator; we cannot reap it, so a /bin/sh wrapper writes
            # $? (128+n for signal deaths) where wait_exit_code() reads.
            status_dir = tempfile.mkdtemp(prefix="tdb-perl-")
            self._exit_status_path = os.path.join(status_dir, "exit-status")
            wrapped = [
                "/bin/sh",
                "-c",
                f"{shlex.join(argv)}; printf %s $? > "
                f"{shlex.quote(self._exit_status_path)}",
            ]
            await run_in_terminal(wrapped, cwd, child_env)
        else:
            self._process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=child_env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            self._pump_tasks = [
                asyncio.create_task(self._pump(self._process.stdout, "stdout")),
                asyncio.create_task(self._pump(self._process.stderr, "stderr")),
            ]
        try:
            self._reader, self._writer = await asyncio.wait_for(server_ready, timeout)
        except asyncio.TimeoutError:
            if run_in_terminal is not None:
                raise PerlProtocolError(
                    "perl5db never connected — the external terminal did not "
                    "launch perl, or perl is not installed/too old there"
                )
            raise PerlProtocolError(
                "perl5db never connected — is perl installed and >= 5.18?"
            )
        finally:
            server.close()
        self._reader_task = asyncio.create_task(self._read_loop())
        await self._await_prompt(timeout=timeout)
        self.stopped = True
        await self.command(f"do '{helpers_path()}'")
        if run_in_terminal is not None:
            reply = await self.command("p $$")
            digits = "".join(
                ch for ev in reply if ev[0] == "text" for ch in ev[1] if ch.isdigit()
            )
            self.debuggee_pid = int(digits) if digits else None

    async def attach_socket(self, reader, writer) -> None:
        """Adopt an already-connected perl5db socket (attach mode)."""
        self._reader, self._writer = reader, writer
        self._reader_task = asyncio.create_task(self._read_loop())
        await self._await_prompt(timeout=15.0)
        self.stopped = True

    async def _pump(self, stream: asyncio.StreamReader, category: str) -> None:
        while True:
            data = await stream.read(4096)
            if not data:
                return
            self._on_output(data.decode("utf-8", errors="replace"), category)

    async def _read_loop(self) -> None:
        assert self._reader is not None
        while True:
            data = await self._reader.read(4096)
            if not data:
                self._on_stop_eof()
                return
            self._tail = (self._tail + data)[-500:]
            for ev in self._parser.feed(data):
                self._dispatch(ev)

    def _on_stop_eof(self) -> None:
        self.stopped = False
        self._eof = True
        self._prompt_evt.set()  # unblock any waiter; command() checks EOF
        # A real socket close (e.g. after `q`) — as opposed to perl5db's
        # "Debugged program terminated" state, which parks at a live prompt
        # without closing the socket. Route this through on_output so the
        # server can translate it into terminated/exited events.
        self._on_output("", "__eof__")

    def _dispatch(self, ev: tuple) -> None:
        kind = ev[0]
        if kind == "prompt":
            self.stopped = True
            if self._collect is not None:
                self._prompt_evt.set()
            else:
                self._on_stop()  # unsolicited: breakpoint / step landed
        elif self._collect is not None:
            self._collect.append(ev)
        elif kind == "text":
            # perl5db chatter while running (the source-line echo it
            # prints at every stop, termination notices). The socket is
            # a control channel -- program output arrives via the
            # stdout/stderr pipes -- so never surface this as output:
            # forwarding it put the debugged source lines in the
            # Console view.
            log.debug("perl5db chatter: %r", ev[1])

    async def _await_prompt(self, timeout: float) -> None:
        self._collect = []
        try:
            await asyncio.wait_for(self._prompt_evt.wait(), timeout)
        except asyncio.TimeoutError:
            raise PerlProtocolError(
                "timed out waiting for perl5db prompt",
                tail=self._tail.decode("utf-8", errors="replace"),
            )
        finally:
            self._prompt_evt.clear()
            self._collect = None
        if self._eof:
            raise PerlProtocolError(
                "perl5db connection closed",
                tail=self._tail.decode("utf-8", errors="replace"),
            )

    async def command(self, text: str, timeout: float = 20.0) -> list[tuple]:
        if self._writer is None:
            raise PerlProtocolError("session not connected")
        self._collect = []
        self._prompt_evt.clear()
        self.stopped = False
        self._writer.write(text.encode("utf-8") + b"\n")
        await self._writer.drain()
        try:
            await asyncio.wait_for(self._prompt_evt.wait(), timeout)
        except asyncio.TimeoutError:
            events, self._collect = self._collect, None
            raise PerlProtocolError(
                f"no prompt after command {text!r}",
                tail=self._tail.decode("utf-8", errors="replace"),
            )
        events, self._collect = self._collect, None
        self._prompt_evt.clear()
        if self._eof:
            raise PerlProtocolError(
                "perl5db connection closed",
                tail=self._tail.decode("utf-8", errors="replace"),
            )
        self.stopped = True
        return events

    async def helper(self, expr: str, timeout: float = 20.0) -> dict:
        events = await self.command(expr, timeout=timeout)
        for ev in events:
            if ev[0] == "json":
                payload = ev[1]
                if "error" in payload:
                    raise PerlProtocolError(f"helper error: {payload['error']}")
                return payload
        raise PerlProtocolError(
            f"helper produced no JSON: {expr!r}",
            tail=self._tail.decode("utf-8", errors="replace"),
        )

    def resume(self, cmd: str) -> None:
        assert self._writer is not None
        self.stopped = False
        self._collect = None
        self._writer.write(cmd.encode("utf-8") + b"\n")

    def interrupt(self) -> bool:
        """SIGINT the owned child (launch-mode pause). False if not owned."""
        if self._process is None:
            return False
        try:
            os.kill(self._process.pid, signal.SIGINT)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    async def stop(self) -> None:
        tasks = [t for t in [self._reader_task, *self._pump_tasks] if t is not None]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._writer is not None:
            self._writer.close()
        if self._process is not None:
            try:
                self._process.kill()
            except ProcessLookupError:
                pass
            await self._process.wait()
        if self._process is None and self.debuggee_pid is not None:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.kill(self.debuggee_pid, sig)
                except (ProcessLookupError, PermissionError):
                    break
                await asyncio.sleep(0.05)
        if self._exit_status_path is not None:
            shutil.rmtree(os.path.dirname(self._exit_status_path), ignore_errors=True)
            self._exit_status_path = None

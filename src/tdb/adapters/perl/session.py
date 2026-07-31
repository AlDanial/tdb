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
import signal
from typing import Callable

from tdb.adapters.perl.protocol import StreamParser

log = logging.getLogger(__name__)


class PerlProtocolError(Exception):
    def __init__(self, message: str, tail: str = "") -> None:
        super().__init__(message)
        self.tail = tail


def helpers_path() -> str:
    ref = importlib.resources.files("tdb.adapters.perl") / "helpers.pl"
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

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    async def launch(
        self,
        program: str,
        args: list[str],
        cwd: str,
        env: dict | None,
        perl: str = "perl",
    ) -> None:
        server_ready = asyncio.get_running_loop().create_future()

        async def _on_connect(reader, writer):
            if not server_ready.done():
                server_ready.set_result((reader, writer))

        server = await asyncio.start_server(_on_connect, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        child_env = dict(env or os.environ)
        child_env["PERLDB_OPTS"] = f"RemotePort=127.0.0.1:{port}"
        self._process = await asyncio.create_subprocess_exec(
            perl,
            "-d",
            program,
            *args,
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
            self._reader, self._writer = await asyncio.wait_for(server_ready, 15.0)
        except asyncio.TimeoutError:
            raise PerlProtocolError(
                "perl5db never connected — is perl installed and >= 5.18?"
            )
        finally:
            server.close()
        self._reader_task = asyncio.create_task(self._read_loop())
        await self._await_prompt(timeout=15.0)
        self.stopped = True
        await self.command(f"do '{helpers_path()}'")

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

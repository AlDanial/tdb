"""Owns the instrumented bash subprocess and its two control pipes."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import shutil
import signal
import tempfile
from typing import Callable

log = logging.getLogger(__name__)

HARNESS = os.path.join(os.path.dirname(__file__), "tdb_harness.sh")


class BashProtocolError(Exception):
    pass


def canonical(path: str) -> str:
    """MUST match the harness: realpath of the directory + raw basename."""
    return os.path.join(
        os.path.realpath(os.path.dirname(os.path.abspath(path))),
        os.path.basename(path),
    )


def b64(value: str) -> str:
    if not value:
        return "-"
    return base64.b64encode(value.encode()).decode()


def unb64(field: str) -> str:
    if field == "-":
        return ""
    return base64.b64decode(field).decode(errors="replace")


class BashSession:
    def __init__(
        self,
        on_output: Callable[[str, str], None],
        on_stop: Callable[[str, str, int], None],
        on_exit: Callable[[int], None],
    ) -> None:
        self._on_output = on_output
        self._on_stop = on_stop
        self._on_exit = on_exit
        self.stopped = False
        self.exit_code: int | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._cmd_w: int | None = None
        self._resp_reader: asyncio.StreamReader | None = None
        self._pending: asyncio.Future | None = None  # one in-flight ok/err request
        self._ready: asyncio.Future | None = None
        self._tasks: list[asyncio.Task] = []
        self._tmpdir: tempfile.TemporaryDirectory | None = None
        self._stderr_tail = ""  # last stderr bytes, for pre-ready failures

    async def launch(
        self,
        *,
        program: str,
        args: list[str],
        cwd: str,
        env: dict | None,
        bash: str = "bash",
    ) -> None:
        if os.name == "nt":
            raise BashProtocolError(
                "bash debugging is not supported on native Windows in this "
                "version — run tdb inside WSL instead"
            )
        bash_path = shutil.which(bash)
        if bash_path is None:
            raise BashProtocolError(
                f"bash not found ({bash!r}) — install bash >= 4.4 or set "
                '{"adapters": {"bash": "/path/to/bash"}} in tdb\'s config.json'
            )
        if not os.path.isfile(HARNESS):
            raise BashProtocolError(
                f"tdb's bash harness is missing from this installation "
                f"({HARNESS}) — the tdb package was built without its "
                f"package-data; reinstall tdb"
            )
        loop = asyncio.get_running_loop()
        self._ready = loop.create_future()
        self._tmpdir = tempfile.TemporaryDirectory(prefix="tdb-bash-")
        cmd_r, cmd_w = os.pipe()  # adapter writes, bash reads
        resp_r, resp_w = os.pipe()  # bash writes, adapter reads
        os.set_inheritable(cmd_r, True)
        os.set_inheritable(resp_w, True)
        child_env = dict(env or os.environ)
        child_env["BASH_ENV"] = HARNESS
        child_env["__TDB_CMD_FD"] = str(cmd_r)
        child_env["__TDB_RESP_FD"] = str(resp_w)
        child_env["__TDB_TMP"] = self._tmpdir.name
        self._process = await asyncio.create_subprocess_exec(
            bash_path,
            program,
            *args,
            cwd=cwd,
            env=child_env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            pass_fds=(cmd_r, resp_w),
        )
        os.close(cmd_r)
        os.close(resp_w)
        self._cmd_w = cmd_w
        self._resp_reader = asyncio.StreamReader()
        transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(self._resp_reader),
            os.fdopen(resp_r, "rb"),
        )
        self._tasks = [
            asyncio.create_task(self._resp_loop()),
            asyncio.create_task(self._pump(self._process.stdout, "stdout")),
            asyncio.create_task(self._pump(self._process.stderr, "stderr")),
            asyncio.create_task(self._reap()),
        ]
        try:
            await asyncio.wait_for(self._ready, 15.0)
        except (asyncio.TimeoutError, BashProtocolError) as e:
            await self.stop()
            if isinstance(e, BashProtocolError):
                raise
            raise BashProtocolError(
                "the bash harness never reported ready — is bash hung during startup?"
            )
        self.stopped = True  # config phase

    async def _pump(self, stream: asyncio.StreamReader, category: str) -> None:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            text = chunk.decode(errors="replace")
            if category == "stderr":
                self._stderr_tail = (self._stderr_tail + text)[-500:]
            self._on_output(text, category)

    async def _reap(self) -> None:
        code = await self._process.wait()
        self.exit_code = code
        # let output pumps flush before reporting exit
        await asyncio.gather(*self._tasks[1:3], return_exceptions=True)
        if self._ready and not self._ready.done():
            # died before the handshake: fail launch() fast with the real
            # reason (e.g. the harness's "bash >= 4.4 required" line)
            tail = self._stderr_tail.strip()
            self._ready.set_exception(
                BashProtocolError(
                    "bash exited before the harness reported ready"
                    + (f": {tail}" if tail else " (bash >= 4.4 required?)")
                )
            )
            return
        if self._pending and not self._pending.done():
            self._pending.set_exception(BashProtocolError("debuggee exited"))
        self._on_exit(code)

    async def _resp_loop(self) -> None:
        while True:
            line = await self._resp_reader.readline()
            if not line:
                return
            fields = line.decode(errors="replace").split()
            if not fields:
                continue
            kind = fields[0]
            if kind == "ready":
                if self._ready and not self._ready.done():
                    self._ready.set_result(None)
            elif kind == "stopped":
                self.stopped = True
                self._on_stop(fields[1], unb64(fields[2]), int(fields[3]))
            elif kind in ("ok", "err"):
                fut, self._pending = self._pending, None
                if fut and not fut.done():
                    if kind == "ok":
                        fut.set_result(unb64(fields[1]) if len(fields) > 1 else "")
                    else:
                        fut.set_exception(BashProtocolError(unb64(fields[1])))

    def _write_line(self, line: str) -> None:
        if self._cmd_w is None:
            raise BashProtocolError("no session")
        os.write(self._cmd_w, (line + "\n").encode())

    def send_async(self, line: str) -> None:
        """Fire-and-forget while the debuggee is running (bp edits, pause)."""
        self._write_line(line)

    async def request(self, line: str) -> str:
        """Send a command that gets an ok/err reply (stopped/config phase only)."""
        if self._pending is not None:
            raise BashProtocolError("another request is in flight")
        self._pending = asyncio.get_running_loop().create_future()
        self._write_line(line)
        return await asyncio.wait_for(self._pending, 15.0)

    def resume(self, mode: str) -> None:
        assert mode in ("step", "next", "finish", "continue")
        self.stopped = False
        self._write_line(mode)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self._process is not None and self._process.returncode is None:
            try:
                os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            await self._process.wait()
        if self._cmd_w is not None:
            try:
                os.close(self._cmd_w)
            except OSError:
                pass
            self._cmd_w = None
        if self._tmpdir is not None:
            self._tmpdir.cleanup()
            self._tmpdir = None

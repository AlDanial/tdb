"""Shared harness for the live breakpoint hooks (Perl, Ruby, Go, ...).

The hooks spawn `tdb` themselves, so tests point `$TDB` at a fake tdb that
records its argv (to recover the ephemeral port or pid) and then sleeps,
standing in for a live TUI. The debuggee runs under a pty because every
hook, like the Python one, is a no-op without a tty.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import signal
import subprocess
import time
from pathlib import Path

import pytest

FAKE_TDB = """\
#!/bin/sh
# Append one line per spawn: "<pid> <argv...>", then play dead-but-alive.
echo "$$ $*" >> "$FAKE_TDB_LOG"
exec sleep 600
"""


class HookDebuggee:
    """A program using a tdb breakpoint hook, run under a pty with a fake tdb."""

    def __init__(
        self,
        tmp_path: Path,
        argv: list[str],
        *,
        tdb: str | None = None,
        env: dict[str, str] | None = None,
    ):
        self.log = tmp_path / "fake_tdb.log"
        fake = tmp_path / "fake_tdb"
        fake.write_text(FAKE_TDB)
        fake.chmod(0o755)
        full_env = {
            **os.environ,
            **(env or {}),
            "TDB": tdb if tdb is not None else str(fake),
            "FAKE_TDB_LOG": str(self.log),
        }
        self.master, slave = pty.openpty()
        self.proc = subprocess.Popen(
            argv,
            stdin=slave,
            stdout=slave,
            stderr=subprocess.PIPE,
            env=full_env,
        )
        os.close(slave)

    def stderr_text(self) -> str:
        """Whatever the debuggee has written to stderr so far. Non-blocking:
        the fake tdb inherits the debuggee's stderr (as the real tdb does),
        so the pipe never reaches EOF while a fake tdb is alive."""
        fd = self.proc.stderr.fileno()
        os.set_blocking(fd, False)
        chunks = []
        while True:
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode(errors="replace")

    def spawns(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [line.split() for line in self.log.read_text().splitlines()]

    def wait_spawn(self, n: int, timeout: float = 15.0) -> list[str]:
        """Block until the fake tdb has been spawned `n` times; return the
        n-th spawn's argv (after the pid)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            spawns = self.spawns()
            if len(spawns) >= n:
                return spawns[n - 1][1:]
            if self.proc.poll() is not None:
                break
            time.sleep(0.05)
        pytest.fail(
            f"fake tdb spawn #{n} never happened; spawns={self.spawns()} "
            f"rc={self.proc.poll()} stderr={self.stderr_text()!r}"
        )

    def fake_pid(self, n: int) -> int:
        return int(self.spawns()[n - 1][0])

    def finish(self, timeout: float = 20.0) -> tuple[str, str]:
        """Wait for the debuggee to exit; return (tty output, stderr)."""
        err = ""
        try:
            self.proc.wait(timeout=timeout)
        finally:
            err = self.stderr_text()
        out = b""
        # Drain whatever the pty holds. Non-blocking: a fake tdb that is
        # still alive keeps the slave end open (it inherited the tty, as
        # the real tdb does), so a blocking read would never see EOF.
        flags = fcntl.fcntl(self.master, fcntl.F_GETFL)
        fcntl.fcntl(self.master, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        while True:
            try:
                chunk = os.read(self.master, 4096)
            except BlockingIOError:
                break
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        return out.decode(errors="replace"), err

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        for pid_line in self.spawns():
            try:
                os.kill(int(pid_line[0]), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass
        try:
            os.close(self.master)
        except OSError:
            pass


def remote_port(argv: list[str], lang: str, flag: str = "-r") -> int:
    """Parse `--lang LANG -r 127.0.0.1:PORT` out of a fake-tdb spawn."""
    assert argv[:3] == ["--lang", lang, flag], argv
    host, port = argv[3].split(":")
    assert host == "127.0.0.1"
    return int(port)


class TcpDapClient:
    """Minimal DAP-over-TCP client for debug servers that speak DAP directly
    (rdbg, dlv dap). Same request/wait_event surface as AdapterClient."""

    def __init__(self):
        self.seq = 0
        self.events: list[dict] = []
        self._responses: dict[int, asyncio.Future] = {}
        self._reader_task = None
        self.writer = None

    async def connect(self, host: str, port: int, retries: int = 100):
        for _ in range(retries):
            try:
                self.reader, self.writer = await asyncio.open_connection(host, port)
                break
            except OSError:
                await asyncio.sleep(0.1)
        else:
            pytest.fail(f"could not connect to {host}:{port}")
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        while True:
            try:
                header = b""
                while not header.endswith(b"\r\n\r\n"):
                    header += await self.reader.readexactly(1)
                length = int(header.split(b":")[1])
                body = json.loads(await self.reader.readexactly(length))
            except (asyncio.IncompleteReadError, ValueError, ConnectionError):
                return
            if body["type"] == "event":
                self.events.append(body)
            elif body["type"] == "response":
                fut = self._responses.pop(body["request_seq"], None)
                if fut and not fut.done():
                    fut.set_result(body)

    def send(self, command: str, arguments: dict | None = None):
        self.seq += 1
        msg = {"seq": self.seq, "type": "request", "command": command}
        if arguments is not None:
            msg["arguments"] = arguments
        fut = asyncio.get_running_loop().create_future()
        self._responses[self.seq] = fut
        body = json.dumps(msg).encode()
        self.writer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        return fut

    async def request(
        self, command: str, arguments: dict | None = None, timeout: float = 30.0
    ) -> dict:
        fut = self.send(command, arguments)
        await self.writer.drain()
        return await asyncio.wait_for(fut, timeout)

    async def wait_event(self, name: str, timeout: float = 30.0) -> dict:
        for _ in range(int(timeout * 10)):
            for ev in self.events:
                if ev["event"] == name:
                    self.events.remove(ev)
                    return ev
            await asyncio.sleep(0.1)
        raise AssertionError(f"event {name!r} never arrived; saw {self.events}")

    async def stop(self):
        """Close the connection and make sure the close has really happened
        before returning: tests go on to block the event loop in a sync
        wait for the next fake-tdb spawn, and a close that is merely
        scheduled would leave the debug server holding a live-looking
        socket forever."""
        if self.writer is not None:
            self.writer.close()
            try:
                await asyncio.wait_for(self.writer.wait_closed(), 5)
            except (asyncio.TimeoutError, ConnectionError, OSError):
                pass
        if self._reader_task is not None:
            self._reader_task.cancel()

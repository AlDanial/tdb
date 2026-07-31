"""Minimal scripted DAP client for driving the perl adapter subprocess."""

import asyncio
import json
import sys


class AdapterClient:
    def __init__(self):
        self.proc = None
        self.seq = 0
        self.events: list[dict] = []
        self._responses: dict[int, asyncio.Future] = {}

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tdb.adapters.perl",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        while True:
            try:
                header = b""
                while not header.endswith(b"\r\n\r\n"):
                    chunk = await self.proc.stdout.readexactly(1)
                    header += chunk
                length = int(header.split(b":")[1])
                body = json.loads(await self.proc.stdout.readexactly(length))
            except (asyncio.IncompleteReadError, ValueError):
                return
            if body["type"] == "event":
                self.events.append(body)
            elif body["type"] == "response":
                fut = self._responses.pop(body["request_seq"], None)
                if fut and not fut.done():
                    fut.set_result(body)

    async def request(
        self, command: str, arguments: dict | None = None, timeout: float = 30.0
    ) -> dict:
        self.seq += 1
        msg = {"seq": self.seq, "type": "request", "command": command}
        if arguments:
            msg["arguments"] = arguments
        fut = asyncio.get_running_loop().create_future()
        self._responses[self.seq] = fut
        body = json.dumps(msg).encode()
        self.proc.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        await self.proc.stdin.drain()
        return await asyncio.wait_for(fut, timeout)

    def send(self, command: str, arguments: dict | None = None):
        """Fire a request without awaiting the response (launch/attach)."""
        self.seq += 1
        msg = {"seq": self.seq, "type": "request", "command": command}
        if arguments:
            msg["arguments"] = arguments
        fut = asyncio.get_running_loop().create_future()
        self._responses[self.seq] = fut
        body = json.dumps(msg).encode()
        self.proc.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        return fut

    async def wait_event(self, name: str, timeout: float = 30.0) -> dict:
        for _ in range(int(timeout * 10)):
            for ev in self.events:
                if ev["event"] == name:
                    self.events.remove(ev)
                    return ev
            await asyncio.sleep(0.1)
        raise AssertionError(f"event {name!r} never arrived; saw {self.events}")

    async def stop(self):
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()

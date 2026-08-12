from __future__ import annotations

import asyncio
import os
import sys
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path

from tdb.adapters.tcsh.protocol import EndOfStream, ProtocolError, encode_message, read_message


class DAPClient:
    """Deterministic black-box DAP client with useful timeout diagnostics."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        *,
        shutdown_timeout: float = 5,
    ) -> None:
        self.process = process
        self._shutdown_timeout = shutdown_timeout
        self.messages: list[dict[str, object]] = []
        self.events: defaultdict[str, list[dict[str, object]]] = defaultdict(list)
        self._responses: dict[int, dict[str, object]] = {}
        self._condition = asyncio.Condition()
        self._next_seq = 1
        self._stderr = bytearray()
        self._collector = asyncio.create_task(self._collect_messages())
        self._stderr_collector = asyncio.create_task(self._collect_stderr())

    @classmethod
    async def start(cls, *, shutdown_timeout: float = 5) -> DAPClient:
        root = Path(__file__).resolve().parents[2]
        environment = dict(os.environ)
        source_path = str(root / "src")
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            source_path if not existing else os.pathsep.join((source_path, existing))
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tdb.adapters.tcsh",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        return cls(process, shutdown_timeout=shutdown_timeout)

    async def request(
        self,
        command: str,
        arguments: Mapping[str, object] | None = None,
        *,
        timeout: float = 5,
    ) -> dict[str, object]:
        sequence = self._next_seq
        self._next_seq += 1
        message: dict[str, object] = {
            "seq": sequence,
            "type": "request",
            "command": command,
        }
        if arguments is not None:
            message["arguments"] = dict(arguments)
        assert self.process.stdin is not None
        self.process.stdin.write(encode_message(message))
        await self.process.stdin.drain()

        async def wait() -> dict[str, object]:
            async with self._condition:
                await self._condition.wait_for(lambda: sequence in self._responses)
                return self._responses.pop(sequence)

        return await self._with_diagnostics(wait(), f"response to {command}", timeout)

    async def initialize(self) -> dict[str, object]:
        response = await self.request("initialize", {"adapterID": "tcsh"})
        if response.get("success") is not True:
            raise AssertionError(f"initialize failed: {response!r}; stderr={self.stderr!r}")
        await self.wait_for_event("initialized")
        return response

    async def launch(self, program: Path, **arguments: object) -> dict[str, object]:
        launch_arguments = {"program": str(program), **arguments}
        response = await self.request("launch", launch_arguments)
        if response.get("success") is not True:
            raise AssertionError(f"launch failed: {response!r}; stderr={self.stderr!r}")
        return response

    def output_contains(self, text: str) -> bool:
        return any(
            message.get("type") == "event"
            and message.get("event") == "output"
            and text in str(message.get("body", {}).get("output", ""))
            for message in self.messages
            if isinstance(message.get("body"), dict)
        )

    async def wait_for_output(
        self,
        text: str,
        *,
        category: str | None = None,
        timeout: float = 5,
    ) -> dict[str, object]:
        def matching() -> dict[str, object] | None:
            for message in self.messages:
                if message.get("type") != "event" or message.get("event") != "output":
                    continue
                body = message.get("body")
                if not isinstance(body, dict):
                    continue
                if category is not None and body.get("category") != category:
                    continue
                if text in str(body.get("output", "")):
                    return message
            return None

        async def wait() -> dict[str, object]:
            async with self._condition:
                await self._condition.wait_for(lambda: matching() is not None)
                result = matching()
                assert result is not None
                return result

        return await self._with_diagnostics(wait(), f"output containing {text!r}", timeout)

    async def wait_for_event(
        self,
        name: str,
        timeout: float = 5,
    ) -> dict[str, object]:
        async def wait() -> dict[str, object]:
            async with self._condition:
                await self._condition.wait_for(lambda: bool(self.events[name]))
                return self.events[name].pop(0)

        return await self._with_diagnostics(wait(), f"{name!r} event", timeout)

    async def close(self) -> None:
        if self.process.stdin is not None and not self.process.stdin.is_closing():
            self.process.stdin.close()
            try:
                await asyncio.wait_for(
                    self.process.stdin.wait_closed(),
                    timeout=self._shutdown_timeout,
                )
            except TimeoutError:
                pass
        try:
            if not await self._wait_for_exit():
                self.process.terminate()
                if not await self._wait_for_exit():
                    self.process.kill()
                    if not await self._wait_for_exit():
                        raise AssertionError("Adapter process did not exit after SIGKILL")
        finally:
            await self._finish_collectors()

    @property
    def stderr(self) -> str:
        return self._stderr.decode("utf-8", errors="replace")

    async def _collect_messages(self) -> None:
        assert self.process.stdout is not None
        try:
            while True:
                message = await read_message(self.process.stdout)
                self.messages.append(message)
                async with self._condition:
                    if message.get("type") == "response":
                        request_seq = message.get("request_seq")
                        if isinstance(request_seq, int):
                            self._responses[request_seq] = message
                    elif message.get("type") == "event":
                        event = message.get("event")
                        if isinstance(event, str):
                            self.events[event].append(message)
                    self._condition.notify_all()
        except (EndOfStream, ProtocolError):
            async with self._condition:
                self._condition.notify_all()

    async def _collect_stderr(self) -> None:
        assert self.process.stderr is not None
        while chunk := await self.process.stderr.read(64 * 1024):
            self._stderr.extend(chunk)

    async def _wait_for_exit(self) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._shutdown_timeout
        while self.process.returncode is None:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.01, remaining))
        await self.process.wait()
        return True

    async def _finish_collectors(self) -> None:
        collectors = (self._collector, self._stderr_collector)
        _, pending = await asyncio.wait(collectors, timeout=self._shutdown_timeout)
        for task in pending:
            task.cancel()
        await asyncio.gather(*collectors, return_exceptions=True)

    async def _with_diagnostics(
        self,
        awaitable: object,
        description: str,
        timeout: float,
    ) -> dict[str, object]:
        try:
            async with asyncio.timeout(timeout):
                return await awaitable  # type: ignore[misc]
        except TimeoutError as error:
            raise AssertionError(
                f"Timed out waiting for {description}; "
                f"stderr={self.stderr!r}; messages={self.messages!r}"
            ) from error

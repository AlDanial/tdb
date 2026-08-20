"""Bridge tdb's stdio DAP transport to the TCP DAP endpoint in ``rdbg``.

The ``debug`` gem owns the Ruby debuggee and exposes DAP only after
``rdbg --open`` has launched it.  tdb, however, starts a stdio DAP adapter
before sending ``launch``.  This bridge performs that lifecycle translation:
it acknowledges tdb's initialize request, starts (or connects to) rdbg on
launch/attach, then relays the remainder of the DAP session unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import socket
import sys
from pathlib import Path
from typing import Any

from tdb.dap.protocol import encode_message, read_message


_BOOTSTRAP_CAPABILITIES = {
    "supportsConfigurationDoneRequest": True,
    "supportsConditionalBreakpoints": True,
    "supportsFunctionBreakpoints": True,
    "supportsTerminateRequest": True,
    "supportTerminateDebuggee": True,
    "exceptionBreakpointFilters": [
        {"filter": "any", "label": "rescue any exception"},
        {"filter": "RuntimeError", "label": "rescue RuntimeError"},
    ],
}


async def _read_remote_message(reader: asyncio.StreamReader) -> dict[str, Any]:
    """Read one DAP frame from rdbg, tolerating its interleaved console lines.

    rdbg's DAP socket carries the debug-console line protocol (`out ...`,
    `input ...` plain-text lines) on the same connection as
    Content-Length-framed DAP messages — for example a source listing is
    emitted every time the debuggee stops.  ``dap.protocol.read_message``
    assumes a pure DAP stream, so it mis-parses that mixture; skip every
    line that isn't a ``Content-Length`` header and parse the next frame.
    """
    while True:
        header = await reader.readline()
        if not header:
            raise ConnectionError("DAP stream closed")
        if not header.startswith(b"Content-Length:"):
            continue
        separator = await reader.readexactly(2)
        if separator != b"\r\n":
            continue  # malformed frame; resync on the next header line
        length = int(header.split(b":", 1)[1].strip())
        body = await reader.readexactly(length)
        return json.loads(body.decode("utf-8"))


class _StdioWriter:
    """The small subset of StreamWriter needed for framed stdout writes."""

    def write(self, data: bytes) -> None:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    async def drain(self) -> None:
        return None


class RubyDapBridge:
    def __init__(self, rdbg: str | None) -> None:
        self._rdbg = rdbg
        self._remote_reader: asyncio.StreamReader | None = None
        self._remote_writer: asyncio.StreamWriter | None = None
        self._ruby_process: asyncio.subprocess.Process | None = None
        self._remote_task: asyncio.Task[None] | None = None
        self._remote_closed = False
        self._output_tasks: list[asyncio.Task[None]] = []
        self._exit_watch_task: asyncio.Task[None] | None = None
        self._event_seq = 0

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @staticmethod
    async def _write(writer: Any, message: dict[str, Any]) -> None:
        writer.write(encode_message(message))
        await writer.drain()

    async def _connect(self, host: str, port: int) -> None:
        last_error: OSError | None = None
        # The first invocation of rdbg can load native Ruby dependencies and
        # take noticeably longer than a warm launch.
        for _ in range(300):
            try:
                self._remote_reader, self._remote_writer = await asyncio.open_connection(
                    host, port
                )
                return
            except OSError as exc:
                last_error = exc
                await asyncio.sleep(0.05)
        raise ConnectionError(f"rdbg did not open {host}:{port}: {last_error}")

    async def _launch_rdbg(self, arguments: dict[str, Any], client_writer: Any) -> None:
        rdbg = self._rdbg or shutil.which("rdbg")
        if rdbg is None:
            raise FileNotFoundError(
                "rdbg not found on PATH — install the Ruby debug gem with `gem install debug`, "
                'or set {"adapters": {"rdbg": "/path/to/rdbg"}} in tdb config.json'
            )
        script = arguments.get("program")
        if not isinstance(script, str) or not script:
            raise ValueError("Ruby launch requires a program path")
        args = arguments.get("args", [])
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ValueError("Ruby launch args must be a list of strings")
        port = arguments.get("debugPort") or self._free_port()
        if not isinstance(port, int) or not 0 < port < 65536:
            raise ValueError("debugPort must be an integer from 1 through 65535")
        command = [rdbg, "--open", "--port", str(port)]
        # Do NOT pass --nonstop here: in that mode rdbg runs the program to
        # completion without waiting for a debugger to attach, so a fast
        # script exits before this bridge can even connect. Stop-on-entry is
        # instead controlled through the DAP attach request's `nonstop`
        # argument (see _start_session), which rdbg applies after the
        # debugger is attached.
        if arguments.get("useBundler"):
            command.extend(["-c", "--", "bundle", "exec", "ruby", script, *args])
        else:
            command.extend([script, *args])
        environment = os.environ.copy()
        env = arguments.get("env")
        if isinstance(env, dict) and all(
            isinstance(key, str) and isinstance(value, str) for key, value in env.items()
        ):
            environment.update(env)
        # Ruby's STDOUT is fully buffered by default when it's a pipe (as
        # here: the bridge reads rdbg's stdout pipe to relay program output
        # as DAP `output` events), so `puts` output would only reach the
        # user at process exit.  Inject a tiny `-r` helper via RUBYOPT that
        # sets STDOUT.sync = true so program output streams live.
        _sync_helper = str(Path(__file__).resolve().parent / "stdout_sync.rb")
        _rubyopt = environment.get("RUBYOPT", "").strip()
        _rubyopt = f"{_rubyopt} -r{_sync_helper}".strip()
        environment["RUBYOPT"] = _rubyopt
        # rdbg execs the debuggee in place, so its stdout/stderr carry the
        # program's own output. Capture them (never inherit — the bridge's
        # own stdout is the DAP pipe to tdb) and forward each as a DAP
        # `output` event so program output reaches the user.
        self._ruby_process = await asyncio.create_subprocess_exec(
            *command,
            cwd=arguments.get("cwd") or None,
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert self._ruby_process.stdout is not None
        assert self._ruby_process.stderr is not None
        self._output_tasks = [
            asyncio.create_task(
                self._forward_stream(
                    self._ruby_process.stdout, "stdout", client_writer
                )
            ),
            asyncio.create_task(
                self._forward_stream(
                    self._ruby_process.stderr, "stderr", client_writer
                )
            ),
        ]
        # rdbg never sends a DAP `exited` event, so tdb would see no exit
        # code.  The bridge owns the process (rdbg `exec`s the debuggee in
        # place), so synthesize `exited` from its real return code.
        self._exit_watch_task = asyncio.create_task(self._watch_exit(client_writer))
        await self._connect("127.0.0.1", port)

    async def _initialize_remote(self, client_writer: Any) -> None:
        assert self._remote_reader is not None and self._remote_writer is not None
        await self._write(
            self._remote_writer,
            {
                "seq": 1,
                "type": "request",
                "command": "initialize",
                "arguments": {
                    "clientID": "tdb",
                    "clientName": "tdb",
                    "adapterID": "rdbg",
                    "pathFormat": "path",
                    "linesStartAt1": True,
                    "columnsStartAt1": True,
                },
            },
        )
        while True:
            message = await _read_remote_message(self._remote_reader)
            if message.get("type") == "response" and message.get("request_seq") == 1:
                break
            await self._write(client_writer, message)

    async def _relay_remote(self, client_writer: Any) -> None:
        assert self._remote_reader is not None
        try:
            while True:
                message = await _read_remote_message(self._remote_reader)
                await self._write(client_writer, message)
        except (ConnectionError, asyncio.IncompleteReadError):
            self._remote_closed = True
            return

    async def _emit_output(
        self, client_writer: Any, category: str, data: bytes
    ) -> None:
        self._event_seq += 1
        await self._write(
            client_writer,
            {
                "seq": self._event_seq,
                "type": "event",
                "event": "output",
                "body": {
                    "category": category,
                    "output": data.decode("utf-8", errors="replace"),
                },
            },
        )

    async def _watch_exit(self, client_writer: Any) -> None:
        """Synthesize a DAP ``exited`` event once the debuggee process ends.

        rdbg reports program completion with a ``terminated`` event only; the
        real exit code is the process return code (rdbg ``exec``s the
        debuggee).  This task awaits the process, then emits ``exited`` so
        tdb's ``on_exited`` records the actual code.  On bridge shutdown the
        task is cancelled before the process is terminated, so a forced kill
        never surfaces as a bogus ``exited``.
        """
        assert self._ruby_process is not None
        await self._ruby_process.wait()
        returncode = self._ruby_process.returncode
        if returncode is None:
            return
        self._event_seq += 1
        with contextlib.suppress(ConnectionError, BrokenPipeError):
            await self._write(
                client_writer,
                {
                    "seq": self._event_seq,
                    "type": "event",
                    "event": "exited",
                    "body": {"exitCode": returncode},
                },
            )

    async def _forward_stream(
        self,
        reader: asyncio.StreamReader,
        category: str,
        client_writer: Any,
    ) -> None:
        """Forward a debuggee stdout/stderr pipe as DAP ``output`` events.

        Lines are delivered as they complete; a trailing partial line (no
        newline before EOF) is flushed when the pipe closes.
        """
        buffer = b""
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    await self._emit_output(client_writer, category, line + b"\n")
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        if buffer:
            await self._emit_output(client_writer, category, buffer)

    async def _start_session(
        self, command: str, request: dict[str, Any], client_writer: Any
    ) -> None:
        arguments = request.get("arguments") or {}
        if command == "launch":
            await self._launch_rdbg(arguments, client_writer)
        else:
            host, port = arguments.get("host", "127.0.0.1"), arguments.get("port")
            if not isinstance(host, str) or not isinstance(port, int):
                raise ValueError("Ruby attach requires host and port")
            await self._connect(host, port)
        await self._initialize_remote(client_writer)
        assert self._remote_writer is not None
        # rdbg has already launched the debuggee; its DAP endpoint always uses attach.
        remote_request = dict(request)
        remote_request["command"] = "attach"
        # rdbg defaults to treating DAP source paths as remote.  The launch
        # bridge runs the debuggee on the same host, so opt into the
        # vscode-rdbg equivalent of local filesystem mode.
        remote_arguments: dict[str, Any] = {
            "type": "rdbg",
            "request": "attach",
            "localfs": True,
        }
        if command == "launch":
            # rdbg's `attach` `nonstop` argument (not the CLI flag) is what
            # decides whether the debuggee stops at entry once the debugger
            # is attached: true → the program keeps running (rdbg `continue`s
            # on configurationDone), false/omitted → rdbg emits `stopped` at
            # the first line.  Mirror the launch body's `stopOnEntry`.
            remote_arguments["nonstop"] = not arguments.get("stopOnEntry", True)
        remote_request["arguments"] = remote_arguments
        await self._write(self._remote_writer, remote_request)
        self._remote_task = asyncio.create_task(self._relay_remote(client_writer))

    async def run(
        self,
        reader: asyncio.StreamReader | None = None,
        writer: Any | None = None,
    ) -> None:
        if reader is None:
            stdin_reader = asyncio.StreamReader()
            protocol = asyncio.StreamReaderProtocol(stdin_reader)
            await asyncio.get_running_loop().connect_read_pipe(
                lambda: protocol, sys.stdin.buffer
            )
        else:
            stdin_reader = reader
        client_writer = writer if writer is not None else _StdioWriter()
        try:
            while True:
                try:
                    request = await read_message(stdin_reader)
                except (ConnectionError, asyncio.IncompleteReadError):
                    # tdb closed the stdio pipe (normal shutdown) — exit
                    # the loop without a traceback.
                    return
                command = request.get("command")
                if command == "initialize":
                    await self._write(
                        client_writer,
                        {
                            "seq": 1,
                            "type": "response",
                            "request_seq": request.get("seq"),
                            "command": "initialize",
                            "success": True,
                            "body": _BOOTSTRAP_CAPABILITIES,
                        },
                    )
                elif command in {"launch", "attach"} and self._remote_writer is None:
                    try:
                        await self._start_session(command, request, client_writer)
                    except Exception as exc:
                        await self._write(
                            client_writer,
                            {
                                "seq": 2,
                                "type": "response",
                                "request_seq": request.get("seq"),
                                "command": command,
                                "success": False,
                                "message": str(exc),
                            },
                        )
                elif self._remote_writer is not None and not self._remote_closed:
                    await self._write(self._remote_writer, request)
                elif self._remote_closed:
                    await self._write(
                        client_writer,
                        {
                            "seq": 2,
                            "type": "response",
                            "request_seq": request.get("seq"),
                            "command": command,
                            "success": False,
                            "message": "Ruby debuggee has terminated",
                        },
                    )
                else:
                    await self._write(
                        client_writer,
                        {
                            "seq": 2,
                            "type": "response",
                            "request_seq": request.get("seq"),
                            "command": command,
                            "success": False,
                            "message": "Ruby session has not been launched",
                        },
                    )
        finally:
            if self._remote_task:
                self._remote_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._remote_task
            for task in self._output_tasks:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if self._exit_watch_task:
                self._exit_watch_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._exit_watch_task
            if self._remote_writer:
                self._remote_writer.close()
                with contextlib.suppress(ConnectionError, BrokenPipeError):
                    await self._remote_writer.wait_closed()
            if self._ruby_process and self._ruby_process.returncode is None:
                self._ruby_process.terminate()
                await self._ruby_process.wait()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rdbg")
    args = parser.parse_args()
    asyncio.run(RubyDapBridge(args.rdbg).run())

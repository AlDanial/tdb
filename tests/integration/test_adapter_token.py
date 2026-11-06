"""The adapter behind `tdb -a` only accepts the program tdb injected into.

It waits on a local port, so anything on the machine can connect first. These
speak the protocol to a real adapter, as that local process would.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

import pytest

from tdb.languages import _python_adapter
from tdb.languages.python import PidAttachDebugpyAdapter
from tdb.languages.python_attach import TargetInfo

from .conftest import SRC_DIR, _free_port

TOKEN = "right-token"


def send(stream, message):
    body = json.dumps(message).encode()
    stream.write(b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
    stream.flush()


def receive(stream):
    """The next message, or None once the other side has closed."""
    length = None
    while True:
        line = stream.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":")[1])
    return json.loads(stream.read(length))


class Adapter:
    """A real adapter, waiting for a debuggee as it does during `tdb -a`."""

    def __init__(self, token=TOKEN):
        self.port = _free_port()
        env = {**os.environ, "PYTHONPATH": str(SRC_DIR)}
        env.pop(_python_adapter.TOKEN_VARIABLE, None)
        if token is not None:
            env[_python_adapter.TOKEN_VARIABLE] = token
        spec = PidAttachDebugpyAdapter(TargetInfo(1, "", (3, 12)), self.port)
        self.proc = subprocess.Popen(
            spec.command(),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def listen(self):
        send(
            self.proc.stdin,
            {
                "seq": 1,
                "type": "request",
                "command": "initialize",
                "arguments": {"adapterID": "test"},
            },
        )
        send(
            self.proc.stdin,
            {
                "seq": 2,
                "type": "request",
                "command": "attach",
                "arguments": {
                    "type": "debugpy",
                    "request": "attach",
                    "listen": {"host": "127.0.0.1", "port": self.port},
                },
            },
        )

    def connect(self):
        deadline = time.monotonic() + 15
        while True:
            try:
                sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
                return sock, sock.makefile("rwb")
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)

    def close(self):
        self.proc.kill()
        self.proc.wait()
        for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            pipe.close()


@pytest.fixture
def adapter():
    started = Adapter()
    started.listen()
    yield started
    started.close()


def answer_authorize(stream, token):
    request = receive(stream)
    assert request is not None and request["command"] == "pydevdAuthorize"
    send(
        stream,
        {
            "seq": 1,
            "type": "response",
            "request_seq": request["seq"],
            "success": True,
            "command": request["command"],
            "body": {"clientAccessToken": token},
        },
    )


@pytest.mark.parametrize("claimed", [None, "wrong-token"])
def test_adapter_drops_a_connection_without_the_token(adapter, claimed):
    sock, stream = adapter.connect()
    with sock, stream:
        answer_authorize(stream, claimed)
        assert receive(stream) is None


def test_adapter_goes_on_with_the_token(adapter):
    sock, stream = adapter.connect()
    with sock, stream:
        answer_authorize(stream, TOKEN)
        assert receive(stream)["command"] == "pydevdSystemInfo"


def test_program_can_still_connect_after_an_impostor(adapter):
    sock, stream = adapter.connect()
    with sock, stream:
        answer_authorize(stream, "wrong-token")
        assert receive(stream) is None
    sock, stream = adapter.connect()
    with sock, stream:
        answer_authorize(stream, TOKEN)
        assert receive(stream)["command"] == "pydevdSystemInfo"


def test_adapter_refuses_to_start_without_a_token():
    # Never fall back to an adapter that takes anyone.
    started = Adapter(token=None)
    try:
        assert started.proc.wait(timeout=15) != 0
        assert _python_adapter.TOKEN_VARIABLE in started.proc.stderr.read().decode()
    finally:
        started.close()


def test_token_never_reaches_the_command_line():
    spec = PidAttachDebugpyAdapter(TargetInfo(1, "", (3, 12)), 1)
    assert spec.token not in " ".join(spec.command())
    assert spec.environment() == {_python_adapter.TOKEN_VARIABLE: spec.token}


def test_each_attach_gets_its_own_token():
    target = TargetInfo(1, "", (3, 12))
    assert (
        PidAttachDebugpyAdapter(target, 1).token
        != PidAttachDebugpyAdapter(target, 1).token
    )


def test_already_debugged_still_recognises_the_adapter():
    # It looks for this in the command line of whatever a program connects to.
    spec = PidAttachDebugpyAdapter(TargetInfo(1, "", (3, 12)), 1)
    assert "debugpy.adapter" in spec.command()

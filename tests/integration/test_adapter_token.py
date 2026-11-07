"""The adapter behind `tdb -a` and the program prove themselves to each other.

The adapter waits on a local port, so anything on the machine can connect
first, and once it has gone anything can take that port. These speak the
handshake to a real adapter, as that local process would.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import resource
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tdb.languages import _python_adapter
from tdb.languages.python import PidAttachDebugpyAdapter
from tdb.languages.python_attach import TargetInfo

from .conftest import SRC_DIR

TOKEN = "ab" * 32
RIGHT_KEY = bytes.fromhex(TOKEN)


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

    def __init__(self, token=TOKEN, max_files=None):
        self.port = None
        env = {**os.environ, "PYTHONPATH": str(SRC_DIR)}
        env.pop(_python_adapter.TOKEN_VARIABLE, None)
        if token is not None:
            env[_python_adapter.TOKEN_VARIABLE] = token
        spec = PidAttachDebugpyAdapter(TargetInfo(1, "", (3, 12)))
        self.proc = subprocess.Popen(
            spec.command(),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=max_files
            and (lambda: resource.setrlimit(resource.RLIMIT_NOFILE, (max_files,) * 2)),
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
                    "listen": {"host": "127.0.0.1", "port": 0},
                },
            },
        )
        while True:
            message = receive(self.proc.stdout)
            if message.get("event") == "debugpyWaitingForServer":
                self.port = message["body"]["port"]
                return

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


def sign(key, label, nonces):
    return hmac.new(key, label + nonces, "sha256").digest()


def read_exactly(sock, size):
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def pose_as_target(sock, key):
    """The target's side of the handshake; True if the adapter proved itself.
    Written out here so that a bug in tdb's side can't hide from the test."""
    ours = os.urandom(32)
    sock.sendall(ours)
    reply = read_exactly(sock, 64)
    assert reply is not None
    nonces = ours + reply[:32]
    genuine = hmac.compare_digest(reply[32:], sign(RIGHT_KEY, b"adapter", nonces))
    sock.sendall(sign(key, b"target", nonces))
    return genuine


def test_adapter_drops_a_connection_without_the_key(adapter):
    sock, stream = adapter.connect()
    with sock, stream:
        pose_as_target(sock, b"wrong key")
        assert receive(stream) is None


def test_adapter_says_nothing_before_the_peer_speaks(adapter):
    # A secret sent first would go to anyone who connected.
    sock, _ = adapter.connect()
    with sock:
        sock.settimeout(0.5)
        with pytest.raises(TimeoutError):
            sock.recv(1)


def test_adapter_proves_itself_and_goes_on_with_the_key(adapter):
    sock, stream = adapter.connect()
    with sock, stream:
        assert pose_as_target(sock, RIGHT_KEY)
        assert receive(stream)["command"] == "pydevdSystemInfo"


def test_program_can_still_connect_after_an_impostor(adapter):
    sock, stream = adapter.connect()
    with sock, stream:
        pose_as_target(sock, b"wrong key")
        assert receive(stream) is None
    sock, stream = adapter.connect()
    with sock, stream:
        assert pose_as_target(sock, RIGHT_KEY)
        assert receive(stream)["command"] == "pydevdSystemInfo"


def test_silent_connection_does_not_hold_up_the_program(adapter):
    # It would otherwise block the only thread accepting connections.
    stalled, _ = adapter.connect()
    with stalled:
        sock, stream = adapter.connect()
        with sock, stream:
            assert pose_as_target(sock, RIGHT_KEY)
            assert receive(stream)["command"] == "pydevdSystemInfo"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX resource limits")
def test_running_out_of_descriptors_leaves_the_adapter_working():
    # accept() failing ends debugpy's accept thread for good.
    started = Adapter(max_files=64)
    started.listen()
    flood = []
    try:
        for _ in range(200):
            sock, _unused = started.connect()
            flood.append(sock)
        for sock in flood:
            sock.close()
        flood.clear()
        sock, stream = started.connect()
        with sock, stream:
            assert pose_as_target(sock, RIGHT_KEY)
            assert receive(stream)["command"] == "pydevdSystemInfo"
    finally:
        for sock in flood:
            sock.close()
        started.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX parent tracking")
def _running(pid: int) -> bool:
    # A container whose PID 1 reaps nothing keeps the dead adapter as a zombie.
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rpartition(")")[2].split()[0] != "Z"


def test_adapter_goes_when_tdb_dies():
    # Blocked waiting for a program, it never sees its client go, and would
    # sit on its port for good.
    spec = PidAttachDebugpyAdapter(TargetInfo(1, "", (3, 12)))
    parent = (
        "import json, os, subprocess, sys, time\n"
        "argv, env = json.loads(sys.argv[1])\n"
        "child = subprocess.Popen(argv, env=env, stdin=subprocess.PIPE,\n"
        "    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
        "    start_new_session=True)\n"
        "def send(message):\n"
        "    body = json.dumps(message).encode()\n"
        "    child.stdin.write(b'Content-Length: %d\\r\\n\\r\\n%s' % (len(body), body))\n"
        "    child.stdin.flush()\n"
        "send({'seq': 1, 'type': 'request', 'command': 'initialize',\n"
        "      'arguments': {'adapterID': 'test'}})\n"
        "send({'seq': 2, 'type': 'request', 'command': 'attach', 'arguments': {\n"
        "    'type': 'debugpy', 'request': 'attach',\n"
        "    'listen': {'host': '127.0.0.1', 'port': 0}}})\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(SRC_DIR), **spec.environment()}
    tdb = subprocess.Popen(
        [sys.executable, "-c", parent, json.dumps([spec.command(), env])],
        stdout=subprocess.PIPE,
        text=True,
    )
    pid = int(tdb.stdout.readline())
    try:
        time.sleep(1)
        assert os.path.exists(f"/proc/{pid}")
        tdb.kill()
        tdb.wait()
        deadline = time.monotonic() + 10
        while _running(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _running(pid), "adapter outlived tdb"
    finally:
        tdb.stdout.close()
        tdb.kill()
        tdb.wait()
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def test_adapter_refuses_to_start_without_a_token():
    # Never fall back to an adapter that takes anyone.
    started = Adapter(token=None)
    try:
        assert started.proc.wait(timeout=15) != 0
        assert _python_adapter.TOKEN_VARIABLE in started.proc.stderr.read().decode()
    finally:
        started.close()


def test_token_never_reaches_the_command_line():
    spec = PidAttachDebugpyAdapter(TargetInfo(1, "", (3, 12)))
    assert spec.token not in " ".join(spec.command())
    assert spec.environment() == {_python_adapter.TOKEN_VARIABLE: spec.token}


def test_each_attach_gets_its_own_token():
    target = TargetInfo(1, "", (3, 12))
    assert (
        PidAttachDebugpyAdapter(target).token
        != PidAttachDebugpyAdapter(target).token
    )


def test_already_debugged_still_recognises_the_adapter():
    # It looks for this in the command line of whatever a program connects to.
    spec = PidAttachDebugpyAdapter(TargetInfo(1, "", (3, 12)))
    assert "debugpy.adapter" in spec.command()

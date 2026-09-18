"""Devel::TdbRemote handshake probed with a raw TCP client."""

import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("perl") is None
    or subprocess.run(["perl", "-e", "require v5.18"]).returncode != 0,
    reason="perl >= 5.18 required",
)

PKG_DIR = Path(__file__).resolve().parents[2] / "src/tdb/adapters/perl"

SCRIPT = """\
use Devel::TdbRemote;
my $before = 40;
open my $fh, '>', $ARGV[1] or die;   # port-ready handshake file
Devel::TdbRemote::listen($ARGV[0], '127.0.0.1');
print {$fh} "listening\\n";
close $fh;
Devel::TdbRemote::wait_for_client();
my $after = $before + 2;
print "after=$after\\n";
"""

# Free-running loop after attach; `$done` is flipped by the test through
# the debugger once pause has landed, so the program can finish promptly.
LOOP_SCRIPT = """\
use Devel::TdbRemote;
open my $fh, '>', $ARGV[1] or die;
Devel::TdbRemote::listen($ARGV[0], '127.0.0.1');
print {$fh} "listening\\n";
close $fh;
Devel::TdbRemote::wait_for_client();
our $done = 0;
my $n = 0;
until ($done) {
    select(undef, undef, undef, 0.05);
    $n++;
    last if $n > 400;
}
print "loops=$n done=$done\\n";
"""


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start(tmp_path, script: str):
    prog = tmp_path / "remote_prog.pl"
    prog.write_text(script)
    ready = tmp_path / "ready"
    port = _free_port()
    proc = subprocess.Popen(
        ["perl", f"-I{PKG_DIR}", str(prog), str(port), str(ready)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    for _ in range(100):
        if ready.exists() and ready.read_text().startswith("listening"):
            break
        time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail(f"never listened; stderr={proc.stderr.read()}")
    return proc, port


def _read_until(sock: socket.socket, marker: bytes) -> bytes:
    buf = b""
    while marker not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise AssertionError(f"socket closed before {marker!r}; got {buf!r}")
        buf += chunk
    return buf


def test_wait_for_client_stops_and_serves_helpers(tmp_path):
    proc, port = _start(tmp_path, SCRIPT)
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        sock.settimeout(10)
        _read_until(sock, b"DB<")  # stopped: prompt arrives
        sock.sendall(b"Devel::TdbHelper::location()\n")
        buf = _read_until(sock, b"<<<TDB")  # helpers were preloaded
        assert b'"version":1' in buf.replace(b" ", b"")
        sock.sendall(b"c\n")  # detach-ish: let it finish
        out, _ = proc.communicate(timeout=15)
        assert "after=42" in out
    finally:
        if proc.poll() is None:
            proc.kill()


def test_control_channel_pauses_free_running_program(tmp_path):
    proc, port = _start(tmp_path, LOOP_SCRIPT)
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        sock.settimeout(10)
        _read_until(sock, b"DB<")
        # Second connection on the same port is the control channel; it is
        # only accepted once the debugger command asks for it.
        ctl = socket.create_connection(("127.0.0.1", port), timeout=10)
        sock.sendall(b"Devel::TdbRemote::arm_control()\n")
        buf = _read_until(sock, b"DB<")
        assert b'"control":1' in buf.replace(b" ", b""), buf
        sock.sendall(b"c\n")
        time.sleep(0.3)  # let the loop actually run free
        ctl.sendall(b"p")
        _read_until(sock, b"DB<")  # paused: an unsolicited prompt
        sock.sendall(b"$done = 1\n")
        _read_until(sock, b"DB<")
        sock.sendall(b"c\n")
        out, _ = proc.communicate(timeout=15)
        assert "done=1" in out, out
        loops = int(out.split("loops=")[1].split()[0])
        assert loops < 400, out
    finally:
        if proc.poll() is None:
            proc.kill()

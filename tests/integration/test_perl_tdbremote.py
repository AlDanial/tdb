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


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_wait_for_client_stops_and_serves_helpers(tmp_path):
    prog = tmp_path / "remote_prog.pl"
    prog.write_text(SCRIPT)
    ready = tmp_path / "ready"
    port = _free_port()
    proc = subprocess.Popen(
        ["perl", f"-I{PKG_DIR}", str(prog), str(port), str(ready)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(100):
            if ready.exists() and ready.read_text().startswith("listening"):
                break
            time.sleep(0.1)
        else:
            proc.kill()
            pytest.fail(f"never listened; stderr={proc.stderr.read()}")
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        sock.settimeout(10)
        buf = b""
        while b"DB<" not in buf:  # stopped: prompt arrives
            buf += sock.recv(4096)
        sock.sendall(b"Devel::TdbHelper::location()\n")
        buf = b""
        while b"<<<TDB" not in buf:  # helpers were preloaded
            buf += sock.recv(4096)
        assert b'"version":1' in buf.replace(b" ", b"")
        sock.sendall(b"c\n")  # detach-ish: let it finish
        out, _ = proc.communicate(timeout=15)
        assert "after=42" in out
    finally:
        if proc.poll() is None:
            proc.kill()

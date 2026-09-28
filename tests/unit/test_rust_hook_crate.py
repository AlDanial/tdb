"""rust/tdb: the Rust live breakpoint hook crate. Builds with plain rustc
(no cargo in CI), exports the shared C stop symbol, and is a no-op without
a tty."""

from __future__ import annotations

import os
import pty
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CRATE = REPO / "rust" / "tdb" / "src" / "lib.rs"
RUSTC = shutil.which("rustc")

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or RUSTC is None, reason="needs Linux and rustc"
)

MAIN = """\
fn main() {
    let counter = 10;
    tdb::breakpoint();
    println!("counter = {}", counter + 1);
}
"""


def build_with_crate(tmp_path: Path, main_src: str) -> Path:
    rlib = tmp_path / "libtdb.rlib"
    subprocess.run(
        [
            RUSTC,
            "--edition",
            "2021",
            "--crate-type",
            "lib",
            "--crate-name",
            "tdb",
            "-g",
            "-C",
            "opt-level=0",
            "-o",
            str(rlib),
            str(CRATE),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    (tmp_path / "main.rs").write_text(main_src)
    exe = tmp_path / "prog"
    subprocess.run(
        [
            RUSTC,
            "--edition",
            "2021",
            "-g",
            "-C",
            "opt-level=0",
            "-L",
            str(tmp_path),
            "--extern",
            f"tdb={rlib}",
            "-o",
            str(exe),
            str(tmp_path / "main.rs"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return exe


def _stderr_text(proc: subprocess.Popen) -> str:
    """Whatever the debuggee has written to stderr so far. Non-blocking:
    the real tdb (and any fake stand-in it might spawn) inherits the
    debuggee's stderr, so the pipe never reaches EOF while a spawned tdb
    is still alive -- a blocking read/communicate() would hang until it
    exits."""
    fd = proc.stderr.fileno()
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


def test_crate_builds_without_dependencies_and_exports_stop_symbol(tmp_path):
    exe = build_with_crate(tmp_path, MAIN)
    nm = subprocess.run(
        ["nm", str(exe)], capture_output=True, text=True, check=True
    ).stdout
    assert " T tdb_breakpoint_stop" in nm or " t tdb_breakpoint_stop" in nm


def test_rust_hook_noop_without_tty(tmp_path):
    exe = build_with_crate(tmp_path, MAIN)
    r = subprocess.run(
        [exe],
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "TDB": "/nonexistent/tdb"},
    )
    assert r.returncode == 0
    assert r.stdout.strip() == "counter = 11"
    assert r.stderr == ""


def test_rust_hook_warns_when_tdb_missing(tmp_path):
    exe = build_with_crate(tmp_path, MAIN)
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [exe],
        stdin=slave,
        stdout=slave,
        stderr=subprocess.PIPE,
        env={**os.environ, "TDB": str(tmp_path / "no-such-tdb")},
    )
    os.close(slave)
    try:
        proc.wait(timeout=20)
        err = _stderr_text(proc)
    finally:
        os.close(master)
    assert proc.returncode == 0
    assert "tdb::breakpoint" in err and "cannot" in err

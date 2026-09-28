"""ocaml/tdb: the OCaml live breakpoint hook. Its tdb.h copy must match the
packaged header; the library builds with plain ocamlopt (no dune/ocamlfind
in CI) and is a no-op without a tty."""

from __future__ import annotations

import importlib.resources
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "ocaml" / "tdb"
PACKAGED_HEADER = importlib.resources.files("tdb.adapters.native") / "tdb.h"
OCAMLOPT = shutil.which("ocamlopt")

DEMO = """\
let () =
  let counter = ref 10 in
  Tdb.breakpoint ();
  counter := !counter + 1;
  Printf.printf "counter = %d\\n" !counter
"""


def test_header_copy_matches_packaged_header():
    assert (LIB / "tdb.h").read_bytes() == PACKAGED_HEADER.read_bytes(), (
        "ocaml/tdb/tdb.h must be byte-identical to src/tdb/adapters/native/tdb.h; "
        "copy the packaged header over it"
    )


def build_demo(tmp_path: Path, demo_src: str) -> Path:
    for name in ("tdb.ml", "tdb.mli", "tdb_stubs.c", "tdb.h"):
        shutil.copy(LIB / name, tmp_path / name)
    (tmp_path / "demo.ml").write_text(demo_src)
    exe = tmp_path / "demo"
    subprocess.run(
        [OCAMLOPT, "-g", "-o", str(exe), "tdb_stubs.c", "tdb.mli", "tdb.ml", "demo.ml"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    return exe


@pytest.mark.skipif(
    sys.platform != "linux" or OCAMLOPT is None, reason="needs Linux + ocamlopt"
)
def test_library_builds_with_plain_ocamlopt_and_is_noop_without_tty(tmp_path):
    exe = build_demo(tmp_path, DEMO)
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
    nm = subprocess.run(
        ["nm", str(exe)], capture_output=True, text=True, check=True
    ).stdout
    assert "tdb_breakpoint_stop" in nm and "tdb_ocaml_breakpoint" in nm


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


@pytest.mark.skipif(
    sys.platform != "linux" or OCAMLOPT is None, reason="needs Linux + ocamlopt"
)
def test_ocaml_hook_warns_when_tdb_missing(tmp_path):
    import pty

    exe = build_demo(tmp_path, DEMO)
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
        proc.kill()
    assert proc.returncode == 0
    assert "cannot" in err and "tdb" in err

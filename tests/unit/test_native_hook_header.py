"""tdb.h: the C/C++ live breakpoint hook. Compiles as C99 and C++, links
from two translation units (weak symbols), is a no-op without a tty, and
gives up with a warning when nothing attaches."""

from __future__ import annotations

import importlib.resources
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HEADER = importlib.resources.files("tdb.adapters.native") / "tdb.h"
CC = shutil.which("gcc") or shutil.which("cc")
CXX = shutil.which("g++") or shutil.which("c++")

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or CC is None, reason="needs Linux and a C compiler"
)

TWO_TU_A = """\
#include "tdb.h"
void other(void);
int main(void) { tdb_breakpoint(); other(); return 0; }
"""
TWO_TU_B = """\
#include "tdb.h"
void other(void) { tdb_breakpoint(); }
"""


def _build(tmp_path, sources: dict[str, str], compiler, *extra) -> str:
    for name, text in sources.items():
        (tmp_path / name).write_text(text)
    exe = tmp_path / "prog"
    cmd = [
        compiler,
        "-g",
        "-O0",
        "-std=c99" if compiler == CC else "-std=c++17",
        "-I",
        str(HEADER.parent),
        *extra,
        "-o",
        str(exe),
        *(str(tmp_path / n) for n in sources),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return str(exe)


def test_header_links_from_two_translation_units_c(tmp_path):
    _build(
        tmp_path, {"a.c": TWO_TU_A, "b.c": TWO_TU_B}, CC, "-Wall", "-Wextra", "-Werror"
    )


@pytest.mark.skipif(CXX is None, reason="no C++ compiler")
def test_header_compiles_as_cxx(tmp_path):
    _build(
        tmp_path,
        {"a.cpp": TWO_TU_A, "b.cpp": TWO_TU_B},
        CXX,
        "-Wall",
        "-Wextra",
        "-Werror",
    )


def _stderr_text(proc: subprocess.Popen) -> str:
    """Whatever the debuggee has written to stderr so far. Non-blocking:
    the real tdb (and our fake stand-in) inherits the debuggee's stderr,
    so the pipe never reaches EOF while a spawned tdb is still alive --
    a blocking read/communicate() would hang until it exits."""
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


def test_hook_is_noop_without_tty(tmp_path):
    exe = _build(tmp_path, {"a.c": TWO_TU_A, "b.c": TWO_TU_B}, CC)
    # stdin/stdout are pipes here, so the hook must return at once.
    r = subprocess.run(
        [exe],
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "TDB": "/nonexistent/tdb"},
    )
    assert r.returncode == 0
    assert r.stderr == ""


def test_header_hook_times_out_when_no_attach(tmp_path):
    """A tty but nothing ever attaches (as under ptrace_scope=3): warn and
    continue. TDB points at a fake that just sleeps; the attach timeout is
    shrunk at compile time."""
    import pty

    fake = tmp_path / "fake_tdb"
    fake.write_text("#!/bin/sh\nexec sleep 30\n")
    fake.chmod(0o755)
    exe = _build(
        tmp_path,
        {"a.c": TWO_TU_A, "b.c": TWO_TU_B},
        CC,
        "-DTDB_ATTACH_TIMEOUT_MS=300",
        "-DTDB_LINGER_TIMEOUT_MS=100",
    )
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [exe],
        stdin=slave,
        stdout=slave,
        stderr=subprocess.PIPE,
        env={**os.environ, "TDB": str(fake)},
    )
    os.close(slave)
    try:
        proc.wait(timeout=20)
        err = _stderr_text(proc)
    finally:
        os.close(master)
        subprocess.run(["pkill", "-f", str(fake)], check=False)
    assert proc.returncode == 0
    assert "did not attach" in err


def test_header_warns_when_tdb_missing(tmp_path):
    import pty

    exe = _build(tmp_path, {"a.c": TWO_TU_A, "b.c": TWO_TU_B}, CC)
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
    assert "cannot" in err and "tdb" in err


def test_header_is_in_package_data():
    text = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text()
    assert "adapters/native/tdb.h" in text

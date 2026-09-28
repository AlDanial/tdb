"""Native live breakpoint hooks (tdb.h, rust/tdb, ocaml/tdb) end to end.

Each demo calls its hook twice. The demo runs under a pty with a fake tdb
(hook_harness) so we can check the spawn argv; then this test plays tdb's
part for real: build the language profile with attach_pid, attach through
DebugController with the real gdb / lldb-dap, expect the hook stop, step
out until the frame is the user's, check the line and a local, detach, and
repeat for the second call. Linux only (TracerPid, PR_SET_PTRACER).
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from tdb.languages.cpp import build_cpp_profile
from tdb.languages.ocaml import build_ocaml_profile
from tdb.languages.rust import build_rust_profile
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController
from tests.integration.hook_harness import HookDebuggee

REPO = Path(__file__).resolve().parents[2]
HEADER_DIR = REPO / "src" / "tdb" / "adapters" / "native"
RUST_LIB = REPO / "rust" / "tdb" / "src" / "lib.rs"
OCAML_LIB = REPO / "ocaml" / "tdb"
WAIT = 30.0


def _gdb_supports_dap() -> bool:
    gdb = shutil.which("gdb")
    if gdb is None:
        return False
    out = subprocess.run([gdb, "--version"], capture_output=True, text=True).stdout
    m = re.search(r"(\d+)\.\d+", out)
    return bool(m) and int(m.group(1)) >= 14


ADAPTERS = [
    a
    for a, ok in (
        ("gdb", _gdb_supports_dap()),
        ("lldb-dap", shutil.which("lldb-dap") is not None),
    )
    if ok
]

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not ADAPTERS,
    reason="native hooks need Linux and gdb>=14 or lldb-dap",
)

# Each program: counter starts 10, hook, += 1, += 20, hook, += 300, print.
# (lang_id, expected local name, values after 1st/2nd stop, line after each hook)
C_SRC = """\
#include "tdb.h"
#include <stdio.h>
int main(void) {
    int counter = 10;
    tdb_breakpoint();
    counter += 1;
    counter += 20;
    tdb_breakpoint();
    counter += 300;
    printf("counter = %d\\n", counter);
    return 0;
}
"""
CPP_SRC = C_SRC.replace("#include <stdio.h>", "#include <cstdio>")
RUST_SRC = """\
fn main() {
    let mut counter = 10;
    tdb::breakpoint();
    counter += 1;
    counter += 20;
    tdb::breakpoint();
    counter += 300;
    println!("counter = {}", counter);
}
"""
OCAML_SRC = """\
let () =
  let counter = ref 10 in
  Tdb.breakpoint ();
  counter := !counter + 1;
  counter := !counter + 20;
  Tdb.breakpoint ();
  counter := !counter + 300;
  Printf.printf "counter = %d\\n" !counter
"""

CASES = {
    # lang: (lang_id, source, lines after hooks, local name, values, toolchain)
    "c": (
        "cpp",
        C_SRC,
        (6, 9),
        "counter",
        ("10", "31"),
        shutil.which("gcc") or shutil.which("cc"),
    ),
    "cpp": (
        "cpp",
        CPP_SRC,
        (6, 9),
        "counter",
        ("10", "31"),
        shutil.which("g++") or shutil.which("c++"),
    ),
    "rust": ("rust", RUST_SRC, (4, 7), "counter", ("10", "31"), shutil.which("rustc")),
    # OCaml deviates from the others in two observed ways (gdb 17.1 and
    # lldb-dap 21.1.8, ocamlopt 5.4): the return address after
    # `Tdb.breakpoint ()` is still attributed to the call's own line, so
    # stepping out lands on lines 3 and 6; and ocamlopt emits no DWARF for
    # locals, so neither debugger shows `counter` (only registers). The
    # local check is skipped (None) for OCaml.
    "ocaml": ("ocaml", OCAML_SRC, (3, 6), None, None, shutil.which("ocamlopt")),
}


def _build(lang: str, tmp_path: Path) -> Path:
    lang_id, src, _, _, _, tool = CASES[lang]
    if tool is None:
        pytest.skip(f"no toolchain for {lang}")
    exe = tmp_path / f"hooked_{lang}"
    if lang in ("c", "cpp"):
        ext = "c" if lang == "c" else "cpp"
        s = tmp_path / f"hooked.{ext}"
        s.write_text(src)
        subprocess.run(
            [tool, "-g", "-O0", "-I", str(HEADER_DIR), "-o", str(exe), str(s)],
            check=True,
            capture_output=True,
            text=True,
        )
    elif lang == "rust":
        rlib = tmp_path / "libtdb.rlib"
        subprocess.run(
            [
                tool,
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
                str(RUST_LIB),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        s = tmp_path / "hooked.rs"
        s.write_text(src)
        subprocess.run(
            [
                tool,
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
                str(s),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        for name in ("tdb.ml", "tdb.mli", "tdb_stubs.c", "tdb.h"):
            shutil.copy(OCAML_LIB / name, tmp_path / name)
        (tmp_path / "hooked.ml").write_text(src)
        subprocess.run(
            [
                tool,
                "-g",
                "-o",
                str(exe),
                "tdb_stubs.c",
                "tdb.mli",
                "tdb.ml",
                "hooked.ml",
            ],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
    return exe


def _profile(lang_id: str, adapter: str, pid: int):
    build = {
        "cpp": build_cpp_profile,
        "rust": build_rust_profile,
        "ocaml": build_ocaml_profile,
    }[lang_id]
    return build(adapter=adapter, attach_pid=pid)


async def _attach_and_land(
    profile,
    exe: Path,
    pid: int,
    fake_tdb: int,
    want_line: int,
    local: str | None,
    value: str | None,
):
    """Attach for real, ride the hook stop out to the caller, check, detach.

    The fake tdb is killed only once the real attach has happened: the hook
    gives up ("tdb exited before attaching") if its tdb dies while no
    tracer is present yet, and starting gdb/lldb-dap takes a while."""
    handler = ServerEventHandler()
    ctrl = DebugController(handler, profile=profile)
    # Detach even when an assertion fails: a debuggee left ptrace-stopped
    # by a live gdb/lldb-dap cannot be reaped, and HookDebuggee.close()
    # would then block forever in wait().
    try:
        await ctrl.remote_attach(
            host="127.0.0.1", port=0, program=str(exe), pre_arm_pause=False
        )
        await asyncio.wait_for(handler.initialized_event.wait(), WAIT)
        await ctrl.do_configure()
        # Only now, with the tracer in place (see docstring): we are tdb.
        os.kill(fake_tdb, signal.SIGKILL)
        await asyncio.wait_for(handler.stopped_event.wait(), WAIT)
        assert handler.last_stop_reason not in ("attach", "pause", "entry"), (
            handler.last_stop_reason
        )
        is_hook = profile.capabilities.breakpoint_hook_frame
        trail = []  # (frame name, line) at each stop, for failure messages
        for _ in range(8):
            await ctrl.fetch_stop_info()
            top = ctrl.state.stack_frames[0]
            trail.append((top.name, top.line))
            if not is_hook(top):
                break
            handler.stopped_event.clear()
            await ctrl.step_out()
            await asyncio.wait_for(handler.stopped_event.wait(), WAIT)
        else:
            pytest.fail(
                f"never left the hook frames: {[f.name for f in ctrl.state.stack_frames]}"
            )
        assert top.line == want_line, (trail, top.source)
        assert Path(top.source.path).name.startswith("hooked"), top.source
        names = {v.name: v.value for vs in ctrl.state.variables.values() for v in vs}
        if local is not None:
            assert local in names, names
        if value is not None:
            assert value in names[local], names[local]
    finally:
        await asyncio.wait_for(ctrl.stop(), WAIT)
    # Detached: alive and untraced.
    for _ in range(100):
        status = Path(f"/proc/{pid}/status").read_text()
        if re.search(r"TracerPid:\s+0\b", status):
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("debuggee still traced after detach")


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("lang", list(CASES))
async def test_hook_opens_tdb_twice_and_program_finishes(lang, adapter, tmp_path):
    lang_id, _, lines, local, values, _ = CASES[lang]
    exe = _build(lang, tmp_path)
    dbg = HookDebuggee(tmp_path, [str(exe)])
    try:
        argv = dbg.wait_spawn(1)
        assert argv == [
            "--lang",
            lang_id,
            "-a",
            str(dbg.proc.pid),
            "--no-pause-on-attach",
        ], argv
        vals = values or (None, None)
        await _attach_and_land(
            _profile(lang_id, adapter, dbg.proc.pid),
            exe,
            dbg.proc.pid,
            dbg.fake_pid(1),
            lines[0],
            local,
            vals[0],
        )
        # After detach the second hook call spawns a fresh tdb.
        argv2 = dbg.wait_spawn(2, timeout=WAIT)
        assert argv2 == argv
        await _attach_and_land(
            _profile(lang_id, adapter, dbg.proc.pid),
            exe,
            dbg.proc.pid,
            dbg.fake_pid(2),
            lines[1],
            local,
            vals[1],
        )
        out, err = dbg.finish(timeout=WAIT)
        assert dbg.proc.returncode == 0, (out, err)
        assert "counter = 331" in out, out
        assert "did not attach" not in err and "exited before" not in err, err
    finally:
        dbg.close()

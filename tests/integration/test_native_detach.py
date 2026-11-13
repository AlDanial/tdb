"""`q` then `d` in the TUI for native debuggers (gdb, lldb-dap, dlv): the
program keeps running under tdb's relay, and Ctrl-C reaches the program as
an ordinary SIGINT instead of being swallowed by the debugger.

Marker strings are built at run time (join) so the TUI source pane cannot
satisfy the asserts."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from . import detach_harness
from .conftest import FIXTURES_DIR
from .detach_harness import run_detached

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="needs a pty"),
    pytest.mark.xdist_group("pty"),
]

C_SRC = FIXTURES_DIR / "native_detach" / "prog.c"

# The fixture's first statement: the line lldb-dap stops on.
C_FIRST_LINE = next(
    n for n, text in enumerate(C_SRC.read_text().splitlines(), 1) if '"before"' in text
)

RUST_SRC = """\
use std::io::Write;
fn main() {
    let mode = std::env::args().nth(1).unwrap_or_default();
    println!("{}{}", "before", "-ran");
    if mode == "loop" {
        println!("{}{}", "looping", "-now");
        std::io::stdout().flush().unwrap();
        loop {
            std::thread::sleep(std::time::Duration::from_secs(1));
        }
    }
    println!("{}{}", "after-", "ran");
    std::process::exit(7);
}
"""

GO_SRC = """\
package main

import (
	"fmt"
	"os"
	"time"
)

func main() {
	fmt.Println("before" + "-ran")
	if len(os.Args) > 1 && os.Args[1] == "loop" {
		fmt.Println("looping" + "-now")
		for {
			time.Sleep(time.Second)
		}
	}
	fmt.Println("after-" + "ran")
	os.Exit(7)
}
"""

OCAML_SRC = """\
let () =
  print_endline ("before" ^ "-ran");
  if Array.length Sys.argv > 1 && Sys.argv.(1) = "loop" then begin
    print_endline ("looping" ^ "-now");
    while true do Unix.sleep 1 done
  end;
  print_endline ("after-" ^ "ran");
  exit 7
"""


def _gdb_dap() -> bool:
    gdb = shutil.which("gdb")
    if gdb is None:
        return False
    out = subprocess.run([gdb, "--version"], capture_output=True, text=True).stdout
    m = re.search(r"(\d+)\.\d+", out)
    return bool(m) and int(m.group(1)) >= 14


HAVE_GDB = _gdb_dap()
HAVE_LLDB = shutil.which("lldb-dap") is not None
HAVE_CC = shutil.which("gcc") is not None or shutil.which("cc") is not None
HAVE_RUSTC = shutil.which("rustc") is not None
HAVE_GO = shutil.which("go") is not None and shutil.which("dlv") is not None
HAVE_OCAML = shutil.which("ocamlfind") is not None or shutil.which("ocamlopt") is not None


def _build_c(d: Path) -> str:
    out = d / "cprog"
    cc = shutil.which("gcc") or shutil.which("cc")
    subprocess.run([cc, "-g", "-O0", "-o", str(out), str(C_SRC)], check=True)
    return str(out)


def _build_rust(d: Path) -> str:
    src = d / "rprog.rs"
    src.write_text(RUST_SRC)
    out = d / "rprog"
    subprocess.run(["rustc", "-g", "-o", str(out), str(src)], check=True)
    return str(out)


def _build_go(d: Path) -> str:
    src = d / "main.go"
    src.write_text(GO_SRC)
    return str(src)


def _build_ocaml(d: Path) -> str:
    src = d / "oprog.ml"
    src.write_text(OCAML_SRC)
    out = d / "oprog"
    subprocess.run(
        ["ocamlfind", "ocamlopt", "-package", "unix", "-linkpkg", "-g",
         "-o", str(out), str(src)]
        if shutil.which("ocamlfind")
        else ["ocamlopt", "-g", "-I", "+unix", "unix.cmxa", "-o", str(out), str(src)],
        cwd=d,
        check=True,
    )
    return str(out)


def _stop_at(program: str, line: int) -> list[str]:
    """lldb-dap's entry stop is in the dynamic loader, with no source and
    an exception modal; a breakpoint on the program's first line gives it
    the ordinary stopped TUI the detach keys expect."""
    src = {"cprog": str(C_SRC), "rprog": "rprog.rs", "oprog": "oprog.ml"}[Path(program).name]
    return ["-k", f"{src}:{line}"]


# id -> (available, builder, tdb options, first line of main's body)
CASES = {
    "c-gdb": (HAVE_GDB and HAVE_CC, _build_c, ["--lang", "cpp", "--adapter", "gdb"], None),
    "c-lldb": (HAVE_LLDB and HAVE_CC, _build_c, ["--lang", "cpp", "--adapter", "lldb-dap"], C_FIRST_LINE),
    "rust-gdb": (HAVE_GDB and HAVE_RUSTC, _build_rust, ["--lang", "rust", "--adapter", "gdb"], None),
    "go-dlv": (HAVE_GO, _build_go, [], None),
    "ocaml-gdb": (HAVE_GDB and HAVE_OCAML, _build_ocaml, ["--lang", "ocaml", "--adapter", "gdb"], None),
    "ocaml-lldb": (HAVE_LLDB and HAVE_OCAML, _build_ocaml, ["--lang", "ocaml", "--adapter", "lldb-dap"], 2),
    "rust-lldb": (HAVE_LLDB and HAVE_RUSTC, _build_rust, ["--lang", "rust", "--adapter", "lldb-dap"], 4),
}


@pytest.fixture(params=sorted(CASES))
def lang(request, tmp_path):
    available, build, opts, line = CASES[request.param]
    if not available:
        pytest.skip(f"toolchain for {request.param} not installed")
    program = build(tmp_path)
    return {
        "program": program,
        "opts": opts + (_stop_at(program, line) if line else []),
        "kw": {
            "cwd": tmp_path,
            "env": {"TDB_LOG_DIR": str(tmp_path), "TDB_CONFIG_DIR": str(tmp_path)},
            "timeout": 60,
        },
    }


def test_native_detach_output_and_exit_code(lang):
    out, code = run_detached([*lang["opts"], lang["program"]], **lang["kw"])
    assert "tdb: detached" in out
    assert "after-ran" in out
    assert code == 7
    assert detach_harness.leftovers == []


def test_native_detach_ctrl_c_ends_loop(lang):
    out, code = run_detached(
        [*lang["opts"], lang["program"], "loop"], interrupt_after=1.5, **lang["kw"]
    )
    assert "looping-now" in out
    assert code == 130
    assert detach_harness.leftovers == []


@pytest.mark.skipif(not (HAVE_GDB and HAVE_CC), reason="needs gdb >= 14 and a C compiler")
def test_gdb_detach_ctrl_c_reaches_program_handler(tmp_path):
    prog = _build_c(tmp_path)
    out, code = run_detached(
        ["--lang", "cpp", "--adapter", "gdb", prog, "handler"],
        interrupt_after=1.5,
        cwd=tmp_path,
        env={"TDB_LOG_DIR": str(tmp_path), "TDB_CONFIG_DIR": str(tmp_path)},
        timeout=60,
    )
    assert "caught-int" in out
    assert code == 5
    assert detach_harness.leftovers == []


@pytest.mark.parametrize("case", ["go-dlv", "c-gdb", "ocaml-gdb"])
def test_native_detach_function_entry_breakpoint_cleared(case, tmp_path):
    """Go's `main.main` function breakpoint and gdb's bootstrap stop must not
    stop the program again once `d` was pressed."""
    available, build, opts, _line = CASES[case]
    if not available:
        pytest.skip(f"toolchain for {case} not installed")
    out, code = run_detached(
        [*opts, build(tmp_path)],
        cwd=tmp_path,
        env={"TDB_LOG_DIR": str(tmp_path), "TDB_CONFIG_DIR": str(tmp_path)},
        timeout=60,
    )
    assert "after-ran" in out
    assert code == 7
    log = (tmp_path / "tdb.log").read_text()
    after_quit = log.split("on_code_view_debug_action called: quit")[-1]
    assert "Request: continue" in after_quit
    assert "Event: stopped" not in after_quit
    assert detach_harness.leftovers == []


@pytest.mark.skipif(not (HAVE_GDB and HAVE_CC), reason="needs gdb >= 14 and a C compiler")
def test_gdb_detach_slow_clean_exit_after_ctrl_c_keeps_exit_code_0(tmp_path):
    """gdb reports exit 0 both for a SIGINT death and a clean exit; only an
    exit right after Ctrl-C is taken for the death. A handler that cleans up
    for 3s (> the grace window) and exits 0 stays 0."""
    prog = _build_c(tmp_path)
    out, code = run_detached(
        ["--lang", "cpp", "--adapter", "gdb", prog, "slow0"],
        interrupt_after=1.5,
        cwd=tmp_path,
        env={"TDB_LOG_DIR": str(tmp_path), "TDB_CONFIG_DIR": str(tmp_path)},
        timeout=60,
    )
    assert "caught-int" in out
    assert code == 0
    assert detach_harness.leftovers == []

"""--config-dir keeps every write out of $HOME (tdb.tool_env).

For each installed toolchain, run a tiny `tdb --config-dir CFG -e ...`
session (breakpoint, evaluate, continue to exit) as a subprocess with
HOME pointed at a temp dir, twice:

* ``readonly`` — HOME is ``chmod 0o500``: the session must still succeed
  (e.g. ``go build`` needs a writable GOCACHE).
* ``writable`` — HOME is writable but must still be empty afterwards;
  this is what actually catches a tool writing under HOME (a read-only
  HOME only makes such writes fail, often silently).
"""

from __future__ import annotations

import os
import shutil
import signal
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tdb.tool_env import tool_redirects
from tests.integration.bash_adapter_harness import bash_ok
from tests.integration.powershell_adapter_harness import pwsh_ok
from tests.integration.ruby_adapter_harness import rdbg_ok

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX HOME permissions"),
    pytest.mark.skipif(
        sys.platform != "win32" and os.geteuid() == 0,
        reason="root ignores the read-only HOME",
    ),
]

REPO_SRC = Path(__file__).resolve().parent.parent.parent / "src"
RUN_TIMEOUT = 90.0


def _gdb_dap_ok() -> bool:
    gdb = shutil.which("gdb")
    if gdb is None:
        return False
    try:
        out = subprocess.run(
            [gdb, "--version"], capture_output=True, text=True, timeout=30
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    import re

    m = re.search(r"(\d+)\.\d+", out)
    return bool(m) and int(m.group(1)) >= 14


def _perl_ok() -> bool:
    if shutil.which("perl") is None:
        return False
    try:
        return (
            subprocess.run(["perl", "-e", "require v5.18"], timeout=30).returncode == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def _need(ok: bool, why: str):
    return pytest.mark.skipif(not ok, reason=why)


# name -> (source file name, source, eval line, eval expr, build argv or
# None, program to debug (relative), extra tdb args). Every program
# prints "y 2".
CASES = {
    "python": ("t.py", "x = 1\ny = x + 1\nprint('y', y)\n", 3, "x", None, "t.py", []),
    "perl": (
        "t.pl",
        'my $x = 1;\nmy $y = $x + 1;\nprint "y $y\\n";\n',
        3,
        "$x",
        None,
        "t.pl",
        [],
    ),
    "ruby": ("t.rb", 'x = 1\ny = x + 1\nputs "y #{y}"\n', 3, "x", None, "t.rb", []),
    "bash": ("t.sh", 'x=1\ny=$((x+1))\necho "y $y"\n', 3, "$x", None, "t.sh", []),
    "tcsh": (
        "t.csh",
        'set x = 1\n@ y = $x + 1\necho "y $y"\n',
        3,
        "$x",
        None,
        "t.csh",
        [],
    ),
    "powershell": (
        "t.ps1",
        '$x = 1\n$y = $x + 1\nWrite-Output "y $y"\n',
        3,
        "$x",
        None,
        "t.ps1",
        [],
    ),
    "go": (
        "main.go",
        'package main\n\nimport "fmt"\n\nfunc main() {\n\tx := 1\n\ty := x + 1\n\tfmt.Println("y", y)\n}\n',
        8,
        "x",
        None,
        "main.go",
        [],
    ),
    "c-gdb": (
        "t.c",
        '#include <stdio.h>\nint main(void) {\n    int x = 1;\n    int y = x + 1;\n    printf("y %d\\n", y);\n    return 0;\n}\n',
        5,
        "x",
        ["gcc", "-g", "-O0", "-o", "prog", "t.c"],
        "prog",
        ["--adapter", "gdb"],
    ),
    "c-lldb-dap": (
        "t.c",
        '#include <stdio.h>\nint main(void) {\n    int x = 1;\n    int y = x + 1;\n    printf("y %d\\n", y);\n    return 0;\n}\n',
        5,
        "x",
        ["gcc", "-g", "-O0", "-o", "prog", "t.c"],
        "prog",
        ["--adapter", "lldb-dap"],
    ),
    "rust": (
        "t.rs",
        'fn main() {\n    let x = 1;\n    let y = x + 1;\n    println!("y {}", y);\n}\n',
        4,
        "x",
        ["rustc", "-g", "-o", "prog", "t.rs"],
        "prog",
        [],
    ),
    "ocaml": (
        "t.ml",
        'let () =\n  let x = 1 in\n  let y = x + 1 in\n  Printf.printf "y %d\\n" y\n',
        4,
        "x",
        ["ocamlopt", "-g", "-o", "prog", "t.ml"],
        "prog",
        [],
    ),
}

MARKS = {
    "python": [],
    "perl": [_need(_perl_ok(), "perl >= 5.18 required")],
    "ruby": [_need(rdbg_ok(), "needs rdbg (debug gem >= 1.9)")],
    "bash": [_need(bash_ok(), "needs bash >= 4.4")],
    "tcsh": [_need(shutil.which("tcsh") is not None, "stock tcsh is not installed")],
    "powershell": [_need(pwsh_ok(), "pwsh and PSES required")],
    "go": [
        _need(
            shutil.which("go") is not None and shutil.which("dlv") is not None,
            "go and dlv required",
        )
    ],
    "c-gdb": [
        _need(
            _gdb_dap_ok() and shutil.which("gcc") is not None,
            "gdb >= 14 and gcc required",
        )
    ],
    "c-lldb-dap": [
        _need(
            shutil.which("lldb-dap") is not None and shutil.which("gcc") is not None,
            "lldb-dap and gcc required",
        )
    ],
    "rust": [
        _need(
            shutil.which("rustc") is not None and _gdb_dap_ok(),
            "rustc and gdb >= 14 required",
        )
    ],
    "ocaml": [
        _need(
            shutil.which("ocamlopt") is not None
            and shutil.which("lldb-dap") is not None,
            "ocamlopt and lldb-dap are required",
        )
    ],
}

# Writes under HOME that have no redirect variable short of one tdb must
# never set (tdb.tool_env never relocates XDG_CONFIG_HOME).
WRITABLE_XFAIL = {
    "go": "go's telemetry counters go to os.UserConfigDir()/go/telemetry "
    "(XDG_CONFIG_HOME); GOTELEMETRYDIR is not settable",
}


def _params():
    out = []
    for name in CASES:
        for mode in ("readonly", "writable"):
            marks = list(MARKS[name])
            if mode == "writable" and name in WRITABLE_XFAIL:
                marks.append(
                    pytest.mark.xfail(reason=WRITABLE_XFAIL[name], strict=True)
                )
            out.append(pytest.param(name, mode, marks=marks, id=f"{name}-{mode}"))
    return out


def _tree(root: Path) -> list[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        for n in dirnames + filenames:
            found.append(os.path.relpath(os.path.join(dirpath, n), root))
    return sorted(found)


def _descendants(pid: int) -> list[int]:
    """Linux: every live descendant of pid (adapters may setsid away from
    tdb's process group). Elsewhere: none (killpg covers the rest)."""
    children: dict[int, list[int]] = {}
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat_line = (entry / "stat").read_text()
        except OSError:
            continue
        ppid = int(stat_line.rsplit(")", 1)[1].split()[1])
        children.setdefault(ppid, []).append(int(entry.name))
    out, todo = [], [pid]
    while todo:
        for c in children.get(todo.pop(), []):
            out.append(c)
            todo.append(c)
    return out


def _marked(marker: str) -> list[int]:
    """Linux: live processes whose environment carries ``marker``. Finds
    children that setsid'd away and were reparented after tdb exited,
    which a descendant walk from tdb's (gone) pid no longer reaches."""
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    needle = f"{_MARKER_KEY}={marker}".encode()
    found = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            environ = (entry / "environ").read_bytes()
        except OSError:
            continue
        if needle in environ.split(b"\0"):
            found.append(int(entry.name))
    return found


def _kill_tree(proc: subprocess.Popen, marker: str) -> None:
    # Descendants only while tdb is unreaped: once reaped its pid may be
    # reused by an unrelated process.
    pids = _descendants(proc.pid) if proc.poll() is None else []
    for pid in {*pids, *_marked(marker)}:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    if proc.returncode is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    proc.wait(timeout=10)


_MARKER_KEY = "TDB_READONLY_HOME_TEST"


@pytest.fixture
def home(tmp_path, request):
    h = tmp_path / "home"
    h.mkdir()
    yield h
    os.chmod(h, stat.S_IRWXU)


@pytest.mark.parametrize("name,mode", _params())
def test_no_writes_under_home(name, mode, tmp_path, home):
    filename, source, line, expr, build, program, extra = CASES[name]
    work = tmp_path / "work"
    work.mkdir()
    (work / filename).write_text(source)
    if build is not None:
        # Built with the real environment: compiling isn't tdb's doing.
        subprocess.run(build, cwd=work, check=True, timeout=120, capture_output=True)

    cfg = tmp_path / "cfg"
    env = dict(os.environ)
    # Let every tool fall back to HOME-based defaults, so the test sees
    # exactly what tdb's redirects (and nothing in the developer's
    # environment) keep out of HOME.
    for key in (*tool_redirects(cfg), "XDG_CONFIG_HOME", "XDG_DATA_HOME", "GOPATH"):
        env.pop(key, None)
    env.pop("TDB_CONFIG_DIR", None)
    env["HOME"] = str(home)
    marker = f"{os.getpid()}-{name}-{mode}"
    env[_MARKER_KEY] = marker
    env["PYTHONPATH"] = str(REPO_SRC) + os.pathsep + env.get("PYTHONPATH", "")
    if name == "powershell":
        # PSES may be found via ~/.vscode, which HOME no longer reaches.
        from tdb.adapters.powershell.locate import find_pses

        env.setdefault("TDB_PSES_PATH", str(find_pses(None)))

    if mode == "readonly":
        os.chmod(home, 0o500)
    before = _tree(home)

    argv = [
        sys.executable,
        "-m",
        "tdb",
        "--config-dir",
        str(cfg),
        *extra,
        "-e",
        f"{work / filename}:{line}",
        expr,
        str(work / program),
    ]
    proc = subprocess.Popen(
        argv,
        cwd=work,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        pytest.fail(f"{name}: tdb did not finish in {RUN_TIMEOUT}s")
    finally:
        # Always, even after a clean exit: an adapter that setsid'd away
        # (or lingers in tdb's session) must not outlive the test.
        _kill_tree(proc, marker)

    assert proc.returncode == 0, out
    assert "y 2" in out, out
    assert _tree(home) == before, f"{name} wrote under HOME:\n" + "\n".join(
        sorted(set(_tree(home)) - set(before))
    )

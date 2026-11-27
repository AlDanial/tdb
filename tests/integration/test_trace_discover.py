"""``tdb --trace-discover`` end to end, and the agent's discover mode."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from tdb.trace.discover import write_discovered_spec
from tdb.trace.launch import NO_PROCESS_WARNING
from tdb.trace.spec import load_spec, norm_path
from tests.integration.test_trace_launch import _env, listing, tdb, write_prog
from tests.integration.trace_helpers import run_traced

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="sys.monitoring")
posix = pytest.mark.skipif(os.name == "nt", reason="fork and POSIX signals")

END_TO_END = """\
import asyncio
import json


def f():
    return json.dumps({"n": 1})


def g():
    pass


class C:
    def m(self):
        return (lambda: 1)()


async def a():
    await asyncio.sleep(0)


for _ in range(3):
    f()
for _ in range(20_000):
    g()
C().m()
asyncio.run(a())
"""


def test_discover_end_to_end(tmp_path):
    write_prog(tmp_path, END_TO_END)
    r = tdb(tmp_path, "--trace-discover", "out.yaml", "prog.py")
    assert r.returncode == 0, r.stderr
    assert r.stderr == "tdb: wrote out.yaml (4 functions)\n"
    assert listing(tmp_path) == ["out.yaml", "prog.py"]  # the parts are gone
    text = (tmp_path / "out.yaml").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert re.fullmatch(
        r"# tdb --trace-discover: prog\.py, \d{4}-\d\d-\d\d \d\d:\d\d, \d+s", lines[0]
    ), lines[0]
    # Nothing else: no json (library) file, no <lambda>, no C (a class
    # body is no function), no module body.
    assert lines[1:] == [
        "# Remove what you do not need; hot functions make traces large.",
        "",
        "prog.py:",
        "  - f     # 3 calls",
        "  - g     # ≥10,000 calls  (hot)",
        "  - C::m  # 1 call",
        "  - a     # 1 call, async",
    ]
    assert "json" not in text and "<lambda>" not in text
    spec = load_spec(tmp_path / "out.yaml")
    assert [f.pattern for f in spec.entries[0].functions] == ["f", "g", "C.m", "a"]


def test_discover_keeps_the_programs_exit_code(tmp_path):
    write_prog(tmp_path, "import sys\ndef f():\n    sys.exit(3)\nf()\n")
    r = tdb(tmp_path, "--trace-discover", "out.yaml", "prog.py")
    assert r.returncode == 3, r.stderr
    assert r.stderr == "tdb: wrote out.yaml (1 function)\n"
    assert "  - f  # 1 call\n" in (tmp_path / "out.yaml").read_text(encoding="utf-8")


@posix
def test_discover_dies_by_the_programs_signal(tmp_path):
    """Ruling 24, as for --trace: tdb dies by the program's signal."""
    write_prog(
        tmp_path,
        "import os, signal\ndef f():\n    os.kill(os.getpid(), signal.SIGINT)\nf()\n",
    )
    r = tdb(tmp_path, "--trace-discover", "out.yaml", "prog.py")
    assert r.returncode == -signal.SIGINT, r.stderr
    assert "KeyboardInterrupt" in r.stderr  # the program's own traceback
    assert r.stderr.endswith("tdb: wrote out.yaml (1 function)\n")
    assert "  - f  # 1 call\n" in (tmp_path / "out.yaml").read_text(encoding="utf-8")


def test_discover_refuses_an_old_interpreter(tmp_path):
    if shutil.which("python3.11") is None:
        pytest.skip("python3.11 not found")
    write_prog(tmp_path, "open('ran', 'w').close()\n")
    r = tdb(
        tmp_path, "--trace-discover", "out.yaml", "--python", "python3.11", "prog.py"
    )
    assert r.returncode == 2
    assert re.fullmatch(
        r"tdb: error: --trace-discover needs Python 3\.12 or newer; "
        r".*python3\.11\S* is 3\.11",
        r.stderr.strip(),
    ), r.stderr
    assert listing(tmp_path) == ["prog.py"]


# --- the agent's discover mode ------------------------------------------------


def largest_counts(parts) -> dict[int, dict[str, int]]:
    """{pid: {qual: the largest n in its part}}; every count is cumulative."""
    out: dict[int, dict[str, int]] = {}
    for recs in parts.records.values():
        assert recs[0]["k"] == "hdr" and recs[0]["mode"] == "discover"
        mine = out.setdefault(recs[0]["pid"], {})
        for r in recs:
            if r["k"] == "count":
                assert r["n"] >= mine.get(r["qual"], 0), r  # never goes down
                mine[r["qual"]] = r["n"]
    return out


FORK = """\
import os

def f():
    pass

def h():
    pass

for _ in range(10_000):
    h()
for _ in range(5):
    f()
pid = os.fork()
if pid == 0:
    for _ in range(3):
        f()
    for _ in range(5):
        h()
    os._exit(0)
os.waitpid(pid, 0)
for _ in range(2):
    f()
print(pid)
"""


@posix
def test_fork_child_counts_only_its_own_calls(tmp_path):
    """A forked child's part starts its counts from zero, so the sum over
    the parts is the number of calls."""
    parts = run_traced(tmp_path, FORK, [], mode="discover", own_group=True)
    assert parts.returncode == 0, parts.stderr
    child = int(parts.stdout)
    counts = largest_counts(parts)
    (parent,) = set(counts) - {child}
    assert counts[parent] == {"f": 7, "h": 10_000}
    assert counts[child]["f"] == 3
    out = tmp_path / "out.yaml"
    n = write_discovered_spec(
        parts.dir, out, program_desc="prog.py", started=datetime.now(), duration_s=1
    )
    assert n == (2, 0, 0)
    assert out.read_text(encoding="utf-8").split("\n")[3:] == [
        "prog.py:",
        "  - f  # 10 calls",
        "  - h  # ≥10,000 calls  (hot)",
        "",
    ]


LIBRARY = """\
import json, site, sys
from pathlib import Path

user_site = Path(site.getusersitepackages())
user_site.mkdir(parents=True)
(user_site / "libmod.py").write_text("def lib_fn():\\n    pass\\n")
sys.path.insert(0, str(user_site))
import libmod
import helper

def outer():
    def inner():
        return [x for x in range(2)], list(x for x in range(2))
    return inner()

class K:
    attr = 1
    def method(self):
        pass

libmod.lib_fn()
helper.helped()
json.dumps(outer())
K().method()
exec("def from_exec():\\n    pass\\nfrom_exec()")
"""


def test_only_user_code_is_counted(tmp_path):
    """Not counted: the standard library, installed packages (here the
    user site-packages directory), exec'd code, module and class bodies,
    lambdas and generator expressions.  Nested functions are, under their
    qualname without ``<locals>``."""
    parts = run_traced(
        tmp_path,
        LIBRARY,
        [],
        mode="discover",
        extra_env={"PYTHONUSERBASE": str(tmp_path / "userbase")},
        files={"helper.py": "def helped():\n    pass\n"},
    )
    assert parts.returncode == 0, parts.stderr
    assert {
        (Path(r["file"]).name, r["qual"], r["kind"]) for r in parts.all("count")
    } == {
        ("prog.py", "outer", "function"),
        ("prog.py", "outer.inner", "function"),
        ("prog.py", "K.method", "function"),
        ("helper.py", "helped", "function"),
    }


def test_program_sees_what_it_would_without_the_tracer(tmp_path):
    """Discovery records no crashes, so it wraps no excepthook; finding the
    library directories leaves site's user directories as they were."""
    src = (
        "import site, sys, threading\n"
        "print(site.USER_BASE, site.USER_SITE, sys.excepthook is sys.__excepthook__,"
        " threading.excepthook is threading.__excepthook__)\n"
    )
    parts = run_traced(tmp_path, src, [], mode="discover")
    assert parts.returncode == 0 and parts.all("exit"), parts.stderr
    plain = subprocess.run(
        [sys.executable, "prog.py"], cwd=tmp_path, capture_output=True, text=True
    )
    assert parts.stdout == plain.stdout and parts.stdout.endswith("True True\n")


def test_library_code_and_hot_functions_stop_calling_back(tmp_path):
    """DISABLE keeps the overhead bounded: library code calls back once
    per code object, a user function until it reaches the cap."""
    src = (
        "import json\nimport _tdb_trace_agent\n"
        "def hot():\n    pass\n"
        "for _ in range(30_000):\n    hot()\n    json.dumps(1)\n"
        "print(_tdb_trace_agent.stats()['py_start_callbacks'])\n"
    )
    parts = run_traced(tmp_path, src, [], mode="discover")
    assert parts.returncode == 0, parts.stderr
    assert 10_000 < int(parts.stdout) < 12_000
    (count,) = parts.all("count")[-1:]
    assert (count["qual"], count["n"], count["capped"]) == ("hot", 10_000, True)


ALARMS = """\
import signal
import time

class Alarm(Exception):
    pass

def on_alarm(signum, frame):
    raise Alarm

def spin():
    return 1

signal.signal(signal.SIGALRM, on_alarm)
caught = 0
for _ in range(20):
    end = time.monotonic() + 2
    signal.setitimer(signal.ITIMER_REAL, 0.002)
    try:
        while time.monotonic() < end:
            spin()
    except Alarm:
        caught += 1
    signal.setitimer(signal.ITIMER_REAL, 0)
print(caught)
"""


@posix
def test_a_signal_handlers_exception_reaches_the_program(tmp_path):
    """An exception a signal handler raises inside the counting callback
    is the program's: it passes through (with no cap, every call of spin
    calls back)."""
    parts = run_traced(
        tmp_path, ALARMS, [], mode="discover", discover_cap=10**9, timeout=120
    )
    assert parts.returncode == 0, parts.stderr
    assert parts.stdout.split() == ["20"]


# --- fix round 1 ------------------------------------------------------------


@posix
def test_no_traced_process_is_reported(tmp_path):
    """Issue 1: a program started with -I loads no tracer; tdb says so
    rather than write an empty spec without a word."""
    script = tmp_path / "isolated"
    script.write_text(f"#!{sys.executable} -I\ndef f():\n    pass\nf()\n")
    script.chmod(0o755)
    r = tdb(tmp_path, "--trace-discover", "out.yaml", "./isolated")
    assert r.returncode == 0
    assert r.stderr.splitlines() == [NO_PROCESS_WARNING]
    assert not (tmp_path / "out.yaml").exists()  # F4: no spec without functions


def test_program_installed_in_site_packages_is_discovered(tmp_path):
    """Ruling 26: the program is the user's code wherever it is installed;
    the other modules of its site-packages directory stay library code."""
    env = {**_env(tmp_path), "PYTHONUSERBASE": str(tmp_path / "userbase")}
    where = subprocess.run(
        [sys.executable, "-c", "import site; print(site.getusersitepackages())"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    site_dir = Path(where.stdout.strip())
    site_dir.mkdir(parents=True)
    (site_dir / "neighbour.py").write_text("def helper():\n    pass\n")
    prog = site_dir / "prog.py"
    prog.write_text("import neighbour\n\ndef f():\n    neighbour.helper()\n\nf()\n")
    r = tdb(tmp_path, "--trace-discover", "out.yaml", str(prog), env=env)
    assert r.returncode == 0, r.stderr
    assert r.stderr == "tdb: wrote out.yaml (1 function)\n"
    key = prog.relative_to(tmp_path).as_posix()
    lines = (tmp_path / "out.yaml").read_text(encoding="utf-8").splitlines()
    assert lines[3:] == [f"{key}:", "  - f  # 1 call"]


PY314 = shutil.which("python3.14")

DATACLASS = """\
import typing
from dataclasses import dataclass


@dataclass
class Point:
    x: int

    def norm(self) -> int:
        return self.x


def area(p: Point) -> int:
    return p.norm()


area(Point(2))
typing.get_type_hints(area)
"""


@pytest.mark.skipif(PY314 is None, reason="no python3.14")
def test_python_3_14_annotations_are_not_functions(tmp_path):
    """Issue 2: 3.14 runs a compiler-made __annotate__ for each annotated
    class and function that is introspected (@dataclass, get_type_hints);
    no source defines it, so it is never counted."""
    write_prog(tmp_path, DATACLASS)
    r = tdb(tmp_path, "--trace-discover", "out.yaml", "--python", PY314, "prog.py")
    assert r.returncode == 0, r.stderr
    assert r.stderr == "tdb: wrote out.yaml (2 functions)\n"  # nothing omitted
    lines = (tmp_path / "out.yaml").read_text(encoding="utf-8").splitlines()
    assert lines[3:] == [
        "prog.py:",
        "  - Point::norm  # 1 call",
        "  - area         # 1 call",
    ]
    load_spec(tmp_path / "out.yaml")


RELOAD = """\
import importlib
import helper

for _ in range(3):
    helper.f()
old = helper.f.__code__
importlib.reload(helper)
assert helper.f.__code__ is not old
for _ in range(4):
    helper.f()
for _ in range(20):
    helper.g()
importlib.reload(helper)
for _ in range(5):
    helper.g()
"""


def test_code_objects_of_one_function_count_together(tmp_path):
    """M6: a module reloaded gives its functions new code objects; they
    count as one, and a function capped through one is DISABLEd at the
    first call through another."""
    parts = run_traced(
        tmp_path,
        RELOAD,
        [],
        mode="discover",
        discover_cap=10,
        files={"helper.py": "def f():\n    pass\n\ndef g():\n    pass\n"},
    )
    assert parts.returncode == 0, parts.stderr
    helper = [r for r in parts.all("count") if Path(r["file"]).name == "helper.py"]
    assert {(r["qual"], r["line"]) for r in helper} == {("f", 1), ("g", 4)}
    last = {r["qual"]: (r["n"], r["capped"]) for r in helper}
    assert last == {"f": (7, False), "g": (10, True)}


# --- final review F4 ----------------------------------------------------------

NEWER_SYNTAX = """\
def risky(x):
    try:
        return 1 / x
    except ZeroDivisionError, TypeError:  # PEP 758: Python 3.14 only
        return 0


def main():
    return risky(0)


main()
"""


@pytest.mark.skipif(PY314 is None, reason="no python3.14")
@pytest.mark.skipif(sys.version_info >= (3, 14), reason="tdb's Python parses it")
def test_source_only_the_programs_python_can_parse(tmp_path):
    """F4 (Ruling 28): tdb's Python cannot parse a 3.14 file the program's
    Python runs.  Discovery keeps its functions, and --trace warns that it
    cannot check their names, then traces them."""
    prog = write_prog(tmp_path, NEWER_SYNTAX)
    r = tdb(tmp_path, "--trace-discover", "out.yaml", "--python", PY314, "prog.py")
    assert r.returncode == 0, r.stderr
    assert r.stderr == "tdb: wrote out.yaml (2 functions)\n"
    lines = (tmp_path / "out.yaml").read_text(encoding="utf-8").splitlines()
    assert lines[3:] == ["prog.py:", "  - risky  # 1 call", "  - main   # 1 call"]
    r = tdb(
        tmp_path,
        "--trace",
        "out.yaml",
        "--trace-out",
        "t",
        "--python",
        PY314,
        "prog.py",
    )
    assert r.returncode == 0, r.stderr
    py = "{}.{}".format(*sys.version_info[:2])
    assert r.stderr.startswith(
        f"tdb: warning: {norm_path(prog)}: cannot check names "
        f"(tdb's Python {py} cannot parse it: "
    ), r.stderr
    assert 'tdb: trace "prog": 1 process, 1 thread, 0 tasks, 2 calls' in r.stderr
    assert "never called" not in r.stderr

"""End-to-end: a variable created at the Evaluate console shows up under
the "Interactive" scope, at the same stop and after a step, for the
languages whose adapters are installed here.

Each case stops inside a function, runs the language's assignment
through `DebugController.evaluate_console`, and checks
`state.scopes` / `state.variables` — the exact data the Variables View
renders. Cases skip individually when their toolchain is missing.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from dataclasses import dataclass

import pytest

from tdb.dap.types import SourceBreakpoint
from tdb.languages import registry
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController
from tdb.session.state import INTERACTIVE_SCOPE_REF

from tests.integration.bash_adapter_harness import bash_ok

WAIT = 30.0


def _gdb_ok() -> bool:
    gdb = shutil.which("gdb")
    if gdb is None or shutil.which("g++") is None:
        return False
    out = subprocess.run([gdb, "--version"], capture_output=True, text=True).stdout
    m = re.search(r"(\d+)\.\d+", out)
    return bool(m) and int(m.group(1)) >= 14


def _perl_ok() -> bool:
    return (
        shutil.which("perl") is not None
        and subprocess.run(["perl", "-e", "require v5.18"]).returncode == 0
    )


@dataclass(frozen=True)
class Case:
    lang: str
    adapter: str | None
    filename: str
    source: str
    bp_line: int  # inside the function, before the line that defines `c`
    assignment: str
    display_name: str
    value: str
    build: tuple[str, ...] = ()  # argv run in the temp dir; empty -> interpreted
    binary: str = ""
    available: bool = True

    @property
    def id(self) -> str:
        return f"{self.lang}-{self.adapter or 'default'}"


CASES = [
    Case(
        "python",
        None,
        "p.py",
        "def f(a):\n    b = a * 2\n    c = b + 1\n    return c\n\nprint(f(3))\n",
        3,
        "newvar = 42",
        "newvar",
        "42",
    ),
    Case(
        "perl",
        None,
        "p.pl",
        "sub f {\n    my ($a) = @_;\n    my $b = $a * 2;\n    my $c = $b + 1;\n"
        '    return $c;\n}\nprint f(3), "\\n";\n',
        4,
        "$newvar = 42",
        "$newvar",
        "42",
        available=_perl_ok(),
    ),
    Case(
        "bash",
        None,
        "p.sh",
        "f() {\n  local b=$(( $1 * 2 ))\n  local c=$(( b + 1 ))\n  echo $c\n}\nf 3\n",
        3,
        "newvar=42",
        "newvar",
        "42",
        available=bash_ok(),
    ),
    Case(
        "cpp",
        "lldb-dap",
        "p.cpp",
        "#include <cstdio>\nint f(int a) {\n    int b = a * 2;\n    int c = b + 1;\n"
        '    return c;\n}\nint main() {\n    printf("%d\\n", f(3));\n}\n',
        3,
        "int $newvar = 42",
        "$newvar",
        "42",
        build=("g++", "-g", "-O0", "-o", "p_cpp", "p.cpp"),
        binary="p_cpp",
        available=shutil.which("lldb-dap") is not None
        and shutil.which("g++") is not None,
    ),
    Case(
        "cpp",
        "gdb",
        "p.cpp",
        "#include <cstdio>\nint f(int a) {\n    int b = a * 2;\n    int c = b + 1;\n"
        '    return c;\n}\nint main() {\n    printf("%d\\n", f(3));\n}\n',
        3,
        "set $newvar = 42",
        "$newvar",
        "42",
        build=("g++", "-g", "-O0", "-o", "p_cpp", "p.cpp"),
        binary="p_cpp",
        available=_gdb_ok(),
    ),
]


def _interactive(ctrl) -> dict[str, str]:
    assert ctrl.state.scopes[-1].name == "Interactive"
    return {v.name: v.value for v in ctrl.state.variables[INTERACTIVE_SCOPE_REF]}


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
async def test_console_assignment_appears_under_interactive_scope(case, tmp_path):
    if not case.available:
        pytest.skip(f"{case.id}: toolchain not installed")
    src = tmp_path / case.filename
    src.write_text(case.source)
    if case.build:
        subprocess.run(case.build, cwd=tmp_path, check=True)
    program = str(tmp_path / (case.binary or case.filename))
    profile = registry.resolve(case.lang, adapter=case.adapter, program=program)
    handler = ServerEventHandler()
    ctrl = DebugController(handler, profile=profile)
    try:
        ctrl.state.breakpoints[str(src)] = [SourceBreakpoint(line=case.bp_line)]
        await ctrl.start(program=program, stop_on_entry=False)
        await asyncio.wait_for(handler.initialized_event.wait(), WAIT)
        await ctrl.do_configure()
        assert await handler.wait_for_stop(timeout=WAIT)
        await ctrl.fetch_stop_info()
        assert all(s.name != "Interactive" for s in ctrl.state.scopes)

        await ctrl.evaluate_console(case.assignment)
        # Same stop: the console entry alone refreshed the scopes.
        assert _interactive(ctrl) == {case.display_name: case.value}

        handler.reset_for_continue()
        await ctrl.step_over()
        assert await handler.wait_for_stop(timeout=WAIT)
        await ctrl.fetch_stop_info()
        assert _interactive(ctrl) == {case.display_name: case.value}
    finally:
        try:
            await asyncio.wait_for(ctrl.stop(), timeout=WAIT)
        except Exception:
            pass

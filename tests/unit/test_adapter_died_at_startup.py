"""An adapter that starts but exits before answering `initialize` (the
RHEL 8 case: `gdb -i dap` on a gdb without a DAP interpreter prints
"Interpreter `dap' unrecognized" and exits 1) must reach the user the
way a missing adapter does — one line on stderr, exit 2 — in every
front end, not as a bare "Failed to start" subtitle with the reason
buried in the log."""

import sys

import pytest

from tdb.app import TdbApp
from tdb.languages.base import (
    AdapterSpec,
    LanguageProfile,
    Presentation,
    ProfileCapabilities,
)
from tdb.persist import TdbConfig
from tdb.run_mode import start_session
from tdb.server.event_handler import ServerEventHandler
from tdb.server.runner import run_headless
from tdb.session.controller import DebugController

GDB_ERROR = "Interpreter `dap' unrecognized"


# Two speeds of dying: a Python one-liner exits after tdb has written
# `initialize` (the death watcher fails the pending request), a shell
# one-liner exits before (the write itself fails and must defer to the
# watcher's message). Both orders happen with real gdb.
SLOW = [
    sys.executable,
    "-c",
    f"import sys; sys.stderr.write({GDB_ERROR!r} + '\\n'); sys.exit(1)",
]
FAST = ["/bin/sh", "-c", "printf '%s\\n' \"$0\" >&2; exit 1", GDB_ERROR]
SPEEDS = [SLOW] + ([FAST] if sys.platform != "win32" else [])


class _DyingAdapterSpec(AdapterSpec):
    id = "gdb"

    def __init__(self, argv: list[str]) -> None:
        self._argv = argv

    def command(self):
        return list(self._argv)

    def diagnose_exit(self, stderr: str) -> str | None:
        return "install GDB >= 14" if GDB_ERROR in stderr else None

    def launch_body(self, **kw):
        return {}

    def attach_body(self, **kw):
        return {}

    def pick_exception_filters(self, caps):
        return []


def _dying_profile(argv: list[str]) -> LanguageProfile:
    return LanguageProfile(
        id="cpp",
        display_name="C/C++",
        adapter=_DyingAdapterSpec(argv),
        presentation=Presentation(),
        capabilities=ProfileCapabilities(),
    )


@pytest.mark.parametrize("argv", SPEEDS, ids=["slow", "fast"][: len(SPEEDS)])
async def test_tui_surfaces_adapter_death_and_exits(argv):
    app = TdbApp(program="prog", config=TdbConfig(), profile=_dying_profile(argv))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(50):
            if app._startup_error is not None:
                break
            await pilot.pause(0.1)
    assert app._startup_error is not None
    assert "adapter died" in app._startup_error
    assert GDB_ERROR in app._startup_error
    assert "install GDB >= 14" in app._startup_error
    assert app.return_code == 2


@pytest.mark.parametrize("argv", SPEEDS, ids=["slow", "fast"][: len(SPEEDS)])
async def test_run_headless_prints_adapter_death_and_exits(argv, capsys):
    with pytest.raises(SystemExit) as exc_info:
        await run_headless(program="prog", profile=_dying_profile(argv))
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert GDB_ERROR in err and "install GDB >= 14" in err


@pytest.mark.parametrize("argv", SPEEDS, ids=["slow", "fast"][: len(SPEEDS)])
async def test_run_mode_start_session_bails_on_adapter_death(argv, capsys):
    controller = DebugController(ServerEventHandler(), profile=_dying_profile(argv))
    code = await start_session(controller, program="prog", args=[], cwd=".")
    assert code == 2
    err = capsys.readouterr().err
    assert GDB_ERROR in err and "install GDB >= 14" in err

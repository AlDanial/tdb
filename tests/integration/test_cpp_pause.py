"""Task 9: does DAP `pause` stop a running (never-stopped) C debuggee?

`tdb --run` launches with stop_on_entry=False and never sees a stop
event until the user signals (Ctrl-C / SIGUSR1) and `controller.pause()`
is called with no prior stop — DebugController.pause() falls back to
querying the adapter's thread list in that case. This decides whether
the cpp LanguageProfile gets `pause_while_running=True`.

Modeled on tests/integration/test_cpp_session.py (lldb-dap) and
tests/integration/test_gdb_session.py (gdb): same compile-and-launch
harness and adapter-availability skip guards, extended to both
adapters via a single parametrized fixture.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess

import pytest

from tdb.languages.cpp import build_cpp_profile
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController

WAIT = 20.0  # generous ceiling for adapter spawn + debuggee start
PAUSE_TIMEOUT = 10.0

SPIN_SRC = """\
#include <unistd.h>
int main(void) {
    volatile long i = 0;
    for (;;) { i++; usleep(1000); }
    return 0;
}
"""


def _gdb_supports_dap() -> bool:
    gdb = shutil.which("gdb")
    if gdb is None:
        return False
    out = subprocess.run([gdb, "--version"], capture_output=True, text=True).stdout
    m = re.search(r"(\d+)\.\d+", out)
    return bool(m) and int(m.group(1)) >= 14


_HAVE_CC = shutil.which("gcc") is not None or shutil.which("cc") is not None
_HAVE_LLDB_DAP = shutil.which("lldb-dap") is not None
_HAVE_GDB_DAP = _gdb_supports_dap()

pytestmark = pytest.mark.skipif(
    not _HAVE_CC or not (_HAVE_LLDB_DAP or _HAVE_GDB_DAP),
    reason="a C compiler and at least one of lldb-dap / gdb>=14 are required",
)


@pytest.fixture(scope="module")
def spin_binary(tmp_path_factory):
    src = tmp_path_factory.mktemp("cppspin") / "spin.c"
    src.write_text(SPIN_SRC)
    binary = src.parent / "spin"
    cc = shutil.which("gcc") or shutil.which("cc")
    subprocess.run([cc, "-g", "-O0", "-o", str(binary), str(src)], check=True)
    return str(binary)


# --- live-session fixture, adapted from test_cpp_session.py / test_gdb_session.py:
# --- launches with stop_on_entry=False (mirrors run_mode.run()) so the
# --- controller never sees a stop event before pause() is exercised.


@pytest.fixture(params=["lldb-dap", "gdb"])
async def cpp_controller_running_spin(request, spin_binary):
    adapter = request.param
    if adapter == "lldb-dap" and not _HAVE_LLDB_DAP:
        pytest.skip("lldb-dap not installed")
    if adapter == "gdb" and not _HAVE_GDB_DAP:
        pytest.skip("gdb >= 14 (its DAP mode) not installed")

    handler = ServerEventHandler()
    ctrl = DebugController(handler, profile=build_cpp_profile(adapter=adapter))
    await ctrl.start(program=spin_binary, stop_on_entry=False)
    await asyncio.wait_for(handler.initialized_event.wait(), WAIT)
    await ctrl.do_configure()
    yield ctrl
    try:
        await asyncio.wait_for(ctrl.stop(), timeout=WAIT)
    except Exception:
        pass  # already stopped / adapter already gone


# --- live-session test -----------------------------------------------------


async def test_pause_stops_running_cpp_loop(cpp_controller_running_spin):
    controller = cpp_controller_running_spin  # launched, NOT stopped
    ok = await controller.pause(timeout=PAUSE_TIMEOUT)
    assert ok is True
    await controller.fetch_stop_info()
    assert controller.state.stack_frames

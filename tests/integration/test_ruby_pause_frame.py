"""A run-mode style pause into `sleep` lands on a frame with locals.

ruby_sleep.rb spends ~100% of its time inside `sleep 0.05`, so a DAP
pause essentially always stops with rdbg reporting "[C] Kernel#sleep"
as the top frame — whose locals scope holds only %self. The ruby
profile marks such native frames opaque, so the controller selects the
first real Ruby frame (`block in <main>`) and the Variables view shows
`i`. Modeled on tests/integration/test_cpp_pause.py.
"""

from __future__ import annotations

import asyncio

import pytest

from tdb.languages.ruby import build_ruby_profile
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController

from tests.integration.ruby_adapter_harness import FIXTURES, rdbg_ok

WAIT = 20.0
PAUSE_TIMEOUT = 10.0

pytestmark = pytest.mark.skipif(
    not rdbg_ok(), reason="rdbg (debug gem >= 1.9) is required"
)


@pytest.fixture
async def ruby_controller_running_sleep():
    handler = ServerEventHandler()
    ctrl = DebugController(handler, profile=build_ruby_profile())
    await ctrl.start(program=str(FIXTURES / "ruby_sleep.rb"), stop_on_entry=False)
    await asyncio.wait_for(handler.initialized_event.wait(), WAIT)
    await ctrl.do_configure()
    yield ctrl
    try:
        await asyncio.wait_for(ctrl.stop(), timeout=WAIT)
    except Exception:
        pass  # already stopped / adapter already gone


async def test_pause_selects_frame_with_locals(ruby_controller_running_sleep):
    ctrl = ruby_controller_running_sleep
    await asyncio.sleep(1.0)  # let the loop settle into sleep/increment cycles
    assert await ctrl.pause(timeout=PAUSE_TIMEOUT) is True
    await ctrl.fetch_stop_info()

    frames = ctrl.state.stack_frames
    assert frames, "no stack after pause"
    selected = next(f for f in frames if f.id == ctrl.state.current_frame_id)
    assert not selected.name.startswith("[C] "), selected.name

    names = {v.name for variables in ctrl.state.variables.values() for v in variables}
    assert "i" in names, f"'i' missing from variables: {sorted(names)}"

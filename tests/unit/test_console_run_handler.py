"""Headless run phase: debuggee output streams straight to the
terminal; exit/stop events become asyncio.Events the run loop awaits."""

import asyncio

from tdb.run_mode import ConsoleRunHandler
from tdb.session.event_bus import DebugEventHandler


async def test_protocol_and_events(capsys):
    h = ConsoleRunHandler()
    assert isinstance(h, DebugEventHandler)
    assert not h.initialized.is_set()

    h.on_initialized()
    assert h.initialized.is_set()

    h.on_output("out\n", "stdout")
    h.on_output("err\n", "stderr")
    h.on_output("note\n", "console")
    captured = capsys.readouterr()
    assert captured.out == "out\nnote\n"
    assert captured.err == "err\n"

    h.on_stopped(4, "pause", None, None)
    assert h.stopped.is_set()
    assert h.last_stop == (4, "pause", None, None)
    h.on_continued()
    assert not h.stopped.is_set()

    h.on_exited(7)
    assert h.exited.is_set()
    assert h.exit_code == 7


async def test_terminated_without_exit_code_ends_run_phase():
    h = ConsoleRunHandler()
    h.on_terminated()
    assert h.exited.is_set()
    assert h.exit_code is None

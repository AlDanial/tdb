"""Run-mode examine cycle, driven with a class-level-patched
DebugController: no adapter, no debuggee. Signals are raised at the
process itself (POSIX only), exactly as a terminal would deliver them."""

from __future__ import annotations

import asyncio
import io
import json
import os
import signal

import pytest

from tdb import run_mode
from tdb.persist import TdbConfig
from tdb.session.controller import DebugController
from tdb.session.state import SessionPhase

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX signals")


class _Sink(io.StringIO):
    """A StringIO that survives the real `close_sinks()` call: records that
    close() was requested but keeps the buffer readable so the test can
    still call getvalue() after run() returns."""

    def __init__(self):
        super().__init__()
        self.closed_by_run = False

    def close(self):
        self.closed_by_run = True


class Script:
    """Records controller calls and lets tests decide when a pause lands."""

    def __init__(self):
        self.calls: list[str] = []
        self.pause_lands = True
        self.console: run_mode.ConsoleRunHandler | None = None
        self.controller: DebugController | None = None

    def stop_now(self, reason="pause"):
        self.controller.state.enter_stop(1, reason)
        self.console.on_stopped(1, reason, None, None)


@pytest.fixture
def script(monkeypatch):
    s = Script()

    async def start(self, **kw):
        s.controller = self
        s.calls.append("start")
        self.state.transition_to(SessionPhase.RUNNING)

    async def do_configure(self):
        s.calls.append("configure")

    async def pause(self, timeout=2.0):
        s.calls.append("pause")
        if s.pause_lands:
            s.stop_now()
            return True
        return False

    async def continue_(self):
        s.calls.append("continue")
        self.state.transition_to(SessionPhase.RUNNING)
        s.console.on_continued()

    async def stop(self):
        s.calls.append("stop")

    async def fetch_stop_info(self):
        s.calls.append("fetch_stop_info")

    for name, fn in [
        ("start", start),
        ("do_configure", do_configure),
        ("pause", pause),
        ("continue_", continue_),
        ("stop", stop),
        ("fetch_stop_info", fetch_stop_info),
    ]:
        monkeypatch.setattr(DebugController, name, fn)

    async def fake_collect(controller, **kw):
        s.calls.append(f"collect:{kw['status']}:{kw['seq']}:{kw['trigger']}")
        return {"seq": kw["seq"], "status": kw["status"], "trigger": kw["trigger"]}

    monkeypatch.setattr(run_mode.examine, "collect", fake_collect)
    return s


def _capture_console(script):
    orig = run_mode.ConsoleRunHandler

    class Hooked(orig):
        def __init__(self):
            super().__init__()
            script.console = self

    return Hooked


async def _run(script, monkeypatch, sink, *, driver, episode=None):
    monkeypatch.setattr(run_mode, "ConsoleRunHandler", _capture_console(script))
    monkeypatch.setattr(run_mode.examine, "open_sinks", lambda dests: [sink])

    async def default_episode(controller, handler, console, config, program):
        script.calls.append("episode")
        return True  # detach

    async def ready_then_drive(controller):
        script.console.initialized.set()
        await driver()

    def on_ready(controller):
        asyncio.get_running_loop().create_task(ready_then_drive(controller))

    monkeypatch.setattr(run_mode, "configure_when_initialized", _configure_immediately)
    return await asyncio.wait_for(
        run_mode.run(
            program="/p.py",
            config=TdbConfig(),
            tui_episode=episode or default_episode,
            on_session_ready=on_ready,
            examine_dests=["-"],
        ),
        timeout=10,
    )


async def _configure_immediately(console, controller):
    await controller.do_configure()


def _records(sink):
    return [json.loads(line) for line in sink.getvalue().splitlines()]


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0.01)


async def test_sigusr2_pause_collect_write_continue(script, monkeypatch):
    sink = _Sink()
    sink.name = "s"

    async def driver():
        await _settle()
        os.kill(os.getpid(), signal.SIGUSR2)
        await _settle()
        script.console.on_exited(0)

    code = await _run(script, monkeypatch, sink, driver=driver)
    assert code == 0
    assert script.calls == [
        "start",
        "configure",
        "pause",
        "fetch_stop_info",
        "collect:ok:1:SIGUSR2",
        "continue",
    ]
    assert [r["seq"] for r in _records(sink)] == [1]
    assert sink.closed_by_run  # run()'s outer finally closes sinks for real


async def test_sigquit_and_repeat_presses_coalesce_and_number_sequentially(
    script, monkeypatch
):
    sink = _Sink()
    sink.name = "s"

    async def driver():
        await _settle()
        os.kill(os.getpid(), signal.SIGQUIT)
        os.kill(os.getpid(), signal.SIGQUIT)
        await _settle()
        os.kill(os.getpid(), signal.SIGQUIT)
        await _settle()
        script.console.on_exited(3)

    code = await _run(script, monkeypatch, sink, driver=driver)
    assert code == 3
    assert script.calls.count("pause") == 2
    assert [r["seq"] for r in _records(sink)] == [1, 2]
    assert _records(sink)[0]["trigger"] == "SIGQUIT"


async def test_pending_then_landed_completes_without_tui(script, monkeypatch):
    sink = _Sink()
    sink.name = "s"
    script.pause_lands = False

    async def driver():
        await _settle()
        os.kill(os.getpid(), signal.SIGUSR2)
        await _settle()
        script.stop_now()  # the pause finally lands
        await _settle()
        script.console.on_exited(0)

    await _run(script, monkeypatch, sink, driver=driver)
    assert "episode" not in script.calls
    assert script.calls == [
        "start",
        "configure",
        "pause",
        "collect:pending:1:SIGUSR2",
        "fetch_stop_info",
        "collect:ok:1:SIGUSR2",
        "continue",
    ]
    recs = _records(sink)
    assert [(r["seq"], r["status"]) for r in recs] == [(1, "pending"), (1, "ok")]


async def test_ctrl_c_cancels_outstanding_examine_and_opens_tui(script, monkeypatch):
    sink = _Sink()
    sink.name = "s"
    script.pause_lands = False

    async def driver():
        await _settle()
        os.kill(os.getpid(), signal.SIGUSR2)
        await _settle()
        script.pause_lands = True
        os.kill(os.getpid(), signal.SIGINT)
        await _settle()
        script.console.on_exited(0)

    await _run(script, monkeypatch, sink, driver=driver)
    assert "episode" in script.calls
    assert "collect:ok:1:SIGUSR2" not in script.calls
    assert [r["status"] for r in _records(sink)] == ["pending"]


async def test_interrupt_wins_over_simultaneous_examine(script, monkeypatch):
    sink = _Sink()
    sink.name = "s"

    async def driver():
        await _settle()
        # Both delivered before the loop wakes: SIGINT is armed on the loop
        # too, so raise them back to back.
        os.kill(os.getpid(), signal.SIGUSR2)
        os.kill(os.getpid(), signal.SIGINT)
        await _settle()
        script.console.on_exited(0)

    await _run(script, monkeypatch, sink, driver=driver)
    assert "episode" in script.calls
    assert not any(c.startswith("collect") for c in script.calls)
    assert _records(sink) == []


async def test_exit_during_capture_writes_exited_record(script, monkeypatch):
    sink = _Sink()
    sink.name = "s"

    async def pause(self, timeout=2.0):
        script.calls.append("pause")
        script.console.on_exited(9)
        return False

    monkeypatch.setattr(DebugController, "pause", pause)

    async def driver():
        await _settle()
        os.kill(os.getpid(), signal.SIGUSR2)

    code = await _run(script, monkeypatch, sink, driver=driver)
    assert code == 9
    assert script.calls[-1] == "collect:exited:1:SIGUSR2"
    assert _records(sink)[0]["status"] == "exited"


async def test_late_landing_ctrl_c_stop_still_opens_tui(script, monkeypatch):
    """Regression guard for the stray-pause rule: a Ctrl-C whose pause
    lands late must still open the TUI when the stop arrives."""
    sink = _Sink()
    sink.name = "s"
    script.pause_lands = False

    async def driver():
        await _settle()
        os.kill(os.getpid(), signal.SIGINT)
        await _settle()
        script.stop_now("pause")  # the Ctrl-C pause finally lands
        await _settle()
        script.console.on_exited(0)

    await _run(script, monkeypatch, sink, driver=driver)
    assert "episode" in script.calls
    assert _records(sink) == []


async def test_pending_ctrl_c_wins_over_examine_and_drops_it(script, monkeypatch):
    """A Ctrl-C whose pause hasn't landed yet must not have its stop stolen
    by a subsequent examine trigger: the examine request is dropped, and
    the eventual stop still opens the TUI rather than being treated as a
    stray pause-all stop (interrupt_pending must stay set)."""
    sink = _Sink()
    sink.name = "s"
    script.pause_lands = False

    async def driver():
        await _settle()
        os.kill(os.getpid(), signal.SIGINT)
        await _settle()
        os.kill(os.getpid(), signal.SIGUSR2)
        await _settle()
        script.stop_now("pause")  # the Ctrl-C pause finally lands
        await _settle()
        script.console.on_exited(0)

    await _run(script, monkeypatch, sink, driver=driver)
    assert "episode" in script.calls
    assert not any(c.startswith("collect") for c in script.calls)
    assert _records(sink) == []


async def test_stray_pause_stop_is_resumed_not_debugged(script, monkeypatch):
    """A `stopped(reason=pause)` with nothing waiting on it (a child's
    late pause-all stop after an examine already resumed everything)
    must be continued, not turned into a TUI episode."""
    sink = _Sink()
    sink.name = "s"

    async def driver():
        await _settle()
        os.kill(os.getpid(), signal.SIGUSR2)
        await _settle()
        script.stop_now("pause")  # late child stop
        await _settle()
        script.console.on_exited(0)

    await _run(script, monkeypatch, sink, driver=driver)
    assert "episode" not in script.calls
    assert script.calls.count("continue") == 2
    assert [r["seq"] for r in _records(sink)] == [1]


async def test_breakpoint_stop_still_opens_tui(script, monkeypatch):
    """The stray-pause rule must not swallow real stops."""
    sink = _Sink()
    sink.name = "s"

    async def driver():
        await _settle()
        script.stop_now("breakpoint")
        await _settle()
        script.console.on_exited(0)

    await _run(script, monkeypatch, sink, driver=driver)
    assert "episode" in script.calls


async def test_examine_signals_ignored_during_episode_and_restored_after(
    script, monkeypatch
):
    sink = _Sink()
    sink.name = "s"
    seen = {}

    async def episode(controller, handler, console, config, program):
        seen["during"] = signal.getsignal(signal.SIGQUIT)
        return True

    async def driver():
        await _settle()
        os.kill(os.getpid(), signal.SIGINT)
        await _settle()
        script.console.on_exited(0)

    await _run(script, monkeypatch, sink, driver=driver, episode=episode)
    assert seen["during"] is signal.SIG_IGN
    assert signal.getsignal(signal.SIGQUIT) is signal.SIG_DFL
    assert signal.getsignal(signal.SIGUSR2) is signal.SIG_DFL

"""Console view stdin line: typing in the Console pane feeds the program.

The Console view grows an input line docked under the output log. Enter
sends the line (plus newline) through `controller.write_stdin`, echoes
it in the log (a pipe has no terminal echo), records the gesture for
--record/--replay, and clears the field. Ctrl+D sends EOF. The line is
hidden when the session has no stdin to feed.
"""

from __future__ import annotations

import pytest
from textual.widgets import Input, RichLog

from tdb.app import TdbApp
from tdb.persist import TdbConfig
from tdb.server.event_handler import ServerEventHandler
from tdb.server.handlers import ControllerRef, RpcHandlers
from tdb.widgets.console_view import ConsoleView

from tests.unit.record_helpers import CaptureRecorder


class _StdinSpy:
    def __init__(self, supports: bool = True) -> None:
        self.supports_stdin = supports
        self.written: list[str] = []
        self.closed = 0

    async def write_stdin(self, text: str) -> None:
        if not self.supports_stdin:
            raise RuntimeError("no stdin here")
        self.written.append(text)

    async def close_stdin(self) -> None:
        self.closed += 1


@pytest.fixture
async def app():
    app = TdbApp(program="", config=TdbConfig(), recorder=CaptureRecorder())
    async with app.run_test() as pilot:
        await pilot.pause()
        yield app, pilot


def _console(app: TdbApp) -> ConsoleView:
    return app.query_one("#console-view", ConsoleView)


def _log_text(app: TdbApp) -> str:
    log = _console(app).query_one(RichLog)
    return "\n".join(str(line) for line in log.lines)


async def test_enter_sends_line_echoes_and_records(app, monkeypatch):
    app, pilot = app
    spy = _StdinSpy()
    monkeypatch.setattr(
        type(app.controller), "supports_stdin", property(lambda s: True), raising=False
    )
    monkeypatch.setattr(app.controller, "write_stdin", spy.write_stdin)
    console = _console(app)
    console.set_stdin_enabled(True)
    console.focus()
    await pilot.pause()
    field = console.query_one(Input)
    assert field.has_focus
    await pilot.press("4", "2", "enter")
    await pilot.pause()
    assert spy.written == ["42\n"]
    assert field.value == ""
    assert "42" in _log_text(app)
    assert ("stdin", ["42"]) in app.recorder.records


async def test_empty_enter_sends_bare_newline(app, monkeypatch):
    """A bare Enter is a real answer (input() returns ''), not a no-op."""
    app, pilot = app
    spy = _StdinSpy()
    monkeypatch.setattr(
        type(app.controller), "supports_stdin", property(lambda s: True), raising=False
    )
    monkeypatch.setattr(app.controller, "write_stdin", spy.write_stdin)
    console = _console(app)
    console.set_stdin_enabled(True)
    console.focus()
    await pilot.press("enter")
    await pilot.pause()
    assert spy.written == ["\n"]


async def test_ctrl_d_sends_eof(app, monkeypatch):
    app, pilot = app
    spy = _StdinSpy()
    monkeypatch.setattr(
        type(app.controller), "supports_stdin", property(lambda s: True), raising=False
    )
    monkeypatch.setattr(app.controller, "close_stdin", spy.close_stdin)
    console = _console(app)
    console.set_stdin_enabled(True)
    console.focus()
    await pilot.press("ctrl+d")
    await pilot.pause()
    assert spy.closed == 1
    assert ("stdin_eof", []) in app.recorder.records


async def test_disabled_hides_input_and_focus_goes_to_log(app):
    app, pilot = app
    console = _console(app)
    console.set_stdin_enabled(False)
    await pilot.pause()
    assert not console.query_one(Input).display
    app.action_focus_console()
    await pilot.pause()
    assert console.query_one(RichLog).has_focus


async def test_write_failure_is_reported_not_raised(app, monkeypatch):
    app, pilot = app
    spy = _StdinSpy(supports=False)
    monkeypatch.setattr(
        type(app.controller), "supports_stdin", property(lambda s: True), raising=False
    )
    monkeypatch.setattr(app.controller, "write_stdin", spy.write_stdin)
    notes: list[str] = []
    monkeypatch.setattr(app, "notify", lambda msg, **kw: notes.append(msg))
    console = _console(app)
    console.set_stdin_enabled(True)
    console.focus()
    await pilot.press("x", "enter")
    await pilot.pause()
    assert notes and "no stdin here" in notes[0]


async def test_exited_disables_input(app, monkeypatch):
    from tdb.session.messages import DapExited

    app, pilot = app
    console = _console(app)
    console.set_stdin_enabled(True)
    app.post_message(DapExited(0))
    await pilot.pause()
    assert not console.query_one(Input).display


# --- RPC / replay actions -----------------------------------------------


class _StubController:
    def __init__(self, supports=True):
        self.supports_stdin = supports
        self.written: list[str] = []
        self.closed = 0
        self.state = type("S", (), {"is_terminated": False})()

    async def write_stdin(self, text):
        if not self.supports_stdin:
            raise RuntimeError("stdin is not available")
        self.written.append(text)

    async def close_stdin(self):
        self.closed += 1


def _handlers(ctrl) -> RpcHandlers:
    return RpcHandlers(ControllerRef(ctrl), ServerEventHandler())


async def test_rpc_stdin_action_appends_newline():
    ctrl = _StubController()
    h = _handlers(ctrl)
    resp = await h.dispatch_table()["stdin"](["42"])
    assert resp.success
    assert ctrl.written == ["42\n"]


async def test_rpc_stdin_eof_action():
    ctrl = _StubController()
    h = _handlers(ctrl)
    resp = await h.dispatch_table()["stdin_eof"]([])
    assert resp.success
    assert ctrl.closed == 1


async def test_rpc_stdin_action_errors_when_unsupported():
    ctrl = _StubController(supports=False)
    h = _handlers(ctrl)
    resp = await h.dispatch_table()["stdin"](["42"])
    assert not resp.success
    assert "stdin" in resp.value

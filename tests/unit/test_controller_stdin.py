"""DebugController's program-stdin surface.

Two routes, chosen by the adapter's `stdin_route` quirk:
  - "run_in_terminal" (debugpy): tdb asks for an external terminal and
    answers the runInTerminal request itself with a PipeLauncher, so it
    owns the debuggee's stdin/stdout pipes.
  - "request" (tdb-owned adapters): the adapter spawns the debuggee on
    a pipe and forwards a `tdbStdin` request into it.
Everything else (--terminal, remote attach, gdb/lldb/dlv) has no stdin.
"""

from __future__ import annotations

import asyncio

import pytest

from tdb.languages.base import (
    AdapterQuirks,
    AdapterSpec,
    LanguageProfile,
    Presentation,
    ProfileCapabilities,
)
from tdb.languages.python import PYTHON_PROFILE
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController
from tdb.session.terminal import PipeLauncher, TerminalLauncher


class _StubDAPClient:
    def __init__(self) -> None:
        self.reverse_handlers: dict = {}
        self.launch_kwargs: dict | None = None
        self.initialize_kwargs: dict | None = None
        self.stdin_calls: list = []

    def on_event(self, name, fn):
        pass

    def on_reverse_request(self, name, fn):
        self.reverse_handlers[name] = fn

    async def start(self):
        return None

    async def initialize(self, support_run_in_terminal=False):
        self.initialize_kwargs = {"support_run_in_terminal": support_run_in_terminal}
        return None

    async def launch(self, **kwargs):
        self.launch_kwargs = kwargs
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        fut.set_result(None)
        return fut

    async def send_stdin(self, text=None, *, eof=False):
        self.stdin_calls.append((text, eof))


class _RequestAdapter(AdapterSpec):
    id = "fake-owned"
    quirks = AdapterQuirks(stdin_route="request")

    def command(self):
        return ["true"]

    def launch_body(self, **kw):
        return {"console": kw["console"]}


def _request_profile() -> LanguageProfile:
    return LanguageProfile(
        id="fake",
        display_name="Fake",
        adapter=_RequestAdapter(),
        presentation=Presentation(),
        capabilities=ProfileCapabilities(),
    )


def _controller(profile=PYTHON_PROFILE) -> DebugController:
    ctrl = DebugController(ServerEventHandler(), profile=profile)
    ctrl.client = _StubDAPClient()  # type: ignore[assignment]
    return ctrl


def test_stdin_route_quirk_per_adapter():
    from tdb.languages.bash import BashAdapter
    from tdb.languages.cpp import GdbDapAdapter, LldbDapAdapter
    from tdb.languages.go import DelveAdapter
    from tdb.languages.perl import PerlAdapter
    from tdb.languages.powershell import PsesAdapter
    from tdb.languages.ruby import RdbgAdapter
    from tdb.languages.tcsh import TcshAdapter

    assert PYTHON_PROFILE.adapter.quirks.stdin_route == "run_in_terminal"
    for cls in (BashAdapter, PerlAdapter, PsesAdapter, RdbgAdapter, TcshAdapter):
        assert cls.quirks.stdin_route == "request", cls
    for cls in (GdbDapAdapter, LldbDapAdapter, DelveAdapter):
        assert cls.quirks.stdin_route is None, cls


async def test_python_launch_owns_stdin_via_pipe_launcher(tmp_path):
    ctrl = _controller()
    await ctrl.start(program=str(tmp_path / "p.py"))
    assert ctrl.client.initialize_kwargs == {"support_run_in_terminal": True}
    assert ctrl.client.launch_kwargs["console"] == "externalTerminal"
    handler = ctrl.client.reverse_handlers["runInTerminal"]
    assert isinstance(handler.__self__, PipeLauncher)
    assert ctrl.supports_stdin is True


async def test_terminal_mode_has_no_stdin(tmp_path):
    ctrl = _controller()
    await ctrl.start(program=str(tmp_path / "p.py"), terminal="xterm")
    handler = ctrl.client.reverse_handlers["runInTerminal"]
    assert isinstance(handler.__self__, TerminalLauncher)
    assert ctrl.supports_stdin is False
    with pytest.raises(RuntimeError):
        await ctrl.write_stdin("x\n")


async def test_headless_inherits_stdin(tmp_path):
    ctrl = _controller()
    await ctrl.start(program=str(tmp_path / "p.py"), stdin_mode="inherit")
    handler = ctrl.client.reverse_handlers["runInTerminal"]
    assert isinstance(handler.__self__, PipeLauncher)
    assert ctrl.supports_stdin is False


async def test_request_route_forwards_through_adapter(tmp_path):
    ctrl = _controller(_request_profile())
    await ctrl.start(program=str(tmp_path / "p"))
    assert ctrl.client.launch_kwargs["console"] == "internalConsole"
    assert "runInTerminal" not in ctrl.client.reverse_handlers
    assert ctrl.supports_stdin is True
    await ctrl.write_stdin("42\n")
    await ctrl.close_stdin()
    assert ctrl.client.stdin_calls == [("42\n", False), (None, True)]


async def test_request_route_in_terminal_mode_has_no_stdin(tmp_path):
    ctrl = _controller(_request_profile())
    await ctrl.start(program=str(tmp_path / "p"), terminal="xterm")
    assert ctrl.supports_stdin is False


async def test_pipe_launcher_write_goes_to_launcher(tmp_path):
    ctrl = _controller()
    await ctrl.start(program=str(tmp_path / "p.py"))
    launcher = ctrl.client.reverse_handlers["runInTerminal"].__self__
    written: list[str] = []
    launcher.write_stdin = written.append  # type: ignore[method-assign]
    await ctrl.write_stdin("hi\n")
    assert written == ["hi\n"]

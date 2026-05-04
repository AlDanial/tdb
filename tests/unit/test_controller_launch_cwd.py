"""Regression: debuggee cwd defaults to the shell's cwd, not program.parent.

`tdb examples/foo.py` invoked from `work/` must leave the debuggee's
`os.getcwd()` at `work/`, not silently chdir into `work/examples/`.
The user expects relative paths in the debuggee to resolve the same
way they would if `python examples/foo.py` had been run directly.

This file pins both the default fallback and the explicit-override
contract on `DebugController.start()`. Without these tests the next
person to touch the launch path could re-introduce the
`Path(program).parent` fallback without anything failing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController


class _StubDAPClient:
    """Just enough surface for `controller.start()` to run without a real
    debugpy subprocess. Records what `launch` was called with so the
    test can assert on the cwd that actually got sent over DAP."""

    def __init__(self) -> None:
        self.events: dict = {}
        self.reverse_handlers: dict = {}
        self.launch_kwargs: dict | None = None

    def on_event(self, name, fn):
        self.events[name] = fn

    def on_reverse_request(self, name, fn):
        self.reverse_handlers[name] = fn

    async def start(self):
        return None

    async def initialize(self, support_run_in_terminal=False):
        return None

    async def launch(self, **kwargs):
        self.launch_kwargs = kwargs
        # controller.start stores this as `_launch_future` and only
        # awaits it later (debugpy holds the response until
        # configurationDone). Return an already-resolved Future so
        # nothing leaks an un-awaited coroutine.
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        fut.set_result(None)
        return fut


async def test_default_cwd_is_shell_cwd_not_program_parent(tmp_path, monkeypatch):
    """The whole point of the regression fix: when no --cwd is passed,
    the debuggee inherits the shell's cwd, not the program's parent."""
    program = tmp_path / "subdir" / "prog.py"
    program.parent.mkdir()
    program.write_text("\n")

    # Pretend the user ran tdb from a totally different directory.
    shell_cwd = tmp_path / "elsewhere"
    shell_cwd.mkdir()
    monkeypatch.chdir(shell_cwd)

    ctrl = DebugController(ServerEventHandler())
    ctrl.client = _StubDAPClient()  # type: ignore[assignment]

    await ctrl.start(program=str(program))

    assert ctrl._launch_params["cwd"] == str(shell_cwd)
    assert ctrl.client.launch_kwargs is not None
    assert ctrl.client.launch_kwargs["cwd"] == str(shell_cwd)
    # And critically NOT the program's parent — that was the old bug.
    assert ctrl._launch_params["cwd"] != str(program.parent)


async def test_explicit_cwd_overrides_default(tmp_path, monkeypatch):
    """`--cwd` must still win over the fallback — otherwise users lose
    the escape hatch."""
    program = tmp_path / "prog.py"
    program.write_text("\n")
    monkeypatch.chdir(tmp_path)

    explicit = tmp_path / "explicit"
    explicit.mkdir()

    ctrl = DebugController(ServerEventHandler())
    ctrl.client = _StubDAPClient()  # type: ignore[assignment]

    await ctrl.start(program=str(program), cwd=str(explicit))

    assert ctrl._launch_params["cwd"] == str(explicit)
    assert ctrl.client.launch_kwargs["cwd"] == str(explicit)


async def test_no_program_parent_fallback_anywhere_in_launch_path():
    """Belt-and-suspenders: the literal `Path(program).parent` fallback
    must not appear in the cwd defaulting on the start() path. If
    someone re-introduces it as `cwd or str(Path(program).parent)`,
    this fails — which is the whole point of this regression file."""
    src = Path(__file__).resolve().parents[2] / "src" / "tdb"
    offenders: list[str] = []
    for path in [
        src / "session" / "controller.py",
        src / "server" / "runner.py",
    ]:
        text = path.read_text()
        # Only flag the dangerous fallback pattern (cwd default), not
        # comments or unrelated uses of program.parent.
        if "cwd or str(Path(program).parent)" in text:
            offenders.append(str(path))
    assert offenders == [], (
        f"Found re-introduced cwd fallback to program.parent in: {offenders}"
    )

"""Inspection results are published only into the stop they describe.

Codex review #5: an awaited scopes()/variables()/stackTrace() reply that
lands after the session resumed (or after the user picked another frame,
thread or process) must not write its old data back into the state.
"""

from __future__ import annotations

import asyncio

from tdb.dap.types import Scope, Variable
from tdb.session.state import SessionPhase

from tests.unit.test_controller_actions import _FakeDAP, _make


def _resume(ctrl) -> None:
    """What a `continued` event does to the state."""
    ctrl.state.transition_to(SessionPhase.RUNNING)
    ctrl.state.clear_frame_data()


class _HookedDAP(_FakeDAP):
    """Runs `hooks[method]` (once) while that request is 'in flight'."""

    def __init__(self) -> None:
        super().__init__()
        self.hooks: dict = {}

    async def _in_flight(self, method: str) -> None:
        hook = self.hooks.pop(method, None)
        if hook is not None:
            result = hook()
            if asyncio.iscoroutine(result):
                await result

    async def scopes(self, frame_id):
        await self._in_flight("scopes")
        return await super().scopes(frame_id)

    async def variables(self, ref, start=0, count=0):
        await self._in_flight("variables")
        return await super().variables(ref, start, count)

    async def stack_trace(self, thread_id, start_frame=0, levels=20):
        await self._in_flight("stackTrace")
        return await super().stack_trace(thread_id, start_frame, levels)


def _hooked():
    ctrl, _, handler = _make()
    fake = _HookedDAP()
    ctrl.client = fake
    ctrl._active_client = fake
    ctrl.state.set_stack(list(fake.frames_result))
    return ctrl, fake


async def test_continue_during_scopes_does_not_restore_old_locals():
    ctrl, fake = _hooked()
    fake.hooks["scopes"] = lambda: _resume(ctrl)
    await ctrl.fetch_scopes_and_variables(11)
    assert ctrl.state.phase is SessionPhase.RUNNING
    assert ctrl.state.scopes == []
    assert ctrl.state.variables == {}


async def test_continue_during_variables_does_not_restore_old_locals():
    ctrl, fake = _hooked()
    fake.hooks["variables"] = lambda: _resume(ctrl)
    await ctrl.fetch_scopes_and_variables(11)
    assert ctrl.state.scopes == []
    assert ctrl.state.variables == {}


async def test_continue_during_stack_fetch_publishes_nothing():
    ctrl, fake = _hooked()
    ctrl.state.set_stack([])
    fake.hooks["stackTrace"] = lambda: _resume(ctrl)
    await ctrl.fetch_stop_info()
    assert ctrl.state.stack_frames == []
    assert fake.calls_to("scopes") == []


async def test_older_frame_fetch_finishing_last_does_not_win():
    """Frame 11's fetch is slow; the user picks frame 12 meanwhile."""
    ctrl, fake = _hooked()
    release = asyncio.Event()

    async def slow():
        await release.wait()

    fake.hooks["variables"] = slow
    first = asyncio.create_task(ctrl.fetch_scopes_and_variables(11))
    await asyncio.sleep(0)
    fake.scopes_result = [Scope(name="Locals12", variables_reference=200)]
    fake.variables_result = [Variable(name="y", value="frame-12")]
    await ctrl.select_frame(12)
    release.set()
    await first
    assert ctrl.state.current_frame_id == 12
    assert [s.name for s in ctrl.state.scopes] == ["Locals12"]
    assert set(ctrl.state.variables) == {200}


async def test_switching_process_during_fetch_drops_the_old_result():
    ctrl, fake = _hooked()
    other = _FakeDAP("child")
    other.scopes_result = [Scope(name="ChildLocals", variables_reference=200)]

    def switch():
        ctrl._active_client = other

    fake.hooks["variables"] = switch
    scopes_before, vars_before = list(ctrl.state.scopes), dict(ctrl.state.variables)
    await ctrl.fetch_scopes_and_variables(11)
    assert ctrl.state.scopes == scopes_before
    assert ctrl.state.variables == vars_before


async def test_resume_during_thread_switch_publishes_nothing():
    ctrl, fake = _hooked()
    ctrl.state.set_stack([])
    fake.hooks["stackTrace"] = lambda: _resume(ctrl)
    await ctrl.switch_active_thread(1)
    assert ctrl.state.stack_frames == []
    assert fake.calls_to("scopes") == []

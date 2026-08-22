"""Initial frame selection after a stop skips language-opaque frames.

When a pause lands inside a native call, rdbg reports a C frame like
"[C] Kernel#sleep" at the top of the stack. Such frames have no
inspectable locals (rdbg's Local variables scope holds only %self), so
the Variables view would open empty. The language profile can mark
frames opaque via ``capabilities.opaque_frame``; the controller then
lands on the first non-opaque frame instead — while the Stack view
still shows the full stack, C frame included.
"""

from dataclasses import replace

from tdb.dap.types import Source, StackFrame
from tdb.languages.python import PYTHON_PROFILE
from tdb.languages.ruby import build_ruby_profile

from tests.unit.test_controller_actions import _FakeDAP, _make


def _c_frame_profile():
    caps = replace(
        PYTHON_PROFILE.capabilities,
        opaque_frame=lambda name: name.startswith("[C] "),
    )
    return replace(PYTHON_PROFILE, capabilities=caps)


def _sleep_stack() -> list[StackFrame]:
    src = Source(path="/sleep.rb")
    return [
        StackFrame(id=11, name="[C] Kernel#sleep", source=src, line=4),
        StackFrame(id=12, name="block in <main>", source=src, line=4),
        StackFrame(id=13, name="<main>", source=src, line=2),
    ]


async def test_fetch_stop_info_skips_opaque_top_frame():
    ctrl, fake, _ = _make(with_frames=False, profile=_c_frame_profile())
    fake.frames_result = _sleep_stack()
    await ctrl.fetch_stop_info()
    # Full stack is preserved (Stack view still shows the C frame) …
    assert ctrl.state.stack_frames == fake.frames_result
    # … but the selected frame, and the scopes fetch, land on the first
    # frame that actually has locals.
    assert ctrl.state.current_frame_id == 12
    assert fake.calls_to("scopes") == [("scopes", 12)]


async def test_fetch_stop_info_falls_back_to_top_when_all_opaque():
    ctrl, fake, _ = _make(with_frames=False, profile=_c_frame_profile())
    fake.frames_result = [
        StackFrame(id=21, name="[C] Kernel#sleep", line=1),
        StackFrame(id=22, name="[C] Kernel#loop", line=1),
    ]
    await ctrl.fetch_stop_info()
    assert ctrl.state.current_frame_id == 21
    assert fake.calls_to("scopes") == [("scopes", 21)]


async def test_fetch_stop_info_unchanged_without_opaque_predicate():
    ctrl, fake, _ = _make(with_frames=False)  # default python profile
    fake.frames_result = _sleep_stack()
    await ctrl.fetch_stop_info()
    assert ctrl.state.current_frame_id == 11
    assert fake.calls_to("scopes") == [("scopes", 11)]


async def test_switch_active_thread_skips_opaque_top_frame():
    ctrl, fake, _ = _make(with_frames=False, profile=_c_frame_profile())
    fake.frames_result = _sleep_stack()
    await ctrl.switch_active_thread(1)
    assert ctrl.state.current_frame_id == 12
    assert fake.calls_to("scopes") == [("scopes", 12)]


async def test_switch_active_process_skips_opaque_top_frame():
    ctrl, fake, _ = _make(with_frames=False, profile=_c_frame_profile())
    child = _FakeDAP(name="child")
    child.frames_result = _sleep_stack()
    ctrl.child_clients = {123: child}
    ctrl.get_child_client = lambda pid: child if pid == 123 else None
    await ctrl.switch_active_process(123)
    assert ctrl.state.current_frame_id == 12
    assert child.calls_to("scopes") == [("scopes", 12)]


def test_ruby_profile_marks_c_frames_opaque():
    opaque = build_ruby_profile().capabilities.opaque_frame
    assert opaque is not None
    assert opaque("[C] Kernel#sleep") is True
    assert opaque("block in <main>") is False
    assert opaque("<main>") is False

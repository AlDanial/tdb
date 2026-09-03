"""tests/unit/test_go_inspect_service.py"""

import pytest

from tdb.session.inspect_service import InspectService, SessionGateError


class _Caps:
    concurrency_inspection = "go"
    task_inspection = False


class _Profile:
    capabilities = _Caps()


class _State:
    is_terminated = False
    is_running = False


class _Ctrl:
    profile = _Profile()
    state = _State()


@pytest.mark.asyncio
async def test_go_gate_rejects_rust_and_python_profiles():
    ctrl = _Ctrl()
    svc = InspectService(lambda: ctrl)
    ctrl.profile.capabilities.concurrency_inspection = None
    with pytest.raises(SessionGateError) as e:
        await svc.collect_go_concurrency()
    assert e.value.reason == "unsupported"
    ctrl.profile.capabilities.concurrency_inspection = "rust"
    with pytest.raises(SessionGateError):
        await svc.collect_go_concurrency()


@pytest.mark.asyncio
async def test_rust_gate_still_works():
    ctrl = _Ctrl()
    ctrl.profile.capabilities.concurrency_inspection = "go"
    svc = InspectService(lambda: ctrl)
    with pytest.raises(SessionGateError):
        await svc.collect_rust_concurrency()

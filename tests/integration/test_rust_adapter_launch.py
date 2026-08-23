"""Real local launches through every Rust DAP adapter available here."""

from __future__ import annotations

import pytest

from tdb.session.state import SessionPhase
from tests.integration.rust_adapter_harness import (
    available_rust_adapters,
    launch_and_pause,
    rust_debug_binary as _rust_debug_binary,  # noqa: F401 - registers fixture
)


@pytest.mark.parametrize("adapter", available_rust_adapters())
async def test_local_launch_reaches_a_real_stopped_rust_stack(
    adapter, rust_debug_binary
):
    ctrl = await launch_and_pause(rust_debug_binary("park", adapter), adapter)
    try:
        assert ctrl.profile.id == "rust"
        assert ctrl.profile.adapter.id == adapter
        assert ctrl.state.phase is SessionPhase.STOPPED
        assert ctrl.state.stack_frames
    finally:
        await ctrl.stop()

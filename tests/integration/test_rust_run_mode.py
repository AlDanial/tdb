"""Real Rust run-mode pause through the public readiness callback."""

from __future__ import annotations

import pytest

from tests.integration.rust_adapter_harness import (
    available_rust_adapters,
    run_mode_pause_probe,
    _rust_debug_binary,  # noqa: F401 - registers rust_debug_binary fixture
)

# With no adapter installed the parametrize lists below would be empty
# and pytest's default 'empty parameter set' skip hides the real reason.
pytestmark = pytest.mark.skipif(
    not available_rust_adapters(),
    reason="Rust debugging requires gdb >= 14 or lldb-dap (LLVM >= 17)",
)


@pytest.mark.parametrize("adapter", available_rust_adapters())
async def test_run_mode_pauses_blocked_rust_program(adapter, rust_debug_binary):
    result = await run_mode_pause_probe(rust_debug_binary("park", adapter), adapter)

    assert result.paused is True
    assert result.adopted is True
    assert result.resumed is True
    assert result.terminated is True
    assert result.episode_count >= 1

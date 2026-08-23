"""Real Rust run-mode pause through the public readiness callback."""

from __future__ import annotations

import pytest

from tests.integration.rust_adapter_harness import (
    available_rust_adapters,
    run_mode_pause_probe,
    rust_debug_binary as _rust_debug_binary,  # noqa: F401 - registers fixture
)


@pytest.mark.parametrize("adapter", available_rust_adapters())
async def test_run_mode_pauses_blocked_rust_program(adapter, rust_debug_binary):
    assert await run_mode_pause_probe(rust_debug_binary("park", adapter), adapter)

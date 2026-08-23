"""Native Rust remote attach through real GDB/LLDB remote stubs."""

from __future__ import annotations

import pytest

from tests.integration.rust_adapter_harness import (
    available_rust_adapters,
    remote_attach_probe,
    rust_debug_binary as _rust_debug_binary,  # noqa: F401 - registers fixture
)


@pytest.mark.parametrize("adapter", available_rust_adapters())
async def test_remote_stub_attach_exposes_rust_stack(adapter, rust_debug_binary):
    assert await remote_attach_probe(rust_debug_binary("park", adapter), adapter)

"""Native Rust remote attach through real GDB/LLDB remote stubs."""

from __future__ import annotations

import pytest

from tests.integration.rust_adapter_harness import (
    available_rust_adapters,
    remote_attach_probe,
    _rust_debug_binary,  # noqa: F401 - registers rust_debug_binary fixture
)

# With no adapter installed the parametrize lists below would be empty
# and pytest's default 'empty parameter set' skip hides the real reason.
pytestmark = pytest.mark.skipif(
    not available_rust_adapters(),
    reason="Rust debugging requires gdb >= 14 or lldb-dap (LLVM >= 17)",
)


@pytest.mark.parametrize("adapter", available_rust_adapters())
async def test_remote_stub_attach_exposes_rust_stack(adapter, rust_debug_binary):
    target = rust_debug_binary("park", adapter)
    evidence = await remote_attach_probe(target, adapter)

    assert any("rust_concurrency" in name for name in evidence.frame_names)
    assert str(target.source_path) in evidence.source_paths
    assert target.compiled_source_path.parent != target.source_path.parent

"""LLDB Rust external-terminal launch through a fake terminal client."""

from __future__ import annotations

import pytest

from tests.integration.rust_adapter_harness import (
    lldb_dap_available,
    rust_debug_binary as _rust_debug_binary,  # noqa: F401 - registers fixture
    terminal_launch_probe,
)


@pytest.mark.skipif(not lldb_dap_available(), reason="lldb-dap not installed")
async def test_lldb_terminal_launch_uses_run_in_terminal(rust_debug_binary):
    request = await terminal_launch_probe(rust_debug_binary("park", "lldb-dap"))

    assert request.command == "runInTerminal"
    # lldb-dap labels this reverse request "integrated".  tdb's
    # TerminalLauncher, not this adapter hint, wraps the requested command in
    # the user's selected external terminal emulator.
    assert request.arguments["kind"] == "integrated"
    assert request.arguments["args"]

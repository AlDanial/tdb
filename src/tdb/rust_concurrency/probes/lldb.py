"""Fixed read-only LLDB probe transport for Rust concurrency evidence."""

from __future__ import annotations

from typing import Any

from tdb.rust_concurrency.models import ProbeResult
from tdb.rust_concurrency.probes.base import gate_supported_layout, parse_probe_output
from tdb.rust_concurrency.probes.gdb import (
    GDB_SNAPSHOT_COMMAND,
    SUPPORTED_RUST_VERSION,
)


LLDB_SNAPSHOT_COMMAND = GDB_SNAPSHOT_COMMAND


def parse_lldb_probe_output(output: str) -> ProbeResult:
    """Parse LLDB output through the shared strict probe envelope parser."""
    return parse_probe_output(output)


class LldbEvidenceProbe:
    """Request the one registered LLDB command; never evaluate user input."""

    async def collect(self, client: Any) -> ProbeResult:
        response = await client.evaluate(LLDB_SNAPSHOT_COMMAND, context="repl")
        output = response[0] if isinstance(response, tuple) else response
        if not isinstance(output, str):
            return ProbeResult(warnings=("invalid Rust probe response",))
        return gate_supported_layout(
            parse_lldb_probe_output(output), supported=SUPPORTED_RUST_VERSION
        )

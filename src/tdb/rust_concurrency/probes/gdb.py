"""Fixed read-only GDB probe transport for Rust concurrency evidence."""

from __future__ import annotations

from typing import Any

from tdb.rust_concurrency.models import ProbeResult
from tdb.rust_concurrency.probes.base import gate_supported_layout, parse_probe_output


GDB_SNAPSHOT_COMMAND = "tdb-rust-snapshot --format json"
SUPPORTED_RUST_VERSION = "1.98.0"


class GdbEvidenceProbe:
    """Request the one registered GDB command; never evaluate user input."""

    async def collect(self, client: Any) -> ProbeResult:
        response = await client.evaluate(GDB_SNAPSHOT_COMMAND, context="repl")
        output = response[0] if isinstance(response, tuple) else response
        if not isinstance(output, str):
            return ProbeResult(
                rust_version=None, warnings=("invalid Rust probe response",)
            )
        return gate_supported_layout(
            parse_probe_output(output), supported=SUPPORTED_RUST_VERSION
        )

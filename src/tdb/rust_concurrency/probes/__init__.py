"""Read-only debugger evidence probes for Rust concurrency snapshots."""

from __future__ import annotations

from tdb.rust_concurrency.probes.gdb import GdbEvidenceProbe
from tdb.rust_concurrency.probes.lldb import LldbEvidenceProbe


def probe_for_adapter(adapter_id: str):
    """Return the supported fixed probe for an adapter, if one exists."""
    if adapter_id == "gdb":
        return GdbEvidenceProbe()
    if adapter_id == "lldb-dap":
        return LldbEvidenceProbe()
    return None


__all__ = ["GdbEvidenceProbe", "LldbEvidenceProbe", "probe_for_adapter"]

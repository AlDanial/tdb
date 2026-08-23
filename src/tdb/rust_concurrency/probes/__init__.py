"""Read-only debugger evidence probes for Rust concurrency snapshots."""

from __future__ import annotations

from tdb.rust_concurrency.probes.gdb import GdbEvidenceProbe


def probe_for_adapter(adapter_id: str):
    """Return the supported fixed probe for an adapter, if one exists."""
    if adapter_id == "gdb":
        return GdbEvidenceProbe()
    return None


__all__ = ["GdbEvidenceProbe", "probe_for_adapter"]

"""GDB-side fixed command for read-only Rust concurrency observations.

This module is sourced by GDB before it enters DAP mode.  It never executes
inferior code or resumes the process: all accesses are GDB Python API reads.
"""

from __future__ import annotations

import json
import re

try:  # Importing this module during normal Python packaging must stay harmless.
    import gdb  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - only happens outside GDB.
    gdb = None  # type: ignore[assignment]


_MARKER = "TDB_RUST_JSON:"
_RUST_VERSION = re.compile(r"rustc(?: version)?\s+([0-9]+\.[0-9]+\.[0-9]+)")
_HEX_ADDRESS = re.compile(r"0x[0-9a-fA-F]+")


def _rust_version() -> str | None:
    """Read DW_AT_producer text exposed by GDB's read-only ``info source``."""
    try:
        source_info = gdb.execute("info source", to_string=True)
    except gdb.error:
        return None
    match = _RUST_VERSION.search(source_info)
    return match.group(1) if match else None


def _os_thread_id(thread) -> str:
    ptid = getattr(thread, "ptid", ())
    for value in tuple(ptid)[1:]:
        if value:
            return str(value)
    return str(getattr(thread, "num", ""))


def _frame_primitive(frame) -> dict[str, object] | None:
    """Read a visible ``self`` value for known lock frames without evaluation."""
    name = frame.name() or ""
    kinds = (
        ("mutex::Mutex", "mutex"),
        ("rwlock::RwLock", "rwlock"),
        ("condvar::Condvar", "condvar"),
    )
    kind = next((value for marker, value in kinds if marker in name), None)
    if kind is None:
        return None
    try:
        value = frame.read_var("self")
        address = _HEX_ADDRESS.search(str(value.address)) if value.address else None
    except gdb.error:
        return None
    if address is None:
        return None
    return {
        "primitive_id": f"{kind}:{address.group(0)}",
        "owner_os_thread_ids": [],
        "raw_state": "observed",
        "evidence": [
            {
                "confidence": "probable",
                "source": "gdb-frame-variable",
                "detail": f"self={address.group(0)}",
            }
        ],
    }


if gdb is not None:  # pragma: no branch - command registration happens in GDB.

    class _RustSnapshotCommand(gdb.Command):
        def __init__(self) -> None:
            super().__init__("tdb-rust-snapshot", gdb.COMMAND_DATA)

        def invoke(self, argument: str, from_tty: bool) -> None:
            if argument.strip() != "--format json":
                raise gdb.GdbError("usage: tdb-rust-snapshot --format json")

            inferior = gdb.selected_inferior()
            selected_thread = gdb.selected_thread()
            threads = []
            states = []
            for thread in inferior.threads():
                threads.append(
                    {
                        "dap_thread_hint": str(getattr(thread, "global_num", thread.num)),
                        "os_thread_id": _os_thread_id(thread),
                    }
                )
                try:
                    thread.switch()
                    frame = gdb.newest_frame()
                    primitive = _frame_primitive(frame) if frame is not None else None
                except gdb.error:
                    primitive = None
                if primitive is not None:
                    states.append(primitive)
            if selected_thread is not None:
                selected_thread.switch()
            payload = {
                "rust_version": _rust_version(),
                "threads": threads,
                "primitive_states": states,
                "warnings": [],
            }
            print(_MARKER + json.dumps(payload, sort_keys=True, separators=(",", ":")))


    _RustSnapshotCommand()

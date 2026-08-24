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
_MAX_THREADS = 256
_MAX_FRAMES_PER_THREAD = 64
_MAX_PRIMITIVES = 1024
_MAX_WARNINGS = 256
_MAX_EVIDENCE_PER_PRIMITIVE = 256


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


def _value_address(value) -> str | None:
    """Return a pointee address for references, otherwise the value address."""
    candidates = []
    try:
        candidates.append(value.referenced_value().address)
    except (gdb.error, RuntimeError, ValueError, AttributeError):
        pass
    try:
        candidates.append(value.address)
    except (gdb.error, RuntimeError, ValueError, AttributeError):
        pass
    for candidate in candidates:
        if candidate is not None:
            match = _HEX_ADDRESS.search(str(candidate))
            if match is not None:
                return match.group(0)
    return None


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
        address = _value_address(value)
    except gdb.error:
        return None
    if address is None:
        return None
    return {
        "primitive_id": f"{kind}:{address}",
        "owner_os_thread_ids": [],
        "raw_state": "observed",
        "evidence": [
            {
                "confidence": "probable",
                "source": "gdb-frame-variable",
                "detail": f"self={address}",
            }
        ],
    }


def _guard_ownership(frame, os_thread_id: str) -> list[dict[str, object]]:
    """Find visible std guard locals; a live guard proves this thread owns it."""
    states = []
    try:
        block = frame.block()
        symbols = []
        block_depth = 0
        while block is not None and block_depth < 32:
            symbols.extend(tuple(block))
            block = getattr(block, "superblock", None)
            block_depth += 1
    except (gdb.error, RuntimeError, ValueError, AttributeError, TypeError):
        return states
    for symbol in symbols:
        try:
            value = frame.read_var(symbol)
            type_name = str(value.type)
            if "MutexGuard" in type_name:
                kind, field_name = "mutex", "lock"
            elif "RwLockReadGuard" in type_name or "RwLockWriteGuard" in type_name:
                kind, field_name = "rwlock", "lock"
            else:
                continue
            lock_value = value[field_name]
            address = _value_address(lock_value)
        except (gdb.error, RuntimeError, ValueError, AttributeError, TypeError):
            continue
        if address is None:
            continue
        states.append(
            {
                "primitive_id": f"{kind}:{address}",
                "owner_os_thread_ids": [os_thread_id],
                "raw_state": "guard-held",
                "evidence": [
                    {
                        "confidence": "confirmed",
                        "source": "gdb-visible-guard",
                        "detail": f"{type_name[:256]} holds {address}",
                    }
                ],
            }
        )
    return states


def _merge_state(states_by_id, state) -> None:
    current = states_by_id.get(state["primitive_id"])
    if current is None:
        states_by_id[state["primitive_id"]] = state
        return
    current["owner_os_thread_ids"] = sorted(
        set(current["owner_os_thread_ids"]) | set(state["owner_os_thread_ids"])
    )
    evidence_by_key = {
        (item["confidence"], item["source"], item["detail"]): item
        for item in current["evidence"] + state["evidence"]
    }
    current["evidence"] = [
        evidence_by_key[key]
        for key in sorted(evidence_by_key)[:_MAX_EVIDENCE_PER_PRIMITIVE]
    ]
    if state["raw_state"] == "guard-held":
        current["raw_state"] = "guard-held"


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
            states_by_id = {}
            warnings = []
            rust_version = None
            try:
                all_inferior_threads = inferior.threads()
                inferior_threads = sorted(
                    all_inferior_threads, key=lambda item: getattr(item, "num", 0)
                )[:_MAX_THREADS]
                if len(all_inferior_threads) > _MAX_THREADS:
                    warnings.append(
                        f"thread evidence truncated at {_MAX_THREADS} threads"
                    )
                for thread in inferior_threads:
                    os_thread_id = _os_thread_id(thread)
                    threads.append(
                        {
                            "dap_thread_hint": str(
                                getattr(thread, "global_num", thread.num)
                            ),
                            "os_thread_id": _os_thread_id(thread),
                        }
                    )
                    try:
                        thread.switch()
                        if rust_version is None:
                            rust_version = _rust_version()
                        frame = gdb.newest_frame()
                        frame_count = 0
                        while (
                            frame is not None and frame_count < _MAX_FRAMES_PER_THREAD
                        ):
                            primitive = _frame_primitive(frame)
                            if primitive is not None:
                                _merge_state(states_by_id, primitive)
                            for owned in _guard_ownership(frame, os_thread_id):
                                _merge_state(states_by_id, owned)
                            frame = frame.older()
                            frame_count += 1
                    except (gdb.error, RuntimeError, ValueError, AttributeError) as exc:
                        warnings.append(
                            f"thread {_os_thread_id(thread)} evidence unavailable: {exc}"
                        )
            except (gdb.error, RuntimeError, ValueError, AttributeError) as exc:
                warnings.append(f"thread enumeration unavailable: {exc}")
            finally:
                if selected_thread is not None:
                    try:
                        selected_thread.switch()
                    except (gdb.error, RuntimeError, ValueError, AttributeError) as exc:
                        warnings.append(f"selected thread restoration failed: {exc}")
            if rust_version is None:
                warnings.append("Rust DW_AT_producer version unavailable")
            payload = {
                "rust_version": rust_version,
                "threads": threads,
                "primitive_states": [
                    states_by_id[key] for key in sorted(states_by_id)[:_MAX_PRIMITIVES]
                ],
                "warnings": [warning[:512] for warning in warnings[:_MAX_WARNINGS]],
            }
            print(_MARKER + json.dumps(payload, sort_keys=True, separators=(",", ":")))

    _RustSnapshotCommand()

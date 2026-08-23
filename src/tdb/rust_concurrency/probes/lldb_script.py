"""LLDB-side fixed command for read-only Rust concurrency observations.

The module is imported by lldb-dap before launch or attach.  Collection uses
only LLDB's object model while the inferior is stopped; it never evaluates an
expression in, resumes, or otherwise executes the inferior.
"""

from __future__ import annotations

import json
import os
import re

try:  # Keep normal-package imports harmless outside an LLDB Python runtime.
    import lldb  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - only exercised inside LLDB.
    lldb = None  # type: ignore[assignment]


_MARKER = "TDB_RUST_JSON:"
_HEX_ADDRESS = re.compile(r"0x[0-9a-fA-F]+")
_RUST_VERSION = re.compile(r"rustc(?: version)?\s+([0-9]+\.[0-9]+\.[0-9]+)")
_RUST_VERSION_BYTES = re.compile(rb"rustc(?: version)?\s+([0-9]+\.[0-9]+\.[0-9]+)")
_MAX_VERSION_SCAN_BYTES = 64 * 1024 * 1024
_MAX_THREADS = 256
_MAX_FRAMES_PER_THREAD = 64
_MAX_PRIMITIVES = 1024
_MAX_WARNINGS = 256
_MAX_EVIDENCE_PER_PRIMITIVE = 256


def _rust_version(target) -> tuple[str | None, tuple[str, ...]]:
    """Read compile-unit producer text, with a local debug-file fallback."""
    for module_index in range(target.GetNumModules()):
        module = target.GetModuleAtIndex(module_index)
        for unit_index in range(module.GetNumCompileUnits()):
            unit = module.GetCompileUnitAtIndex(unit_index)
            producer_getter = getattr(unit, "GetProducer", None)
            producer = producer_getter() if callable(producer_getter) else ""
            if isinstance(producer, str):
                match = _RUST_VERSION.search(producer)
                if match is not None:
                    return match.group(1), ()
    executable = target.GetExecutable()
    path_getter = getattr(executable, "GetPath", None)
    path = path_getter() if callable(path_getter) else ""
    if not path:
        return None, ()
    try:
        stat_result = os.stat(path)
        if not os.path.isfile(path) or stat_result.st_size > _MAX_VERSION_SCAN_BYTES:
            return None, ("local executable is not a bounded regular file",)
        with open(path, "rb") as executable_file:
            data = executable_file.read(_MAX_VERSION_SCAN_BYTES + 1)
        if len(data) > _MAX_VERSION_SCAN_BYTES:
            return None, ("local executable is not a bounded regular file",)
        versions = {
            match.decode("ascii") for match in _RUST_VERSION_BYTES.findall(data)
        }
    except (OSError, ValueError):
        return None, ("local executable producer scan unavailable",)
    if len(versions) != 1:
        return None, ("local executable Rust producer version is ambiguous",)
    candidate = next(iter(versions))
    return None, (
        "unverified local executable Rust version candidate "
        f"{candidate}; layout evidence remains disabled",
    )


def _thread_hint(thread) -> str:
    return f"thread #{thread.GetIndexID()}"


def _os_thread_id(thread) -> str:
    return str(thread.GetThreadID())


def _frame_primitive(frame, target) -> dict[str, object] | None:
    """Read a visible synchronization value without calling into Rust code."""
    name = frame.GetFunctionName() or ""
    kinds = (
        ("mutex::Mutex", "mutex"),
        ("rwlock::RwLock", "rwlock"),
        ("condvar::Condvar", "condvar"),
    )
    kind = next((value for marker, value in kinds if marker in name), None)
    if kind is None:
        return None
    value = frame.FindVariable("self")
    if not value.IsValid():
        return None
    address_text = value.GetValue() or ""
    match = _HEX_ADDRESS.search(address_text)
    if match is None:
        address_of = value.AddressOf()
        if address_of.IsValid():
            load_address = address_of.GetValueAsUnsigned()
            if load_address:
                match = _HEX_ADDRESS.search(hex(load_address))
    if match is None:
        return None
    address = match.group(0)
    return {
        "primitive_id": f"{kind}:{address}",
        "owner_os_thread_ids": [],
        "raw_state": "observed",
        "evidence": [
            {
                "confidence": "probable",
                "source": "lldb-frame-variable",
                "detail": f"self={address}",
            }
        ],
    }


def _guard_ownership(frame, os_thread_id: str) -> list[dict[str, object]]:
    """Find visible std guard locals without evaluating target expressions."""
    states = []
    try:
        variables = frame.GetVariables(True, True, False, True)
        count = variables.GetSize()
    except Exception:
        return states
    for index in range(count):
        try:
            value = variables.GetValueAtIndex(index)
            type_name = value.GetTypeName() or ""
            if "MutexGuard" in type_name:
                kind = "mutex"
            elif "RwLockReadGuard" in type_name or "RwLockWriteGuard" in type_name:
                kind = "rwlock"
            else:
                continue
            lock_value = value.GetChildMemberWithName("lock")
            if not lock_value.IsValid():
                continue
            numeric = lock_value.GetValueAsUnsigned()
            if not numeric:
                address_of = lock_value.AddressOf()
                numeric = address_of.GetValueAsUnsigned() if address_of.IsValid() else 0
            if not numeric:
                continue
            address = hex(numeric)
        except Exception:
            continue
        states.append(
            {
                "primitive_id": f"{kind}:{address}",
                "owner_os_thread_ids": [os_thread_id],
                "raw_state": "guard-held",
                "evidence": [
                    {
                        "confidence": "confirmed",
                        "source": "lldb-visible-guard",
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


def tdb_rust_snapshot(debugger, command, result, internal_dict) -> None:
    """Emit exactly one strict JSON envelope for the fixed snapshot command."""
    if command.strip() != "--format json":
        result.SetError("usage: tdb-rust-snapshot --format json")
        return
    threads: list[dict[str, str]] = []
    states_by_id: dict[str, dict[str, object]] = {}
    warnings: list[str] = []
    rust_version = None
    try:
        target = debugger.GetSelectedTarget()
        process = target.GetProcess()
        stopped = lldb is not None and process.GetState() == lldb.eStateStopped
    except Exception as exc:
        warnings.append(f"LLDB target unavailable: {exc}")
        stopped = False
        target = None
        process = None
    if not stopped:
        warnings.append("inferior is not stopped")
    else:
        try:
            rust_version, version_warnings = _rust_version(target)
            warnings.extend(version_warnings)
        except Exception as exc:
            warnings.append(f"Rust producer metadata unavailable: {exc}")
        try:
            actual_thread_count = process.GetNumThreads()
            thread_count = min(actual_thread_count, _MAX_THREADS)
            if actual_thread_count > _MAX_THREADS:
                warnings.append(f"thread evidence truncated at {_MAX_THREADS} threads")
        except Exception as exc:
            warnings.append(f"LLDB thread enumeration unavailable: {exc}")
            thread_count = 0
        process_threads = []
        for index in range(thread_count):
            try:
                process_threads.append(process.GetThreadAtIndex(index))
            except Exception as exc:
                warnings.append(f"LLDB thread index {index} unavailable: {exc}")
        for thread in process_threads:
            thread_id = "unknown"
            try:
                if not thread.IsValid():
                    continue
                thread_id = _os_thread_id(thread)
                threads.append(
                    {
                        "dap_thread_hint": _thread_hint(thread),
                        "os_thread_id": thread_id,
                    }
                )
                frame_count = min(thread.GetNumFrames(), _MAX_FRAMES_PER_THREAD)
                for frame_index in range(frame_count):
                    frame = thread.GetFrameAtIndex(frame_index)
                    if not frame.IsValid():
                        continue
                    primitive = _frame_primitive(frame, target)
                    if primitive is not None:
                        _merge_state(states_by_id, primitive)
                    for owned in _guard_ownership(frame, thread_id):
                        _merge_state(states_by_id, owned)
            except Exception as exc:  # LLDB wraps failures in Python exceptions.
                warnings.append(f"thread {thread_id} evidence unavailable: {exc}")
    if rust_version is None:
        warnings.append("Rust DW_AT_producer version unavailable")
    payload = {
        "rust_version": rust_version,
        "threads": sorted(
            threads,
            key=lambda thread: (thread["os_thread_id"], thread["dap_thread_hint"]),
        ),
        "primitive_states": [
            states_by_id[key] for key in sorted(states_by_id)[:_MAX_PRIMITIVES]
        ],
        "warnings": [
            warning[:512] for warning in sorted(set(warnings))[:_MAX_WARNINGS]
        ],
    }
    result.PutCString(
        _MARKER + json.dumps(payload, sort_keys=True, separators=(",", ":"))
    )


def __lldb_init_module(debugger, internal_dict) -> None:
    """Register the fixed command when LLDB imports this module."""
    debugger.HandleCommand(
        f"command script add -f {__name__}.tdb_rust_snapshot tdb-rust-snapshot"
    )

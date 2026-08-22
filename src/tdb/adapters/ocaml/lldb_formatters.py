"""OCaml value decoding for lldb, loaded INTO lldb's Python via
`command script import` (see OCamlLldbAdapter.launch_body).

Layout (64-bit, spec: "Variable inspection"):
  odd word  -> immediate: int n encoded as 2n+1 (may really be bool/char/
               constructor — DWARF can't tell us, so show both forms)
  even word -> pointer to a heap block; header word at ptr-8:
               tag = header & 0xff, size (words) = header >> 10

Pure decoding lives in `describe_value` (unit-tested without lldb);
the lldb API is confined to the provider glue at the bottom.
"""

from __future__ import annotations

import json
import struct
from typing import Callable

WORD = 8
MAX_DEPTH = 3
MAX_FIELDS = 16

_STRING_TAG = 252
_DOUBLE_TAG = 253
_DOUBLE_ARRAY_TAG = 254
_CUSTOM_TAG = 255
_ABSTRACT_TAG = 251
_CLOSURE_TAG = 247
_OBJECT_TAG = 248
_INFIX_TAG = 249
_FORWARD_TAG = 250
_LAZY_TAG = 246

ReadMemory = Callable[[int, int], "bytes | None"]


def describe_value(
    word: int, read_memory: ReadMemory, depth: int = 0
) -> tuple[str, list[tuple[str, int]]]:
    """Decode one OCaml value word.

    Returns (summary, children) where children are (display_name, word)
    pairs for expandable block fields (empty for leaves). Any unreadable
    memory degrades to a raw-pointer summary — never raises.
    """
    if word & 1:
        return f"{word >> 1} (int, raw {hex(word)})", []
    ptr = word
    header_raw = read_memory(ptr - WORD, WORD)
    if header_raw is None or len(header_raw) < WORD:
        return f"<unreadable {hex(ptr)}>", []
    header = struct.unpack("<Q", header_raw)[0]
    tag = header & 0xFF
    size = header >> 10

    if tag == _STRING_TAG:
        return _decode_string(ptr, size, read_memory), []
    if tag == _DOUBLE_TAG:
        raw = read_memory(ptr, WORD)
        if raw is None:
            return f"<unreadable float {hex(ptr)}>", []
        return repr(struct.unpack("<d", raw)[0]), []
    if tag == _DOUBLE_ARRAY_TAG:
        vals = []
        for i in range(min(size, MAX_FIELDS)):
            raw = read_memory(ptr + i * WORD, WORD)
            vals.append(repr(struct.unpack("<d", raw)[0]) if raw else "?")
        suffix = ", ..." if size > MAX_FIELDS else ""
        return f"float array [{'; '.join(vals)}{suffix}]", []
    if tag in (_CLOSURE_TAG, _INFIX_TAG):
        return "fun (closure)", []
    if tag == _CUSTOM_TAG:
        return f"custom block (size={size})", []
    if tag == _ABSTRACT_TAG:
        return f"abstract block (size={size})", []
    if tag == _OBJECT_TAG:
        return f"object (size={size})", []
    if tag == _LAZY_TAG:
        return "lazy", []
    if tag == _FORWARD_TAG:
        raw = read_memory(ptr, WORD)
        if raw is not None:
            return describe_value(struct.unpack("<Q", raw)[0], read_memory, depth)
        return f"<forward {hex(ptr)}>", []

    # Plain structured block: tuple / record / constructor with args.
    children: list[tuple[str, int]] = []
    if depth < MAX_DEPTH:
        for i in range(min(size, MAX_FIELDS)):
            raw = read_memory(ptr + i * WORD, WORD)
            if raw is None:
                break
            children.append((f"[{i}]", struct.unpack("<Q", raw)[0]))
    return f"block(tag={tag}, size={size})", children


def _decode_string(ptr: int, size: int, read_memory: ReadMemory) -> str:
    data = read_memory(ptr, size * WORD)
    if data is None:
        return f"<unreadable string {hex(ptr)}>"
    padding = data[-1]
    raw = data[: size * WORD - 1 - padding]
    try:
        return json.dumps(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return f"bytes {raw[:32]!r}{'...' if len(raw) > 32 else ''}"


# --- lldb glue (only reachable inside lldb's embedded Python) -------------


def _read_via_process(process):
    import lldb  # noqa: F401  (import here: absent under pytest)

    def read(addr: int, size: int):
        err = __import__("lldb").SBError()
        data = process.ReadMemory(addr, size, err)
        return data if err.Success() else None

    return read


def ocaml_value_summary(valobj, _internal_dict):
    """Type summary for OCaml `value`-typed variables."""
    try:
        word = valobj.GetValueAsUnsigned()
        read = _read_via_process(valobj.GetProcess())
        summary, _children = describe_value(word, read)
        return summary
    except Exception as exc:  # never let a formatter kill the session
        return f"<ocaml decode error: {exc}>"


def __lldb_init_module(debugger, _internal_dict):
    # Note: probe (Q2, 2026-08-22) found that stock OCaml 5.4.0 lldb-dap
    # reports NO variables at all (Locals/Globals empty) for OCaml frames,
    # so these registrations are dormant until DWARF improves.
    debugger.HandleCommand(
        "type summary add -F {}.ocaml_value_summary value".format(__name__)
    )
    debugger.HandleCommand(
        'type summary add -F {}.ocaml_value_summary "unsigned long"'
        " -x '^caml.*'".format(__name__)
    )

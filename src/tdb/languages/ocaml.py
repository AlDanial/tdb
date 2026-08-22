"""The OCaml language profile (built up across Tasks 2-7).

This task: executable-flavor sniffing used by registry.detect(), plus
frame-name demangling for Presentation.frame_name.
"""

from __future__ import annotations

import re
from pathlib import Path

_BYTECODE_TRAILER_MARK = b"Caml1999"  # e.g. b"Caml1999X033" at file end
_NATIVE_MAGIC = (
    b"\x7fELF",
    b"\xcf\xfa\xed\xfe",
    b"\xce\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
)
_CAML_MARKERS = (b"caml_program", b"caml_startup")
_SCAN_CHUNK = 2 * 1024 * 1024  # spec risk 5: bounded scan, head + tail


def ocaml_flavor(program: str) -> str | None:
    """ "native"/"bytecode" when `program` is an OCaml executable, else None.

    Best-effort byte sniffing: a stripped native binary may return None
    (lands in cpp; --lang ocaml overrides — documented in README).
    """
    path = Path(program)
    try:
        with open(path, "rb") as f:
            head = f.read(64)
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 16))
            tail = f.read()
    except OSError:
        return None
    if head.startswith(b"#!") and b"ocamlrun" in head.splitlines()[0]:
        return "bytecode"
    if _BYTECODE_TRAILER_MARK in tail:
        return "bytecode"
    if any(head.startswith(m) for m in _NATIVE_MAGIC):
        if _scan_for_caml_marker(path, size):
            return "native"
    return None


_MANGLED_SUFFIX_RE = re.compile(r"_\d+$")
# "caml" followed by an uppercase letter marks a mangled OCaml symbol
# (camlMain__worker_271, camlOcaml_domains.worker_297). Runtime C symbols
# (caml_apply2, caml_start_program) have a lowercase/underscore letter
# right after "caml" and must be left untouched.
_CAML_MANGLED_PREFIX_RE = re.compile(r"^caml[A-Z]")


def demangle_frame_name(name: str) -> str:
    """ "camlFoo__Bar__run_17" -> "Foo.Bar.run"; anything else unchanged.

    lldb's OCaml 5.x symbol display separates module-path segments with
    "__" but a module and its function with "." (e.g.
    "camlOcaml_domains.worker_297" -> "Ocaml_domains.worker",
    "camlStdlib__Domain.body_757" -> "Stdlib.Domain.body"), so both
    separators are normalized to ".". Runtime C symbols (caml_apply2,
    caml_start_program) don't match the mangled-prefix shape and pass
    through unchanged.
    """
    if not _CAML_MANGLED_PREFIX_RE.match(name) or (
        "__" not in name and "." not in name
    ):
        return name
    body = _MANGLED_SUFFIX_RE.sub("", name[len("caml") :])
    return body.replace("__", ".")


def _scan_for_caml_marker(path: Path, size: int) -> bool:
    try:
        with open(path, "rb") as f:
            if any(m in f.read(_SCAN_CHUNK) for m in _CAML_MARKERS):
                return True
            if size > _SCAN_CHUNK:
                # overlap by 16 bytes so a marker straddling the boundary
                # of head and tail chunks is still seen for mid-size files
                f.seek(max(_SCAN_CHUNK - 16, size - _SCAN_CHUNK))
                data = f.read()
                return any(m in data for m in _CAML_MARKERS)
    except OSError:
        pass
    return False

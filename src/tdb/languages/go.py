"""The Go language profile (Delve DAP).

This module is built up across Tasks 2-4: detection helpers here,
DelveAdapter + build_go_profile in Task 3, error parsing and thread
classification in Task 4.
"""

from __future__ import annotations

# Magic prefix of the build-info blob Go links into every binary
# (what `go version <binary>` locates). 16 bytes, version-stable.
_BUILDINFO_MAGIC = b"\xff Go buildinf:"
_CHUNK = 1024 * 1024  # scan in 1MB chunks
_SCAN_LIMIT = 16 * _CHUNK  # bounded: huge non-Go binaries stay cheap
_OVERLAP = len(_BUILDINFO_MAGIC) - 1


def is_go_binary(program: str) -> bool:
    """True when `program` is an executable with embedded Go buildinfo.

    Bounded scan of the first 16MB (the blob sits in an early data
    section in practice). Best-effort: unreadable files and misses
    beyond the limit return False — `--lang go` overrides (README).
    """
    scanned = 0
    tail = b""
    try:
        with open(program, "rb") as f:
            while scanned < _SCAN_LIMIT:
                chunk = f.read(_CHUNK)
                if not chunk:
                    break
                if _BUILDINFO_MAGIC in tail + chunk[:_OVERLAP] or (
                    _BUILDINFO_MAGIC in chunk
                ):
                    return True
                tail = chunk[-_OVERLAP:]
                scanned += len(chunk)
    except OSError:
        pass
    return False

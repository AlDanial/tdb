"""``tdb --trace-merge``: weave several services' captures into one trace.

Each input is a ``BASE.jsonl`` capture or a leftover ``.parts`` directory
(see :func:`tdb.trace.capture.read_input`); a directory's warnings (what
the trace may be missing) are printed as ``--trace`` would, naming it.  The
result is one Perfetto trace in which every service gets its own group of
process tracks, on the shared wall-clock time axis.

This module runs inside tdb, not the traced program; it must work on
Python 3.11.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from tdb.trace.capture import Capture, read_input
from tdb.trace.launch import say, warning_lines, write_atomic
from tdb.trace.perfetto import write_trace

_WARNING = "tdb: warning: "


def _warn_non_overlapping(captures: list[Capture], inputs: list[str]) -> None:
    """Warn about each capture whose time range meets no other's: it is
    probably from another run, and would sit far off on the time axis."""
    spans = [(c.header.get("start_ns"), c.header.get("end_ns")) for c in captures]
    if len(spans) < 2:
        return
    for i, (cap, (start, end)) in enumerate(zip(captures, spans)):
        if start is None or end is None:
            continue
        if not any(
            j != i and o_start is not None and o_start <= end and start <= o_end
            for j, (o_start, o_end) in enumerate(spans)
        ):
            name = cap.header.get("name") or Path(inputs[i]).stem
            say(f"tdb: warning: {name} does not overlap any other capture in time")


def _about(line: str, name: str) -> str:
    """A warning line about one input: ``tdb: warning: NAME: ...``."""
    if line.startswith(_WARNING):
        return f"{_WARNING}{name}: {line[len(_WARNING) :]}"
    return f"tdb: {name}: {line.removeprefix('tdb: ')}"


def _why(exc: Exception) -> str:
    if isinstance(exc, KeyError):  # a hand-edited record lost a field
        return f"a record has no {exc.args[0]!r} field"
    return str(exc) or type(exc).__name__


def _unreadable(captures: list[Capture], inputs: list[str]) -> tuple[str, str]:
    """After building the trace failed: the first input that cannot be
    drawn on its own, and why; else ("", "")."""
    for path, capture in zip(inputs, captures):
        try:
            write_trace([capture], Path(os.devnull))
        except Exception as exc:  # noqa: BLE001 - whatever the input breaks
            return path, _why(exc)
    return "", ""


def merge_main(
    inputs: list[str], out_base: str | None, *, now: datetime | None = None
) -> int:
    """Merge ``inputs`` into ``{out_base}.perfetto.json``.  Returns 0, or 2
    when an input cannot be read (or drawn) or the output cannot be
    written."""
    captures = []
    for path in inputs:
        try:
            capture, summary = read_input(Path(path))
        except Exception as exc:  # noqa: BLE001 - any read failure (corrupt JSON, ...)
            say(f"tdb: error: cannot read {path}: {exc}")
            return 2
        captures.append(capture)
        if summary is not None:  # a parts directory: as --trace would warn
            for line in warning_lines(summary):
                say(_about(line, path))
    if out_base is None:
        out_base = (now or datetime.now()).strftime("merged-%Y%m%d-%H%M%S")
    out = Path(f"{out_base}.perfetto.json")
    try:
        _warn_non_overlapping(captures, inputs)
        write_atomic(out, lambda tmp: write_trace(captures, tmp))
    except OSError as exc:
        say(f"tdb: error: cannot write {out}: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 - an input that cannot be drawn (M11)
        path, why = _unreadable(captures, inputs)
        if path:
            say(f"tdb: error: cannot read {path}: {why}")
        else:
            say(f"tdb: error: cannot merge the inputs: {_why(exc)}")
        return 2
    say(f"tdb: wrote {out}  (open in https://ui.perfetto.dev)")
    return 0

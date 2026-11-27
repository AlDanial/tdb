"""``tdb --trace-merge``: weave several services' captures into one trace.

Each input is a ``BASE.jsonl`` capture or a leftover ``.parts`` directory
(see :func:`tdb.trace.capture.load_input`).  The result is one Perfetto
trace in which every service gets its own group of process tracks, on the
shared wall-clock time axis.

This module runs inside tdb, not the traced program; it must work on
Python 3.11.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from tdb.trace.capture import Capture, load_input
from tdb.trace.launch import _write_atomic, say
from tdb.trace.perfetto import write_trace


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


def merge_main(
    inputs: list[str], out_base: str | None, *, now: datetime | None = None
) -> int:
    """Merge ``inputs`` into ``{out_base}.perfetto.json``.  Returns 0, or 2
    when an input cannot be read or the output cannot be written."""
    captures = []
    for path in inputs:
        try:
            captures.append(load_input(Path(path)))
        except Exception as exc:  # noqa: BLE001 - any read failure (corrupt JSON, ...)
            say(f"tdb: error: cannot read {path}: {exc}")
            return 2
    _warn_non_overlapping(captures, inputs)
    if out_base is None:
        out_base = (now or datetime.now()).strftime("merged-%Y%m%d-%H%M%S")
    out = Path(f"{out_base}.perfetto.json")
    try:
        _write_atomic(out, lambda tmp: write_trace(captures, tmp))
    except OSError as exc:
        say(f"tdb: error: cannot write {out}: {exc}")
        return 2
    say(f"tdb: wrote {out}  (open in https://ui.perfetto.dev)")
    return 0

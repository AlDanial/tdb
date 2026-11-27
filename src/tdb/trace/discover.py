"""``tdb --trace-discover``: turn the agent's call counts into a spec.

In discover mode the agent counts the calls of every function in the
program's own code and writes cumulative ``count`` records to its part
files.  :func:`write_discovered_spec` reads them and writes a spec YAML,
one entry per function with its count as a comment, ready to edit and
pass to ``tdb --trace``.  PyYAML cannot write comments, so the YAML is
written by hand, quoting whatever YAML would read back as something else.

This module runs inside tdb, not the traced program; it must work on
Python 3.11.
"""

from __future__ import annotations

import glob
import json
import os
import re
from datetime import datetime
from pathlib import Path

import yaml

from tdb.trace.capture import PART_FILE
from tdb.trace.contract import DISCOVER_CAP
from tdb.trace.launch import write_atomic
from tdb.trace.spec import norm_path

_PLAIN = re.compile(r"[A-Za-z0-9_./-]+")  # file keys written unquoted
_MAX_UP = 3  # a relative file key climbs at most this many directories
_ASYNC_KINDS = ("coroutine", "async_generator")

# Function -> (kind, calls, capped).  A function is (file, qualname, line).
_Counts = dict[tuple[str, str, int], tuple[str, int, bool]]


def _read_counts(parts_dir: Path) -> _Counts:
    """Each function's calls: the largest count of each part (they are
    cumulative), summed over the parts (one per process); capped if any
    part capped it.  Unreadable lines (a part cut off by a kill) are
    skipped."""
    totals: dict[tuple[str, str, int], list] = {}
    for path in sorted(parts_dir.iterdir()):
        if not PART_FILE.match(path.name):
            continue
        mine: dict[tuple[str, str, int], list] = {}
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                rec = _count_record(line)
                if rec is None:
                    continue
                key = (rec["file"], rec["qual"], rec["line"])
                seen = mine.get(key)
                if seen is None:
                    mine[key] = [rec["kind"], rec["n"], rec["capped"]]
                else:
                    seen[1] = max(seen[1], rec["n"])
                    seen[2] = seen[2] or rec["capped"]
        for key, (kind, n, capped) in mine.items():
            total = totals.setdefault(key, [kind, 0, False])
            total[1] += n
            total[2] = total[2] or capped
    return {key: (kind, n, capped) for key, (kind, n, capped) in totals.items()}


def _count_record(line: str) -> dict | None:
    """A well-formed ``count`` record, or None."""
    if '"count"' not in line:
        return None
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    if not isinstance(rec, dict) or rec.get("k") != "count":
        return None
    for field, kind in (
        ("file", str),
        ("qual", str),
        ("kind", str),
        ("line", int),
        ("n", int),
        ("capped", bool),
    ):
        value = rec.get(field)
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            return None
    return rec


def format_duration(seconds: float) -> str:
    """``45s`` under a minute, else ``3m12s``."""
    total = round(seconds)
    if total < 60:
        return f"{total}s"
    return f"{total // 60}m{total % 60:02d}s"


def _printable(text: str) -> str:
    """``text`` with what a YAML comment line cannot hold (line breaks,
    control characters, lone surrogates) as backslash escapes."""
    return "".join(
        c if c.isprintable() else c.encode("unicode_escape").decode("ascii")
        for c in text
    )


def _quoted(text: str) -> str:
    """``text`` as a YAML double-quoted scalar: json.dumps's escapes, which
    YAML shares, except that a character beyond U+FFFF is one ``\\U``
    escape (PyYAML reads json's surrogate pair as two lone surrogates)."""
    return (
        '"'
        + "".join(
            json.dumps(c)[1:-1] if ord(c) <= 0xFFFF else f"\\U{ord(c):08x}"
            for c in text
        )
        + '"'
    )


def _reads_back(text: str) -> bool:
    """True if YAML reads ``text``, unquoted, as that string: not as a
    boolean, null or number (``on``, ``null``, ``1.5``)."""
    try:
        return yaml.safe_load(text) == text
    except yaml.YAMLError:
        return False


def _file_key(file: str, spec_dir: str) -> str:
    """The spec key of ``file``: relative to the spec's directory, with
    ``/`` separators, unless that climbs more than _MAX_UP directories
    (or cannot be: another drive), then absolute.  load_spec takes a key
    without a ``/`` that does not end in ``.py`` for a module name, so such
    a key starts with ``./``; and it expands ``*?[`` as a glob, so those
    are escaped."""
    try:
        rel = os.path.relpath(file, spec_dir)
    except ValueError:  # Windows: another drive
        rel = None
    if rel is not None:
        parts = rel.split(os.sep)
        ups = next((i for i, p in enumerate(parts) if p != os.pardir), len(parts))
        if ups > _MAX_UP:
            rel = None
    key = (file if rel is None else rel).replace(os.sep, "/")
    if "/" not in key and not key.endswith(".py"):
        key = "./" + key
    return glob.escape(key)


def _scalar(text: str, *, plain: re.Pattern | None = None) -> str:
    """``text`` unquoted where YAML reads it back as is (and it matches
    ``plain``, if given), else double-quoted."""
    if (plain is None or plain.fullmatch(text)) and _reads_back(text):
        return text
    return _quoted(text)


def _count_text(n: int, capped: bool, cap: int) -> str:
    if capped:
        return f"≥{cap:,} calls"
    return "1 call" if n == 1 else f"{n:,} calls"


def _render(
    counts: _Counts,
    spec_dir: str,
    *,
    program_desc: str,
    started: datetime,
    duration_s: float,
    cap: int = DISCOVER_CAP,
) -> tuple[str, int]:
    """The spec text for ``counts`` and the number of functions in it.
    Files are sorted by key, functions within a file by line."""
    by_file: dict[str, list[tuple[int, str, str, int, bool]]] = {}
    for (file, qual, line), (kind, n, capped) in counts.items():
        if n < 1:
            continue
        key = _file_key(file, spec_dir)
        by_file.setdefault(key, []).append(
            (line, qual.replace(".", "::"), kind, n, capped or n >= cap)
        )
    header = (
        f"# tdb --trace-discover: {_printable(program_desc)}, "
        f"{started:%Y-%m-%d %H:%M}, {format_duration(duration_s)}\n"
        "# Remove what you do not need; hot functions make traces large.\n"
    )
    blocks = []  # one per file, separated by a blank line
    total = 0
    for key in sorted(by_file):
        functions = sorted(by_file[key])
        names = [_scalar(name) for _, name, _, _, _ in functions]
        width = max(len(name) for name in names)
        lines = [f"{_scalar(key, plain=_PLAIN)}:"]
        for name, (_, _, kind, n, capped) in zip(names, functions):
            lines.append(
                f"  - {name.ljust(width)}  # {_count_text(n, capped, cap)}"
                f"{', async' if kind in _ASYNC_KINDS else ''}"
                f"{'  (hot)' if capped else ''}"
            )
        blocks.append("".join(line + "\n" for line in lines))
        total += len(functions)
    return header + "\n" + "\n".join(blocks), total


def write_discovered_spec(
    parts_dir: Path,
    out_path: Path,
    *,
    program_desc: str,
    started: datetime,
    duration_s: float,
    cap: int = DISCOVER_CAP,
) -> int:
    """Write the spec of the functions counted in ``parts_dir`` to
    ``out_path``, whole or not at all; return the number of functions.
    Raises OSError if the parts cannot be read or the spec written."""
    text, total = _render(
        _read_counts(parts_dir),
        norm_path(Path(out_path).parent),
        program_desc=program_desc,
        started=started,
        duration_s=duration_s,
        cap=cap,
    )
    write_atomic(Path(out_path), lambda tmp: tmp.write_text(text, encoding="utf-8"))
    return total

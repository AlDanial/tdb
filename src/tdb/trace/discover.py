"""``tdb --trace-discover``: turn the agent's call counts into a spec.

In discover mode the agent counts the calls of every function in the
program's own code and writes cumulative ``count`` records to its part
files.  :func:`read_counts` sums them, and :func:`write_spec` writes a
spec YAML, one entry per function name with its count as a comment, ready
to edit and pass to ``tdb --trace``.  It keeps only what ``load_spec``
accepts: functions their source file defines (Ruling 27), every function
of a ``.py`` file tdb's own Python cannot parse (Ruling 28: load_spec does
not check its names), and keys and names YAML reads back as they are.  A
spec with no function in it would not load, so none is written then.
PyYAML cannot write comments, so the YAML is written by hand.

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
from typing import NamedTuple

import yaml

from tdb.trace.capture import PART_FILE
from tdb.trace.contract import DISCOVER_CAP
from tdb.trace.launch import write_atomic
from tdb.trace.spec import defined_functions, norm_path

_PLAIN = re.compile(r"[A-Za-z0-9_./-]+")  # file keys written unquoted
_MAX_UP = 3  # a relative file key climbs at most this many directories
_MAX_SIMPLE_KEY = 1024  # characters: PyYAML reads no longer "key:" line
_ASYNC_KINDS = ("coroutine", "async_generator")
_UNCHECKED = "unchecked"  # _in_source: a .py file tdb cannot parse

# Function -> (kind, calls, capped).  A function is (file, qualname, line).
Counts = dict[tuple[str, str, int], tuple[str, int, bool]]


class Discovered(NamedTuple):
    """What a spec was written with: its number of functions (none: no
    spec was written), and of those left out because their source file
    does not define them, or because it is missing or not Python."""

    functions: int
    not_found: int
    no_source: int

    @property
    def omitted(self) -> int:
        return self.not_found + self.no_source


def read_counts(parts_dir: Path) -> Counts:
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
    """``text`` as a YAML double-quoted scalar.  Printable characters but
    ``"`` and ``\\`` are written as they are (UTF-8), which keeps a
    non-ASCII path short; the rest as json.dumps escapes them (YAML shares
    its escapes), beyond U+FFFF as one ``\\U`` escape (PyYAML reads
    json's surrogate pair as two lone surrogates)."""
    out = []
    for c in text:
        if c.isprintable() and c not in '"\\':
            out.append(c)
        elif ord(c) <= 0xFFFF:
            out.append(json.dumps(c)[1:-1])
        else:
            out.append(f"\\U{ord(c):08x}")
    return '"' + "".join(out) + '"'


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


def _key_lines(key: str) -> list[str]:
    """A file key's line, or (longer than PyYAML reads in one) the
    explicit ``? key`` form's two."""
    text = _scalar(key, plain=_PLAIN)
    if len(text) > _MAX_SIMPLE_KEY:
        return [f"? {text}", ":"]
    return [f"{text}:"]


def _in_source(counts: Counts) -> tuple[dict, int, int]:
    """The functions of ``counts`` that load_spec accepts, merged by name:
    ``{(file, qual): [first line, async, calls, capped]}``.  Those are the
    ones their source file defines, and all of a ``.py`` file that tdb's
    Python cannot parse (it may need the program's newer one).  Also how
    many (file, qual) others there were: names the file does not define
    (3.12's ``type X = ...`` values), and names whose file is missing or
    not Python (a zipapp's, code compiled from a template under its own
    path)."""
    defined: dict[str, set[str] | str | None] = {}
    kept: dict[tuple[str, str], list] = {}
    not_found: set[tuple[str, str]] = set()
    no_source: set[tuple[str, str]] = set()
    for (file, qual, line), (kind, n, capped) in counts.items():
        if n < 1:
            continue
        if file not in defined:
            try:
                defined[file] = {q for q, _ in defined_functions(file)}
            except (SyntaxError, ValueError, RecursionError):
                py = os.path.normcase(file).endswith(".py")
                defined[file] = _UNCHECKED if py else None
            except OSError:
                defined[file] = None
        names = defined[file]
        if names is None:
            no_source.add((file, qual))
            continue
        if names is not _UNCHECKED and qual not in names:
            not_found.add((file, qual))
            continue
        # One name, several defs (a property's getter and setter, a def in
        # each branch of an if): one entry for load_spec, so one here.
        is_async = kind in _ASYNC_KINDS
        fn = kept.get((file, qual))
        if fn is None:
            kept[(file, qual)] = [line, is_async, n, capped]
        else:
            fn[0], fn[1] = min(fn[0], line), fn[1] or is_async
            fn[2], fn[3] = fn[2] + n, fn[3] or capped
    return kept, len(not_found), len(no_source)


def _count_text(n: int, capped: bool, cap: int) -> str:
    if capped:
        return f"≥{cap:,} calls"
    return "1 call" if n == 1 else f"{n:,} calls"


def _render(
    functions: dict,
    spec_dir: str,
    *,
    program_desc: str,
    started: datetime,
    duration_s: float,
    cap: int,
) -> str:
    """The spec text for ``functions`` (from _in_source).  Files are sorted
    by key, functions within a file by line."""
    by_file: dict[str, list[tuple[int, str, bool, int, bool]]] = {}
    for (file, qual), (line, is_async, n, capped) in functions.items():
        by_file.setdefault(_file_key(file, spec_dir), []).append(
            # "hot" only when a process stopped counting it: a sum over
            # processes is exact (M10)
            (line, qual.replace(".", "::"), is_async, n, capped)
        )
    header = (
        f"# tdb --trace-discover: {_printable(program_desc)}, "
        f"{started:%Y-%m-%d %H:%M}, {format_duration(duration_s)}\n"
        "# Remove what you do not need; hot functions make traces large.\n"
    )
    blocks = []  # one per file, separated by a blank line
    for key in sorted(by_file):
        entries = sorted(by_file[key])
        names = [_scalar(name) for _, name, _, _, _ in entries]
        width = max(len(name) for name in names)
        lines = _key_lines(key)
        for name, (_, _, is_async, n, capped) in zip(names, entries):
            lines.append(
                f"  - {name.ljust(width)}  # {_count_text(n, capped, cap)}"
                f"{', async' if is_async else ''}"
                f"{'  (hot)' if capped else ''}"
            )
        blocks.append("".join(line + "\n" for line in lines))
    return header + "\n" + "\n".join(blocks)


def write_spec(
    counts: Counts,
    out_path: Path,
    *,
    program_desc: str,
    started: datetime,
    duration_s: float,
    cap: int = DISCOVER_CAP,
) -> Discovered:
    """Write the spec of ``counts`` to ``out_path``, whole or not at all,
    and not at all when no function is left (such a spec would not load).
    Raises OSError if it cannot be written."""
    functions, not_found, no_source = _in_source(counts)
    if not functions:
        return Discovered(0, not_found, no_source)
    text = _render(
        functions,
        norm_path(Path(out_path).parent),
        program_desc=program_desc,
        started=started,
        duration_s=duration_s,
        cap=cap,
    )
    write_atomic(Path(out_path), lambda tmp: tmp.write_text(text, encoding="utf-8"))
    return Discovered(len(functions), not_found, no_source)


def write_discovered_spec(
    parts_dir: Path,
    out_path: Path,
    *,
    program_desc: str,
    started: datetime,
    duration_s: float,
    cap: int = DISCOVER_CAP,
) -> Discovered:
    """read_counts, then write_spec.  Raises OSError if the parts cannot
    be read or the spec written."""
    return write_spec(
        read_counts(parts_dir),
        out_path,
        program_desc=program_desc,
        started=started,
        duration_s=duration_s,
        cap=cap,
    )

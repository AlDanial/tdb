"""Load and validate the YAML spec file for ``tdb --trace``.

A spec maps file names (paths, globs, or dotted module names) to lists of
functions to trace.  ``load_spec`` reports every problem it finds in one
``SpecError`` so users can fix a spec in a single pass.
"""

from __future__ import annotations

import ast
import difflib
import fnmatch
import glob
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

_MODULE_RE = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$")
_OPTIONS = ("capture", "return", "if", "max_hits")


class SpecError(Exception):
    """A spec failed validation; ``problems`` lists every issue found."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("\n".join(self.problems))


@dataclass(frozen=True)
class FunctionEntry:
    raw: str
    pattern: str
    capture: tuple[str, ...] = ()
    record_return: bool = False
    condition: str | None = None
    max_hits: int | None = None


@dataclass(frozen=True)
class FileEntry:
    key: str
    kind: Literal["path", "module"]
    files: tuple[str, ...]
    suffixes: tuple[str, ...]
    functions: tuple[FunctionEntry, ...]


@dataclass(frozen=True)
class TraceSpec:
    path: Path
    entries: tuple[FileEntry, ...]

    def agent_targets(self) -> list[dict]:
        """Return the TARGET list for the agent config JSON."""
        return [
            {
                "files": list(e.files),
                "suffixes": list(e.suffixes),
                "functions": [
                    {
                        "pattern": f.pattern,
                        "capture": list(f.capture),
                        "return": f.record_return,
                        "if": f.condition,
                        "max_hits": f.max_hits,
                    }
                    for f in e.functions
                ],
            }
            for e in self.entries
        ]


def normalize_name(name: str) -> str:
    """``Cart::add`` -> ``Cart.add``; drop ``.<locals>`` segments."""
    return name.replace("::", ".").replace(".<locals>", "")


def norm_path(p: str | os.PathLike) -> str:
    """Canonical form used to compare file paths everywhere in tracing."""
    return os.path.normcase(os.path.realpath(os.fspath(p)))


def defined_functions(path: str) -> list[tuple[str, int]]:
    """Return ``(qualname, lineno)`` for every def in a source file.

    Classes contribute to the qualname but are not themselves listed;
    lambdas are ignored.  Raises ``SyntaxError``/``OSError`` on bad input.
    """
    with open(path, "rb") as fh:
        tree = ast.parse(fh.read(), filename=path)
    found: list[tuple[str, int]] = []

    def walk(node: ast.AST, stack: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                stack.append(child.name)
                found.append((".".join(stack), child.lineno))
                walk(child, stack)
                stack.pop()
            elif isinstance(child, ast.ClassDef):
                stack.append(child.name)
                walk(child, stack)
                stack.pop()
            else:
                walk(child, stack)

    walk(tree, [])
    return found


def _is_glob(key: str) -> bool:
    return any(c in key for c in "*?[")


def _parse_entry(
    key: str, n: int, item: Any, problems: list[str]
) -> FunctionEntry | None:
    """Parse one list item; return None (after reporting) if it has problems."""
    opts: dict = {}
    if isinstance(item, str) and item.strip():
        raw = item.strip()
    elif isinstance(item, dict) and len(item) == 1:
        ((raw, opts),) = item.items()
        if not isinstance(raw, str) or not raw.strip():
            problems.append(f"{key}: entry {n} must be a name or a one-key mapping")
            return None
        raw = raw.strip()
        if opts is None:
            opts = {}
        if not isinstance(opts, dict):
            problems.append(f"{key}: entry {n} must be a name or a one-key mapping")
            return None
    else:
        problems.append(f"{key}: entry {n} must be a name or a one-key mapping")
        return None

    bad = False

    def problem(msg: str) -> None:
        nonlocal bad
        bad = True
        problems.append(f"{key}: {raw!r}: {msg}")

    def check_expr(expr: str) -> bool:
        try:
            compile(expr, "<trace-spec>", "eval")
        except SyntaxError as exc:
            problem(f"cannot compile {expr!r}: {exc.msg}")
            return False
        return True

    capture: tuple[str, ...] = ()
    record_return = False
    condition: str | None = None
    max_hits: int | None = None
    for opt, val in opts.items():
        if opt not in _OPTIONS:
            problem(f"unknown option {opt!r}")
        elif opt == "capture":
            if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
                problem("capture must be a list of expressions")
            else:
                if all([check_expr(x) for x in val]):
                    capture = tuple(val)
        elif opt == "return":
            if not isinstance(val, bool):
                problem("return must be true or false")
            else:
                record_return = val
        elif opt == "if":
            if not isinstance(val, str):
                problem(f"cannot compile {val!r}: expression must be a string")
            elif check_expr(val):
                condition = val
        elif opt == "max_hits":
            if isinstance(val, bool) or not isinstance(val, int) or val < 1:
                problem("max_hits must be a positive integer")
            else:
                max_hits = val
    if bad:
        return None
    return FunctionEntry(
        raw, normalize_name(raw), capture, record_return, condition, max_hits
    )


def load_spec(path: str | os.PathLike) -> TraceSpec:
    """Read and validate a spec file, raising ``SpecError`` on any problem."""
    spec_path = Path(os.path.abspath(os.fspath(path)))
    try:
        data = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SpecError([f"invalid YAML: {exc}"]) from None
    if data is None or data == {}:
        raise SpecError(["spec is empty"])
    if not isinstance(data, dict):
        raise SpecError(["spec must map file names to lists of functions"])

    problems: list[str] = []
    entries: list[FileEntry] = []
    # file -> list of (qualname, lineno), or None if it failed to parse
    defs_cache: dict[str, list[tuple[str, int]] | None] = {}

    def names_in(f: str) -> list[str] | None:
        if f not in defs_cache:
            try:
                defs_cache[f] = defined_functions(f)
            except (SyntaxError, ValueError) as exc:
                msg = getattr(exc, "msg", None) or str(exc)
                problems.append(f"{f}: cannot parse: {msg}")
                defs_cache[f] = None
            except OSError as exc:
                problems.append(f"{f}: cannot parse: {exc.strerror or exc}")
                defs_cache[f] = None
        d = defs_cache[f]
        return None if d is None else [q for q, _ in d]

    for raw_key, value in data.items():
        key = str(raw_key)
        # (1) key kind
        if not isinstance(raw_key, str):
            problems.append(f"{key}: not a path or module name")
            continue
        if "/" in key or "\\" in key or key.endswith(".py"):
            kind: Literal["path", "module"] = "path"
        elif _MODULE_RE.match(key):
            kind = "module"
        else:
            problems.append(f"{key}: not a path or module name")
            continue
        # (2) non-empty list
        if not isinstance(value, list) or not value:
            problems.append(f"{key}: no functions listed")
            continue
        # (3) file resolution
        files: tuple[str, ...] = ()
        suffixes: tuple[str, ...] = ()
        resolved = True
        if kind == "path":
            pattern = os.path.join(str(spec_path.parent), key)
            if _is_glob(key):
                hits = sorted(
                    h for h in glob.glob(pattern, recursive=True) if os.path.isfile(h)
                )
            else:
                hits = [pattern] if os.path.isfile(pattern) else []
            if not hits:
                problems.append(
                    f"{key}: matches no files"
                    if _is_glob(key)
                    else f"{key}: no such file"
                )
                resolved = False
            files = tuple(dict.fromkeys(norm_path(h) for h in hits))
        else:
            base = key.replace(".", "/")
            suffixes = (f"{base}.py", f"{base}/__init__.py")
        # (4) entries
        names: list[str] | None = None
        if resolved and files:
            names = []
            for f in files:
                got = names_in(f)
                if got is None:
                    names = None
                    break
                names.extend(got)
        funcs: list[FunctionEntry] = []
        for n, item in enumerate(value, 1):
            fe = _parse_entry(key, n, item, problems)
            if fe is None:
                continue
            funcs.append(fe)
            if names is not None and not any(
                fnmatch.fnmatchcase(q, fe.pattern) for q in names
            ):
                where = key if len(files) == 1 else f"any of {len(files)} files"
                msg = f"{key}: {fe.raw!r} matches no function in {where}"
                hints = difflib.get_close_matches(
                    fe.pattern, sorted(set(names)), n=3, cutoff=0.6
                )
                if hints:
                    msg += f" (did you mean: {', '.join(hints)}?)"
                problems.append(msg)
        if resolved:
            entries.append(FileEntry(key, kind, files, suffixes, tuple(funcs)))

    if problems:
        raise SpecError(problems)
    return TraceSpec(spec_path, tuple(entries))


def unmatched_entries(spec: TraceSpec, called: set[tuple[str, str]]) -> list[str]:
    """List ``"key: raw"`` for spec entries never seen in ``called``.

    ``called`` holds ``(norm_path file, normalized qualname)`` pairs.
    """
    out: list[str] = []
    for e in spec.entries:
        if e.kind == "path":
            files = set(e.files)
            quals = [q for f, q in called if f in files]
        else:
            sufs = tuple(
                "/" + os.path.normcase(s).replace("\\", "/") for s in e.suffixes
            )
            quals = [q for f, q in called if f.replace("\\", "/").endswith(sufs)]
        for fe in e.functions:
            if not any(fnmatch.fnmatchcase(q, fe.pattern) for q in quals):
                out.append(f"{e.key}: {fe.raw}")
    return out

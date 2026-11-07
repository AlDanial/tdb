"""Collect local source files without importing or executing debuggee code."""

from __future__ import annotations

import ast
import tokenize
from collections.abc import Iterable
from pathlib import Path

# Only used to distinguish source entry points from native executables.
_SOURCE_SUFFIXES = {
    ".py",
    ".pyw",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".cxx",
    ".hpp",
    ".hxx",
    ".rs",
    ".go",
    ".ml",
    ".mli",
    ".pl",
    ".pm",
    ".rb",
    ".sh",
    ".bash",
    ".tcsh",
    ".csh",
    ".ps1",
    ".psm1",
    ".f",
    ".f90",
    ".f95",
}


def read_source_list(filename: str) -> tuple[list[Path], list[str]]:
    """Read literal UTF-8 paths; report bad entries, propagate list read errors."""
    listing = Path(filename).resolve()
    paths: set[Path] = set()
    warnings: list[str] = []
    for line in listing.read_text(encoding="utf-8").splitlines():
        name = line.strip()
        if not name:
            continue
        path = listing.parent / name
        try:
            path = path.resolve()
            # Opening (rather than os.access) checks actual readability.
            with path.open("rb"):
                pass
        except (OSError, ValueError) as exc:
            warnings.append(f"Cannot read source file {path}: {exc}")
        else:
            paths.add(path)
    return sorted(paths), warnings


def _resolve_module(roots: list[Path], parts: list[str]) -> list[Path]:
    """Resolve each component across roots, respecting regular-package boundaries."""
    found: list[Path] = []
    for index, part in enumerate(parts):
        namespaces: list[Path] = []
        for root in roots:
            directory = root / part
            initializer = directory / "__init__.py"
            module = root / f"{part}.py"
            if initializer.is_file():
                found.append(initializer)
                roots = [directory]
                break
            if module.is_file():
                return found + ([module] if index == len(parts) - 1 else [])
            if directory.is_dir():
                namespaces.append(directory)
        else:
            if not namespaces:
                return found
            roots = namespaces
    return found


def _is_source_entry(path: Path) -> bool:
    if path.suffix.lower() in _SOURCE_SUFFIXES:
        return True
    try:
        with path.open("rb") as stream:
            return stream.read(2) == b"#!"
    except OSError:
        return False


def discover_sources(
    program: str,
    cwd: str | None = None,
    additional: Iterable[Path] = (),
    *,
    python: bool = True,
) -> list[Path]:
    """Follow static Python imports within local roots, returning unique paths.

    Files which cannot be parsed remain selectable. No environment import
    hooks, sys.path, PYTHONPATH, or installed modules are consulted.
    """
    working = Path(cwd or Path.cwd()).resolve()
    extras = [Path(path).resolve() for path in additional]
    entry = Path(program).resolve() if program else None
    roots = list(
        dict.fromkeys(
            ([entry.parent] if entry else []) + [working] + [p.parent for p in extras]
        )
    )
    pending = list(extras)
    if entry and (python or _is_source_entry(entry)):
        pending.append(entry)
    visited: set[Path] = set()
    sources: set[Path] = set()
    while pending:
        path = pending.pop().resolve()
        if path in visited:
            continue
        visited.add(path)
        if not path.is_file():
            continue
        try:
            with path.open("rb"):
                pass
        except OSError:
            continue
        sources.add(path)
        if not python or (path != entry and path.suffix.lower() not in {".py", ".pyw"}):
            continue
        try:
            with tokenize.open(path) as stream:
                tree = ast.parse(stream.read(), filename=str(path))
        except (OSError, SyntaxError, UnicodeError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
                search_roots = roots
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                names = ([base] if base else []) + [
                    f"{base}.{alias.name}" if base else alias.name
                    for alias in node.names
                    if alias.name != "*"
                ]
                if node.level:
                    parent = path.parent
                    for _ in range(node.level - 1):
                        parent = parent.parent
                    search_roots = [parent]
                else:
                    search_roots = roots
            else:
                continue
            for name in names:
                pending.extend(_resolve_module(search_roots, name.split(".")))
    return sorted(sources)

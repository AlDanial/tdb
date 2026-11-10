"""Resolve static import requests in a Python environment and save source lists."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from tdb.source_discovery import ImportRequest, _resolve_module


@dataclass
class SourceSelection:
    """Session curation shared by successive Source modal instances."""

    discovered: set[Path] = field(default_factory=set)
    excluded: set[Path] = field(default_factory=set)
    filename: Path | None = None
    saving: bool = False


# Snapshot the configured path before restricting helper imports to the actual
# stdlib. -P omits the unsafe cwd entry but keeps PYTHONPATH and site setup.
# No target modules or packages are imported by the helper.
_METADATA_SCRIPT = """
import sys
search_paths = sys.path[:]
stdlib = getattr(sys, '_stdlib_dir', None)
if not stdlib:
    if sys.platform == 'win32':
        stdlib = sys.base_prefix + '/Lib'
    else:
        stdlib = sys.base_prefix + '/lib/python%d.%d' % sys.version_info[:2]
sys.path = [stdlib]
import json
import sysconfig
import site
print('TDB_SOURCE_PATHS:' + json.dumps({
    'paths': search_paths,
    'stdlib': [stdlib, sysconfig.get_path('stdlib'), sysconfig.get_path('platstdlib')],
    'site': [sysconfig.get_path('purelib'), sysconfig.get_path('platlib'), site.getusersitepackages()],
}))
"""


def search_module_files(
    requests: list[ImportRequest],
    *,
    python: str | None = None,
    cwd: str | None = None,
    just_my_code: bool = True,
) -> list[Path]:
    """Resolve exactly the supplied imports; never parse or import found files."""
    executable = python or sys.executable
    # A --python path is relative to tdb's launch directory, not --cwd.
    if os.path.dirname(executable):
        executable = os.path.abspath(executable)
    working = Path(cwd or Path.cwd()).resolve()
    result = subprocess.run(
        [executable, "-P", "-c", _METADATA_SCRIPT],
        cwd=working,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    marker = "TDB_SOURCE_PATHS:"
    payload = next(
        (
            line[len(marker) :]
            for line in reversed(result.stdout.splitlines())
            if line.startswith(marker)
        ),
        None,
    )
    if payload is None:
        raise ValueError("Python did not return its module search paths")
    metadata = json.loads(payload)

    def directories(values):
        paths = []
        for value in values:
            if not isinstance(value, str):
                continue
            path = Path(value)
            path = (path if path.is_absolute() else working / path).resolve()
            if path.is_dir() and path not in paths:
                paths.append(path)
        return paths

    search_paths = directories(metadata["paths"])
    stdlib = directories(metadata["stdlib"])
    sites = directories(metadata["site"])
    sites.extend(
        p for p in search_paths if p.name in {"site-packages", "dist-packages"}
    )
    found: set[Path] = set()
    for request in requests:
        roots = list(request.roots)
        if not request.relative:
            roots = list(dict.fromkeys(roots + search_paths))
        for file in _resolve_module(roots, request.name.split(".")):
            file = file.resolve()
            if (
                just_my_code
                and any(file.is_relative_to(root) for root in stdlib)
                and not any(file.is_relative_to(root) for root in sites)
            ):
                continue
            try:
                with file.open("rb"):
                    pass
            except OSError:
                continue
            found.add(file)
    return sorted(found)


def write_source_list(filename: Path, paths: list[Path]) -> None:
    """Atomically save absolute paths in the existing UTF-8 source-list format."""
    names = [str(path.resolve()) for path in paths]
    # The existing reader treats # as a comment and strips whitespace. Reject
    # names it cannot represent instead of silently writing a corrupted list.
    if any(
        "#" in name or name.splitlines() != [name] or name != name.strip()
        for name in names
    ):
        raise ValueError(
            "Source list cannot represent paths containing #, newlines, or trailing whitespace"
        )
    destination = filename.resolve()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.writelines(name + "\n" for name in names)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

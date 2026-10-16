"""pyproject's package-data ships every per-language example directory."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LANGUAGES = (
    "C",
    "C++",
    "Fortran",
    "Go",
    "OCaml",
    "Perl",
    "PowerShell",
    "Python",
    "Ruby",
    "Rust",
    "bash",
    "tcsh",
)


def _packaged() -> set[str]:
    """Paths under examples/ that the tdb package-data globs select."""
    config = tomllib.loads((REPO / "pyproject.toml").read_text())
    patterns = config["tool"]["setuptools"]["package-data"]["tdb"]
    pkg = REPO / "src" / "tdb"
    return {
        p.relative_to(pkg / "examples").as_posix()
        for pat in patterns
        if pat.startswith("examples/")
        for p in pkg.glob(pat)
        if p.is_file()
    }


def test_examples_link_points_at_repo_examples():
    link = REPO / "src" / "tdb" / "examples"
    if not link.is_dir():
        pytest.skip("checkout without symlink support")
    assert link.resolve() == (REPO / "examples").resolve()


def _example_sources() -> set[str]:
    """Example files that belong in the package, relative to examples/.

    `git ls-files` when git works. CI runs in a container holding a clean
    copy of the checkout but no git binary; there, every file on disk is a
    tracked one, so walk the directories (skipping build outputs anyway)."""
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "--", *(f"examples/{lang}" for lang in LANGUAGES)],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        tracked = None
    if tracked is not None and tracked.returncode == 0:
        files = [f.removeprefix("examples/") for f in tracked.stdout.splitlines()]
    else:
        root = REPO / "examples"
        files = [
            p.relative_to(root).as_posix()
            for lang in LANGUAGES
            for p in (root / lang).rglob("*")
            if p.is_file()
            and "target" not in p.relative_to(root).parts
            and p.name != "Cargo.lock"
        ]
    return {f for f in files if not f.endswith(".gitignore")}


def test_every_tracked_language_example_is_packaged():
    want = _example_sources()
    got = _packaged()
    assert want <= got, sorted(want - got)
    for lang in LANGUAGES:
        assert any(p.startswith(f"{lang}/") for p in got), lang
    # Build outputs (cargo's target/) must never be swept in.
    assert not [p for p in got if "/target/" in p], got

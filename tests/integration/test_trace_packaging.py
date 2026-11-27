"""The wheel ships the trace agent and the example services."""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_wheel_contains_agent_and_examples(tmp_path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not installed")
    # Build from a copy: the checkout's build/ and egg-info may be stale.
    work = tmp_path / "copy"
    work.mkdir()
    ignore = shutil.ignore_patterns(
        "build", "*.egg-info", "__pycache__", ".superpowers"
    )
    for name in ("pyproject.toml", "README.md", "LICENSE", "mkdocs.yml"):
        shutil.copy2(ROOT / name, work / name)
    for name in ("src", "docs", "examples"):
        shutil.copytree(ROOT / name, work / name, symlinks=True, ignore=ignore)
    done = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", "dist"],
        cwd=work,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode == 0, done.stderr
    (wheel,) = (work / "dist").glob("*.whl")
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
    for wanted in (
        "tdb/trace/agent/sitecustomize.py",
        "tdb/trace/agent/_tdb_trace_agent.py",
        "tdb/examples/Python/trace/checkout.yaml",
        "tdb/examples/Python/trace/inventory_service.py",
        "tdb/docs/python/tracing.md",
    ):
        assert wanted in names, wanted
    assert not [n for n in names if "/trace/agent/" in n and n.endswith("__init__.py")]

"""Opt-in benchmark: the agent must stay cheap for code it does not trace.

Run with ``TDB_TRACE_BENCH=1 uv run pytest -s`` on this file.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from tests.integration.trace_helpers import run_traced, target

pytestmark = [
    pytest.mark.skipif(sys.version_info < (3, 12), reason="sys.monitoring"),
    pytest.mark.skipif(
        os.environ.get("TDB_TRACE_BENCH") != "1", reason="set TDB_TRACE_BENCH=1"
    ),
]

LOOP = """\
import time

def untraced(x):
    return x + 1

def traced_never_called():
    pass

def main():
    t = time.perf_counter()
    n = 0
    for _ in range(1_000_000):
        n = untraced(n)
    print(time.perf_counter() - t)

main()
"""


def test_overhead_untraced_loop(tmp_path):
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    (plain_dir / "prog.py").write_text(LOOP, encoding="utf-8")
    best_plain = best_traced = float("inf")
    for i in range(3):  # best of three smooths scheduler noise
        done = subprocess.run(
            [sys.executable, "prog.py"],
            cwd=plain_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert done.returncode == 0, done.stderr
        best_plain = min(best_plain, float(done.stdout))
        traced_dir = tmp_path / f"traced{i}"
        traced_dir.mkdir()
        parts = run_traced(
            traced_dir,
            LOOP,
            [target(traced_dir / "prog.py", "traced_never_called")],
            timeout=120,
        )
        assert parts.returncode == 0, parts.stderr
        best_traced = min(best_traced, float(parts.stdout))
    print(
        f"\n1,000,000 untraced calls: plain {best_plain:.3f} s, "
        f"under the agent {best_traced:.3f} s, ratio {best_traced / best_plain:.2f}"
    )
    assert best_traced < 2 * best_plain

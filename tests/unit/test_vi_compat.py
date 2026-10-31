"""Golden compatibility: ViEngine vs recorded real-nvim results.

Regenerate fixtures with ``python scripts/vi_golden.py [--only ID ...]``.
Cases carrying a ``"difference"`` key are known gaps and are skipped.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tdb.vi import ViEngine

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "vi"
CASES = json.loads((FIX / "cases.json").read_text(encoding="utf-8"))
GOLDEN = json.loads((FIX / "golden.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
async def test_matches_nvim(case):
    if "difference" in case:
        pytest.skip(case["difference"])
    e = ViEngine(case["text"], cursor=tuple(case["cursor"]))
    e.feed_keys(case["keys"])
    want = GOLDEN[case["id"]]
    assert (e.text, list(e.cursor)) == (want["text"], want["cursor"])


def test_every_case_has_golden_entry():
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    assert set(ids) == set(GOLDEN), "cases.json and golden.json out of sync"


def test_vi_compat_has_no_todo_differences():
    todo = [c["id"] for c in CASES if c.get("difference", "").startswith("TODO")]
    assert todo == []

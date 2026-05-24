"""Tests for tdb.processes_cache round-trip + invalidation."""

from __future__ import annotations

import json

import pytest

from tdb import processes_cache
from tdb.dap.types import Scope, Source, StackFrame, Variable
from tdb.inspection import ProcessInfo


@pytest.fixture(autouse=True)
def _isolated_cache_path(tmp_path, monkeypatch):
    """Redirect cache_path() to the per-test tmp_path so the real
    tdb cache file in /tmp is never touched."""
    target = tmp_path / "cache.json"
    monkeypatch.setattr(processes_cache, "cache_path", lambda: target)
    yield target


def _sample_payload():
    procs = [
        ProcessInfo(
            name="ForkPoolWorker-1",
            pid=1001,
            alive=True,
            exitcode=None,
            daemon=True,
            target="<f>",
            args="()",
            kwargs="{}",
            start_method="fork",
        ),
        ProcessInfo(
            name="ForkPoolWorker-2",
            pid=1002,
            alive=False,
            exitcode=0,
            daemon=True,
            target="<f>",
            args="()",
            kwargs="{}",
            start_method="fork",
        ),
    ]
    details = {
        1001: {
            "frames": [
                StackFrame(
                    id=10,
                    name="main",
                    line=42,
                    column=4,
                    source=Source(path="/tmp/a.py", name="a.py"),
                ),
                StackFrame(id=11, name="inner", line=99, source=None),
            ],
            "scopes": [Scope(name="Locals", variables_reference=5)],
            "variables": {
                5: [
                    Variable(name="x", value="1", type="int"),
                    Variable(
                        name="lst",
                        value="[1,2]",
                        type="list",
                        variables_reference=7,
                    ),
                ],
            },
        },
    }
    return procs, details, 1001


def test_round_trip_preserves_all_fields(_isolated_cache_path):
    procs, details, current_pid = _sample_payload()
    processes_cache.save(procs, details, current_pid)
    loaded = processes_cache.load()

    assert loaded is not None
    assert [p.pid for p in loaded["processes"]] == [1001, 1002]
    assert loaded["processes"][0].name == "ForkPoolWorker-1"
    assert loaded["processes"][0].alive is True
    assert loaded["processes"][1].exitcode == 0
    assert loaded["current_pid"] == 1001
    assert set(loaded["details"].keys()) == {1001}
    d = loaded["details"][1001]
    assert d["frames"][0].source is not None
    assert d["frames"][0].source.path == "/tmp/a.py"
    assert d["frames"][1].source is None
    assert d["scopes"][0].variables_reference == 5
    assert list(d["variables"].keys()) == [5]
    assert d["variables"][5][1].variables_reference == 7


def test_load_returns_none_when_missing(_isolated_cache_path):
    assert processes_cache.load() is None


def test_load_returns_none_on_corrupt_json(_isolated_cache_path):
    _isolated_cache_path.write_text("not json {")
    assert processes_cache.load() is None


def test_load_returns_none_on_schema_mismatch(_isolated_cache_path):
    _isolated_cache_path.write_text(json.dumps({"not": "the right shape"}))
    assert processes_cache.load() is None


def test_clear_removes_file(_isolated_cache_path):
    processes_cache.save(*_sample_payload())
    assert _isolated_cache_path.is_file()
    processes_cache.clear()
    assert not _isolated_cache_path.exists()


def test_clear_is_noop_when_missing(_isolated_cache_path):
    # Should not raise even though the file doesn't exist.
    processes_cache.clear()


def test_round_trip_with_empty_details(_isolated_cache_path):
    procs = [
        ProcessInfo(
            name="x",
            pid=1,
            alive=True,
            exitcode=None,
            daemon=False,
            target="",
            args="",
            kwargs="",
            start_method="spawn",
        )
    ]
    processes_cache.save(procs, {}, None)
    loaded = processes_cache.load()
    assert loaded is not None
    assert loaded["processes"][0].name == "x"
    assert loaded["details"] == {}
    assert loaded["current_pid"] is None

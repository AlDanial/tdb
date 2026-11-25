"""Tests for the tdb <-> trace-agent contract and the agent's pure helpers."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import sys

import pytest

from tdb.trace.contract import (
    AGENT_DIR,
    CONFIG_ENV,
    DEFAULT_MAX_EVENTS,
    DEFAULT_REPR_LIMIT,
    DISCOVER_CAP,
    FLUSH_INTERVAL,
    PARTS_ENV,
    build_env,
    write_config,
)


def _load_agent_file(name: str, filename: str):
    """Load a file from AGENT_DIR as a module, without touching sys.path."""
    spec = importlib.util.spec_from_file_location(name, AGENT_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_build_env_prepends_agent_dir_and_strips_stale(tmp_path):
    base = {
        "PYTHONPATH": os.pathsep.join([str(AGENT_DIR), "/x"]),
        "TDB_TRACE_OLD": "1",
        "HOME": "/h",
    }
    env = build_env(base, config_path=tmp_path / "c.json", parts_dir=tmp_path / "p")
    assert env["PYTHONPATH"].split(os.pathsep) == [str(AGENT_DIR), "/x"]
    assert "TDB_TRACE_OLD" not in env and env["HOME"] == "/h"
    assert env[CONFIG_ENV] == str(tmp_path / "c.json") and env[PARTS_ENV] == str(
        tmp_path / "p"
    )


def test_build_env_without_pythonpath_adds_no_empty_entry(tmp_path):
    # An empty PYTHONPATH entry would put the cwd on the program's sys.path.
    env = build_env({}, config_path=tmp_path / "c.json", parts_dir=tmp_path / "p")
    assert env["PYTHONPATH"] == str(AGENT_DIR)


def test_write_config_uses_shared_format(tmp_path):
    targets = [{"files": ["/a.py"], "suffixes": [], "functions": []}]
    write_config(
        tmp_path / "c.json", mode="trace", parts_dir=tmp_path / "p", targets=targets
    )
    assert json.loads((tmp_path / "c.json").read_text()) == {
        "version": 1,
        "mode": "trace",
        "parts": str(tmp_path / "p"),
        "agent_dir": str(AGENT_DIR),
        "targets": targets,
        "max_events": DEFAULT_MAX_EVENTS,
        "repr_limit": DEFAULT_REPR_LIMIT,
        "flush_interval": FLUSH_INTERVAL,
        "discover_cap": DISCOVER_CAP,
    }


def test_agent_dir_has_only_two_files():
    assert sorted(p.name for p in AGENT_DIR.iterdir() if p.name != "__pycache__") == [
        "_tdb_trace_agent.py",
        "sitecustomize.py",
    ]


def test_skip_reason():
    sc = _load_agent_file("tdb_sitecustomize_under_test", "sitecustomize.py")
    assert sc.skip_reason((3, 11, 9), None) == "python 3.11 is older than 3.12"
    assert sc.skip_reason((3, 13, 1), False) == "free-threaded build without the GIL"
    assert sc.skip_reason((3, 12, 0), True) is None


def test_sitecustomize_parses_on_python_3_7():
    ast.parse((AGENT_DIR / "sitecustomize.py").read_text(), feature_version=(3, 7))


@pytest.mark.skipif(sys.version_info < (3, 12), reason="sys.monitoring")
def test_pick_tool_id_skips_taken_ids():
    agent = _load_agent_file("tdb_trace_agent_under_test", "_tdb_trace_agent.py")
    mon = sys.monitoring
    if mon.get_tool(3) is not None or mon.get_tool(4) is not None:
        pytest.skip("tool id 3 or 4 already in use in the test process")
    assert agent.pick_tool_id() == 3
    mon.use_tool_id(3, "test-3")
    try:
        assert agent.pick_tool_id() == 4
        mon.use_tool_id(4, "test-4")
        try:
            assert agent.pick_tool_id() is None
        finally:
            mon.free_tool_id(4)
    finally:
        mon.free_tool_id(3)

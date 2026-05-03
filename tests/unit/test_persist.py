"""Unit tests for tdb.persist (config + breakpoints)."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from tdb.dap.types import SourceBreakpoint


@pytest.fixture
def isolated_persist(tmp_path, monkeypatch):
    """Reload persist with CONFIG_DIR pointing inside tmp_path.

    Reloading is the cleanest way to flip the module-level constants
    that depend on Path.home().
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    # Windows uses USERPROFILE for Path.home(); set it too so the same fixture
    # works on either platform.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))

    import tdb.persist as persist_module
    persist_module = importlib.reload(persist_module)
    return persist_module


def test_default_config_dir_unix(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path))
    import tdb.persist as persist_module
    persist_module = importlib.reload(persist_module)
    assert persist_module.CONFIG_DIR == tmp_path / ".config" / "tdb"


def test_default_config_dir_windows_with_appdata(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    appdata = tmp_path / "AppData" / "Roaming"
    monkeypatch.setenv("APPDATA", str(appdata))
    import tdb.persist as persist_module
    persist_module = importlib.reload(persist_module)
    assert persist_module.CONFIG_DIR == appdata / "tdb"


def test_default_config_dir_windows_no_appdata(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    import tdb.persist as persist_module
    persist_module = importlib.reload(persist_module)
    expected = tmp_path / "AppData" / "Roaming" / "tdb"
    assert persist_module.CONFIG_DIR == expected


def test_save_and_load_breakpoints_per_program(isolated_persist):
    bps = {"/abs/foo.py": [SourceBreakpoint(line=10), SourceBreakpoint(line=20)]}
    isolated_persist.save_breakpoints(bps, program="/abs/foo.py")

    loaded = isolated_persist.load_breakpoints(program="/abs/foo.py")
    assert sorted(b.line for b in loaded["/abs/foo.py"]) == [10, 20]

    # Different program key returns nothing
    other = isolated_persist.load_breakpoints(program="/abs/bar.py")
    assert other == {}


def test_save_breakpoints_preserves_other_program_keys(isolated_persist):
    isolated_persist.save_breakpoints(
        {"/abs/a.py": [SourceBreakpoint(line=1)]}, program="/abs/a.py",
    )
    isolated_persist.save_breakpoints(
        {"/abs/b.py": [SourceBreakpoint(line=2)]}, program="/abs/b.py",
    )
    assert isolated_persist.load_breakpoints(program="/abs/a.py")["/abs/a.py"][0].line == 1
    assert isolated_persist.load_breakpoints(program="/abs/b.py")["/abs/b.py"][0].line == 2


def test_breakpoint_round_trip_preserves_condition_and_disabled(isolated_persist):
    bps = {"/abs/x.py": [
        SourceBreakpoint(line=5, condition="i > 3", hit_condition="2"),
        SourceBreakpoint(line=8, enabled=False),
    ]}
    isolated_persist.save_breakpoints(bps, program="/abs/x.py")
    loaded = isolated_persist.load_breakpoints(program="/abs/x.py")
    by_line = {b.line: b for b in loaded["/abs/x.py"]}
    assert by_line[5].condition == "i > 3"
    assert by_line[5].hit_condition == "2"
    assert by_line[8].enabled is False


def test_legacy_last_run_json_is_migrated(isolated_persist):
    """If breakpoints.json is missing but last_run.json is present, rename."""
    legacy = isolated_persist._LEGACY_STATE_FILE
    state = isolated_persist.STATE_FILE
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({
        "programs": {"/abs/x.py": {"/abs/x.py": [{"line": 11}]}}
    }))
    assert not state.exists()

    loaded = isolated_persist.load_breakpoints(program="/abs/x.py")
    assert loaded["/abs/x.py"][0].line == 11
    assert state.exists()
    assert not legacy.exists()


def test_save_config_round_trip(isolated_persist):
    isolated_persist.save_config(keybindings="emacs")
    assert isolated_persist.load_keybinding_scheme() == "emacs"

    # Updating one field preserves the other
    isolated_persist.save_config(theme="textual-dark")
    assert isolated_persist.load_keybinding_scheme() == "emacs"
    assert isolated_persist.load_theme() == "textual-dark"


def test_load_config_missing_file_returns_empty(isolated_persist):
    assert isolated_persist.load_config_raw() == {}
    assert isolated_persist.load_keybinding_scheme() is None
    assert isolated_persist.load_theme() is None


def test_load_breakpoints_corrupt_json_returns_empty(isolated_persist):
    isolated_persist.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    isolated_persist.STATE_FILE.write_text("{not json")
    assert isolated_persist.load_breakpoints(program="/abs/x.py") == {}

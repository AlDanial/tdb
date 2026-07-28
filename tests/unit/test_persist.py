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
        {"/abs/a.py": [SourceBreakpoint(line=1)]},
        program="/abs/a.py",
    )
    isolated_persist.save_breakpoints(
        {"/abs/b.py": [SourceBreakpoint(line=2)]},
        program="/abs/b.py",
    )
    assert (
        isolated_persist.load_breakpoints(program="/abs/a.py")["/abs/a.py"][0].line == 1
    )
    assert (
        isolated_persist.load_breakpoints(program="/abs/b.py")["/abs/b.py"][0].line == 2
    )


def test_breakpoint_round_trip_preserves_condition_and_disabled(isolated_persist):
    bps = {
        "/abs/x.py": [
            SourceBreakpoint(line=5, condition="i > 3", hit_condition="2"),
            SourceBreakpoint(line=8, enabled=False),
        ]
    }
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
    legacy.write_text(
        json.dumps({"programs": {"/abs/x.py": {"/abs/x.py": [{"line": 11}]}}})
    )
    assert not state.exists()

    loaded = isolated_persist.load_breakpoints(program="/abs/x.py")
    assert loaded["/abs/x.py"][0].line == 11
    assert state.exists()
    assert not legacy.exists()


def test_save_config_round_trip(isolated_persist):
    TdbConfig = isolated_persist.TdbConfig
    cfg = isolated_persist.load_config()
    cfg.keybindings = "emacs"
    isolated_persist.save_config(cfg)
    assert isolated_persist.load_config().keybindings == "emacs"

    # Updating one field preserves the other (load-mutate-save pattern).
    cfg2 = isolated_persist.load_config()
    cfg2.theme = "textual-dark"
    isolated_persist.save_config(cfg2)
    reloaded = isolated_persist.load_config()
    assert reloaded.keybindings == "emacs"
    assert reloaded.theme == "textual-dark"


def test_load_config_missing_file_returns_defaults(isolated_persist):
    cfg = isolated_persist.load_config()
    assert cfg.keybindings == "vim"
    assert cfg.theme is None
    assert cfg.step_mode == "statement"


def test_config_from_dict_drops_unknown_keys(isolated_persist):
    cfg = isolated_persist.TdbConfig.from_dict(
        {
            "keybindings": "emacs",
            "unknown_future_key": "ignored",
        }
    )
    assert cfg.keybindings == "emacs"
    assert cfg.theme is None


def test_config_from_dict_rejects_invalid_step_mode(isolated_persist):
    # Bad step_mode falls back to default rather than raising.
    cfg = isolated_persist.TdbConfig.from_dict({"step_mode": "bogus"})
    assert cfg.step_mode == "statement"


def test_load_breakpoints_corrupt_json_returns_empty(isolated_persist):
    isolated_persist.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    isolated_persist.STATE_FILE.write_text("{not json")
    assert isolated_persist.load_breakpoints(program="/abs/x.py") == {}


def test_config_adapter_fields_default_empty(isolated_persist):
    TdbConfig = isolated_persist.TdbConfig
    cfg = TdbConfig()
    assert cfg.adapters == {}
    assert cfg.default_adapters == {}


def test_config_adapter_fields_from_dict(isolated_persist):
    TdbConfig = isolated_persist.TdbConfig
    cfg = TdbConfig.from_dict(
        {
            "adapters": {"lldb-dap": "/opt/llvm/bin/lldb-dap"},
            "default_adapters": {"cpp": "gdb"},
        }
    )
    assert cfg.adapters == {"lldb-dap": "/opt/llvm/bin/lldb-dap"}
    assert cfg.default_adapters == {"cpp": "gdb"}


def test_config_adapter_fields_reject_bad_shapes(isolated_persist):
    TdbConfig = isolated_persist.TdbConfig
    cfg = TdbConfig.from_dict({"adapters": "nope", "default_adapters": [1]})
    assert cfg.adapters == {}
    assert cfg.default_adapters == {}


def test_config_adapter_fields_round_trip(isolated_persist):
    TdbConfig = isolated_persist.TdbConfig
    cfg = TdbConfig(adapters={"gdb": "/usr/bin/gdb"})
    assert TdbConfig.from_dict(cfg.to_dict()).adapters == {"gdb": "/usr/bin/gdb"}


def test_transient_breakpoints_not_saved(isolated_persist):
    """-t breakpoints (persist=False) must never reach breakpoints.json."""
    bps = {
        "/abs/x.py": [
            SourceBreakpoint(line=5),
            SourceBreakpoint(line=9, persist=False),
        ],
        "/abs/only_transient.py": [SourceBreakpoint(line=1, persist=False)],
    }
    isolated_persist.save_breakpoints(bps, program="/abs/x.py")
    loaded = isolated_persist.load_breakpoints(program="/abs/x.py")
    assert [b.line for b in loaded["/abs/x.py"]] == [5]
    assert "/abs/only_transient.py" not in loaded

"""Tool write-location redirects under --config-dir (tdb.tool_env)."""

from __future__ import annotations

import os

from tdb.config_dir import apply_config_dir
from tdb.tool_env import apply_tool_redirects, tool_redirects

NEVER_SET = (
    "HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "GOROOT",
    "GOPATH",
    "CARGO_HOME",
    "RUSTUP_HOME",
    "GEM_HOME",
    "OPAMROOT",
)


def test_sets_unset_vars(tmp_path):
    env = {}
    apply_tool_redirects(tmp_path, env)
    assert env["GOCACHE"] == str(tmp_path / "tools" / "go" / "build")
    assert env["XDG_CACHE_HOME"] == str(tmp_path / "tools" / "cache")


def test_user_value_wins(tmp_path):
    env = {"GOCACHE": "/mine", "XDG_CACHE_HOME": "/c"}
    apply_tool_redirects(tmp_path, env)
    assert env["GOCACHE"] == "/mine" and env["XDG_CACHE_HOME"] == "/c"


def test_empty_value_is_filled(tmp_path):
    env = {"GOCACHE": ""}
    apply_tool_redirects(tmp_path, env)
    assert env["GOCACHE"] == str(tmp_path / "tools" / "go" / "build")


def test_returns_only_what_it_set(tmp_path):
    env = {"GOCACHE": "/mine"}
    applied = apply_tool_redirects(tmp_path, env)
    assert "GOCACHE" not in applied
    assert applied["XDG_CACHE_HOME"] == env["XDG_CACHE_HOME"]
    assert set(applied) == set(env) - {"GOCACHE"}


def test_never_touches_lookup_locations(tmp_path):
    env = {}
    apply_tool_redirects(tmp_path, env)
    for k in NEVER_SET:
        assert k not in env
    for k in NEVER_SET:
        assert k not in tool_redirects(tmp_path)


def test_gomodcache_skipped_when_gopath_set(tmp_path):
    env = {"GOPATH": "/g"}
    apply_tool_redirects(tmp_path, env)
    assert "GOMODCACHE" not in env


def _fake_home(monkeypatch, home):
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


def test_gomodcache_set_when_gopath_unset(monkeypatch, tmp_path):
    _fake_home(monkeypatch, tmp_path / "home")
    env = {}
    apply_tool_redirects(tmp_path, env)
    assert env["GOMODCACHE"] == str(tmp_path / "tools" / "go" / "mod")


def test_gomodcache_skipped_when_default_modcache_exists(monkeypatch, tmp_path):
    """An existing ~/go/pkg/mod holds the user's downloaded modules;
    redirecting would break offline builds that worked before."""
    home = tmp_path / "home"
    (home / "go" / "pkg" / "mod").mkdir(parents=True)
    _fake_home(monkeypatch, home)
    env = {}
    apply_tool_redirects(tmp_path / "cfg", env)
    assert "GOMODCACHE" not in env
    assert "GOCACHE" in env


def test_table_is_complete_and_under_tools(tmp_path):
    table = tool_redirects(tmp_path)
    assert table == {
        "XDG_CACHE_HOME": str(tmp_path / "tools" / "cache"),
        "XDG_STATE_HOME": str(tmp_path / "tools" / "state"),
        "GOCACHE": str(tmp_path / "tools" / "go" / "build"),
        "GOMODCACHE": str(tmp_path / "tools" / "go" / "mod"),
        "DEBUGINFOD_CACHE_PATH": str(tmp_path / "tools" / "gdb" / "debuginfod"),
        "GDBHISTFILE": str(tmp_path / "tools" / "gdb" / "history"),
        "RUBY_DEBUG_HISTORY_FILE": str(tmp_path / "tools" / "ruby" / "rdbg_history"),
        "DUNE_CACHE_ROOT": str(tmp_path / "tools" / "ocaml" / "dune"),
        "PIP_CACHE_DIR": str(tmp_path / "tools" / "pip"),
    }


def test_table_independent_of_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("GOCACHE", "/mine")
    monkeypatch.setenv("GOPATH", "/g")
    assert tool_redirects(tmp_path)["GOCACHE"] == str(
        tmp_path / "tools" / "go" / "build"
    )
    assert "GOMODCACHE" in tool_redirects(tmp_path)


def test_creates_no_directories(tmp_path):
    apply_tool_redirects(tmp_path, {})
    assert list(tmp_path.iterdir()) == []


def test_not_applied_without_override(monkeypatch):
    monkeypatch.delenv("TDB_CONFIG_DIR", raising=False)
    monkeypatch.delenv("GOCACHE", raising=False)
    apply_config_dir(None)
    assert "GOCACHE" not in os.environ


def test_applied_with_override(monkeypatch, tmp_path):
    monkeypatch.delenv("GOCACHE", raising=False)
    apply_config_dir(str(tmp_path))
    assert os.environ["GOCACHE"].startswith(str(tmp_path))


def test_applied_via_env_var_alone(monkeypatch, tmp_path):
    monkeypatch.setenv("TDB_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("GOCACHE", raising=False)
    apply_config_dir(None)
    assert os.environ["GOCACHE"] == str(tmp_path / "tools" / "go" / "build")


def test_user_env_wins_through_apply_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("GOCACHE", "/mine")
    apply_config_dir(str(tmp_path))
    assert os.environ["GOCACHE"] == "/mine"


def test_conftest_restores_redirect_keys_part1(tmp_path):
    # Leaks into os.environ; part2 asserts the autouse fixture undid it.
    os.environ.pop("XDG_STATE_HOME", None)
    apply_config_dir(str(tmp_path))
    assert os.environ["XDG_STATE_HOME"].startswith(str(tmp_path))


def test_conftest_restores_redirect_keys_part2(tmp_path):
    assert str(tmp_path.parent) not in os.environ.get("XDG_STATE_HOME", "")
    assert "TDB_CONFIG_DIR" not in os.environ


def test_tdb_mcp_entry_applies_env_override(monkeypatch, tmp_path):
    # `tdb-mcp` bypasses cli.main(); TDB_CONFIG_DIR must still redirect
    # the tools its sessions start.
    import pytest

    import tdb.mcp

    monkeypatch.setenv("TDB_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("GOCACHE", raising=False)
    monkeypatch.setattr(tdb.mcp, "mcp_import_error", lambda: "absent")
    with pytest.raises(SystemExit):
        tdb.mcp.main()
    assert os.environ["GOCACHE"] == str(tmp_path / "tools" / "go" / "build")

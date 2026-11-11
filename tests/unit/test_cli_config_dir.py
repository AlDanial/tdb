"""`tdb --config-dir DIR` / TDB_CONFIG_DIR (tdb.config_dir)."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from tdb import cli, info
from tdb.config_dir import apply_config_dir


def test_flag_sets_env(monkeypatch, tmp_path):
    monkeypatch.delenv("TDB_CONFIG_DIR", raising=False)
    assert apply_config_dir(str(tmp_path / "c")) == tmp_path / "c"
    assert os.environ["TDB_CONFIG_DIR"] == str(tmp_path / "c")
    assert (tmp_path / "c").is_dir()


def test_config_dir_made_absolute(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    apply_config_dir("rel")
    assert os.environ["TDB_CONFIG_DIR"] == str(tmp_path / "rel")


def test_tilde_expanded(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert apply_config_dir("~/x") == tmp_path / "x"


def test_flag_beats_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TDB_CONFIG_DIR", str(tmp_path / "env"))
    assert apply_config_dir(str(tmp_path / "flag")) == tmp_path / "flag"


def test_env_alone_is_honored(monkeypatch, tmp_path):
    monkeypatch.setenv("TDB_CONFIG_DIR", str(tmp_path / "env"))
    assert apply_config_dir(None) == tmp_path / "env"


def test_no_override_returns_none(monkeypatch):
    monkeypatch.delenv("TDB_CONFIG_DIR", raising=False)
    assert apply_config_dir(None) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores permission bits",
)
def test_unwritable_dir_warns_and_continues(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("TDB_CONFIG_DIR", raising=False)
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        apply_config_dir(str(ro / "c"))  # must not raise
    finally:
        ro.chmod(0o700)
    assert "cannot create config dir" in capsys.readouterr().err


def test_write_default_goes_to_config_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("TDB_CONFIG_DIR", raising=False)
    d = tmp_path / "cfg"
    with pytest.raises(SystemExit) as ei:
        cli.main(["--config-dir", str(d), "--config", "write-default"])
    assert ei.value.code == 0
    assert (d / "config.json").is_file()


def test_info_shows_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("TDB_CONFIG_DIR", str(tmp_path / "c"))
    line = next(
        ln
        for ln in info.info_text().splitlines()
        if ln.strip().startswith("config dir")
    )
    assert str(tmp_path / "c") in line
    assert "(from --config-dir/TDB_CONFIG_DIR)" in line


def test_info_without_override_has_no_note(monkeypatch):
    monkeypatch.delenv("TDB_CONFIG_DIR", raising=False)
    line = next(
        ln
        for ln in info.info_text().splitlines()
        if ln.strip().startswith("config dir")
    )
    assert "--config-dir" not in line


def test_child_process_inherits(monkeypatch, tmp_path):
    monkeypatch.delenv("TDB_CONFIG_DIR", raising=False)
    apply_config_dir(str(tmp_path / "c"))
    out = subprocess.run(
        [sys.executable, "-c", "from tdb import persist; print(persist.config_dir())"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert out == str(tmp_path / "c")


def test_env_relative_value_made_absolute(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TDB_CONFIG_DIR", "relenv")
    assert apply_config_dir(None) == tmp_path / "relenv"
    assert os.environ["TDB_CONFIG_DIR"] == str(tmp_path / "relenv")


def test_env_tilde_value_expanded(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("TDB_CONFIG_DIR", "~/envx")
    assert apply_config_dir(None) == tmp_path / "envx"
    assert os.environ["TDB_CONFIG_DIR"] == str(tmp_path / "envx")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores permission bits",
)
def test_existing_unwritable_dir_warns(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("TDB_CONFIG_DIR", raising=False)
    d = tmp_path / "existing"
    d.mkdir()
    d.chmod(0o500)
    try:
        assert apply_config_dir(str(d)) == d  # must not raise
    finally:
        d.chmod(0o700)
    assert f"tdb: warning: config dir {d} is not writable" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores permission bits",
)
def test_second_call_does_not_rewarn(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("TDB_CONFIG_DIR", raising=False)
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        apply_config_dir(str(ro / "c"))
        env_after_first = dict(os.environ)
        apply_config_dir(str(ro / "c"))
    finally:
        ro.chmod(0o700)
    assert capsys.readouterr().err.count("warning") == 1
    assert dict(os.environ) == env_after_first


def test_parse_args_uses_config_dir_config(monkeypatch, tmp_path, capsys):
    """--config-dir alone must be in effect when parse_args reads config.json."""
    monkeypatch.delenv("TDB_CONFIG_DIR", raising=False)
    d = tmp_path / "cfg"
    d.mkdir()
    (d / "config.json").write_text('{"default_adapters": {"python": "nope"}}')
    prog = tmp_path / "p.py"
    prog.write_text("x = 1\n")
    with pytest.raises(SystemExit):
        cli.parse_args(["--config-dir", str(d), str(prog)])
    assert "unknown adapter 'nope'" in capsys.readouterr().err

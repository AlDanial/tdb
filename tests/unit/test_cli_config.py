"""Unit tests for `tdb --config {write-default,install-padwalker}`."""

from __future__ import annotations

import json
import sys
from datetime import datetime

import pytest

from tdb import persist
from tdb.cli import _run_config, parse_args

FROZEN = datetime(2026, 10, 5, 14, 7, 9)


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point persist's module-level CONFIG_DIR / CONFIG_FILE at tmp_path."""
    cfg_dir = tmp_path / "tdb"
    monkeypatch.setattr(persist, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(persist, "CONFIG_FILE", cfg_dir / "config.json")
    return cfg_dir


# --- argument parsing --------------------------------------------------------


@pytest.mark.parametrize("choice", ["write-default", "install-padwalker"])
def test_config_switch_needs_no_program(choice):
    args = parse_args(["--config", choice])
    assert args.config == choice


def test_config_switch_rejects_unknown_choice():
    with pytest.raises(SystemExit):
        parse_args(["--config", "frobnicate"])


def test_config_switch_default_is_none(tmp_path):
    prog = tmp_path / "x.py"
    prog.write_text("\n")
    assert parse_args([str(prog)]).config is None


# --- persist.write_default_config ------------------------------------------


def test_backup_suffix_posix(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert persist.backup_suffix(FROZEN) == "2026-10-05-14:07:09"


def test_backup_suffix_windows_has_no_colons(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert persist.backup_suffix(FROZEN) == "2026-10-05-14.07.09"


def test_write_default_renames_existing_and_writes_defaults(
    isolated_config, monkeypatch
):
    monkeypatch.setattr(sys, "platform", "linux")
    isolated_config.mkdir()
    old = isolated_config / "config.json"
    old.write_text(json.dumps({"keybindings": "emacs", "step_mode": "line"}))

    backup, written = persist.write_default_config(now=FROZEN)

    assert backup == isolated_config / "config.json-2026-10-05-14:07:09"
    assert json.loads(backup.read_text()) == {
        "keybindings": "emacs",
        "step_mode": "line",
    }
    assert written == old
    data = json.loads(old.read_text())
    assert data["keybindings"] == "vim"
    assert data["step_mode"] == "statement"
    assert data["theme"] is None
    assert data["default_adapters"] == {}
    # A fresh first-run file seeds the native adapter keys.
    assert set(data["adapters"]) >= {"gdb", "lldb-dap"}


def test_write_default_without_existing_file(isolated_config):
    backup, written = persist.write_default_config(now=FROZEN)
    assert backup is None
    assert written == isolated_config / "config.json"
    assert json.loads(written.read_text())["keybindings"] == "vim"


def test_write_default_raises_on_unwritable_dir(isolated_config, monkeypatch):
    def boom(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(persist.Path, "mkdir", boom)
    with pytest.raises(OSError):
        persist.write_default_config(now=FROZEN)


# --- cli._run_config -------------------------------------------------------


def _ns(choice: str):
    return parse_args(["--config", choice])


def test_run_config_write_default_reports_paths(isolated_config, capsys, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    isolated_config.mkdir()
    (isolated_config / "config.json").write_text("{}")

    code = _run_config(_ns("write-default"), now=FROZEN)

    out = capsys.readouterr().out
    assert code == 0
    assert "config.json-2026-10-05-14:07:09" in out
    assert str(isolated_config / "config.json") in out


def test_run_config_write_default_failure_exits_1(isolated_config, capsys, monkeypatch):
    def boom(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(persist.Path, "mkdir", boom)
    code = _run_config(_ns("write-default"), now=FROZEN)
    err = capsys.readouterr().err
    assert code == 1
    assert "read-only file system" in err


@pytest.fixture
def fake_padwalker(monkeypatch):
    """Stub ensure_padwalker; record the perl it was asked about."""
    from tdb.adapters.perl import padwalker

    calls: list[str] = []
    outcome: dict = {}

    def fake_ensure(perl, env=None):
        calls.append(perl)
        return padwalker.PadWalkerResult(**outcome)

    monkeypatch.setattr(padwalker, "ensure_padwalker", fake_ensure)
    outcome["calls"] = calls
    return outcome


def _set(outcome: dict, lib_dir, source, message=None):
    calls = outcome.pop("calls")
    outcome.clear()
    outcome.update(lib_dir=lib_dir, source=source, message=message)
    return calls


def test_install_padwalker_uses_config_perl_override(
    isolated_config, fake_padwalker, capsys
):
    isolated_config.mkdir()
    (isolated_config / "config.json").write_text(
        json.dumps({"adapters": {"perl": "/opt/perl/bin/perl"}})
    )
    calls = _set(
        fake_padwalker, "/cache/pw", "built", "tdb: built the bundled PadWalker"
    )

    code = _run_config(_ns("install-padwalker"))

    assert calls == ["/opt/perl/bin/perl"]
    assert code == 0
    assert "built the bundled PadWalker" in capsys.readouterr().out


def test_install_padwalker_falls_back_to_path_perl(
    isolated_config, fake_padwalker, monkeypatch, capsys
):
    import shutil

    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/perl" if name == "perl" else None
    )
    calls = _set(fake_padwalker, None, "native")

    code = _run_config(_ns("install-padwalker"))

    assert calls == ["/usr/bin/perl"]
    assert code == 0
    assert "already" in capsys.readouterr().out.lower()


def test_install_padwalker_reports_cached(
    isolated_config, fake_padwalker, monkeypatch, capsys
):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/perl")
    _set(fake_padwalker, "/cache/5.38-x86_64-pw2.5", "cached")

    code = _run_config(_ns("install-padwalker"))

    assert code == 0
    assert "/cache/5.38-x86_64-pw2.5" in capsys.readouterr().out


def test_install_padwalker_unavailable_exits_1(
    isolated_config, fake_padwalker, monkeypatch, capsys
):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/perl")
    _set(fake_padwalker, None, "unavailable", "tdb: PadWalker unavailable: no compiler")

    code = _run_config(_ns("install-padwalker"))

    assert code == 1
    assert "no compiler" in capsys.readouterr().err


def test_install_padwalker_no_perl_exits_1(
    isolated_config, fake_padwalker, monkeypatch, capsys
):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    calls = _set(fake_padwalker, None, "native")

    code = _run_config(_ns("install-padwalker"))

    assert code == 1
    assert calls == []
    assert "perl" in capsys.readouterr().err.lower()

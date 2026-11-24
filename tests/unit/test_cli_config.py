"""Unit tests for `tdb --config {write-default,install-padwalker,update-adapters}`."""

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
    """Point persist's config dir at tmp_path via TDB_CONFIG_DIR."""
    cfg_dir = tmp_path / "tdb"
    monkeypatch.setenv("TDB_CONFIG_DIR", str(cfg_dir))
    return cfg_dir


# --- argument parsing --------------------------------------------------------


@pytest.mark.parametrize(
    "choice", ["write-default", "install-padwalker", "update-adapters"]
)
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


# --- update-adapters -------------------------------------------------------


@pytest.fixture
def fake_tools(tmp_path, monkeypatch):
    """A fake PATH: executables named in the returned dict exist under
    tmp_path/bin; shutil.which is pointed at that directory only."""
    import os
    import shutil
    import stat

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    real_which = shutil.which

    def make(*names):
        out = {}
        for n in names:
            p = bin_dir / n
            p.write_text("#!/bin/sh\n")
            p.chmod(p.stat().st_mode | stat.S_IXUSR)
            out[n] = str(p)
        return out

    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(
        shutil,
        "which",
        lambda cmd, mode=os.F_OK | os.X_OK, path=None: real_which(
            cmd, mode, path or str(bin_dir)
        ),
    )
    return make


def test_update_adapters_fills_from_path_and_keeps_other_settings(
    isolated_config, fake_tools
):
    tools = fake_tools("gdb", "perl", "dlv")
    isolated_config.mkdir()
    cfg = isolated_config / "config.json"
    cfg.write_text(json.dumps({"keybindings": "emacs", "adapters": {"gdb": None}}))

    results, written = persist.update_adapters()

    assert written == cfg
    data = json.loads(cfg.read_text())
    assert data["keybindings"] == "emacs"
    assert data["adapters"]["gdb"] == tools["gdb"]
    assert data["adapters"]["perl"] == tools["perl"]
    assert data["adapters"]["dlv"] == tools["dlv"]
    # Not found and not previously listed -> not added.
    assert "lldb-dap" not in data["adapters"]
    by_id = {r.adapter_id: r.status for r in results}
    assert by_id["gdb"] == "added" and by_id["lldb-dap"] == "absent"


def _custom_exe(tmp_path, name):
    import stat

    custom = tmp_path / "opt" / name
    custom.parent.mkdir(exist_ok=True)
    custom.write_text("#!/bin/sh\n")
    custom.chmod(custom.stat().st_mode | stat.S_IXUSR)
    return str(custom)


def test_update_adapters_path_hit_replaces_existing_entry(
    isolated_config, fake_tools, tmp_path, capsys
):
    tools = fake_tools("gdb")
    custom = _custom_exe(tmp_path, "gdb")
    isolated_config.mkdir()
    cfg = isolated_config / "config.json"
    cfg.write_text(json.dumps({"adapters": {"gdb": custom}}))

    code = _run_config(_ns("update-adapters"))

    assert code == 0
    assert json.loads(cfg.read_text())["adapters"]["gdb"] == tools["gdb"]
    assert f"{tools['gdb']}  (found on PATH; was {custom})" in capsys.readouterr().out


def test_update_adapters_keeps_present_entry_not_on_path(
    isolated_config, fake_tools, tmp_path
):
    fake_tools()
    custom = _custom_exe(tmp_path, "lldb-dap-21")
    isolated_config.mkdir()
    cfg = isolated_config / "config.json"
    cfg.write_text(json.dumps({"adapters": {"lldb-dap": custom}}))

    results, _ = persist.update_adapters()

    assert json.loads(cfg.read_text())["adapters"]["lldb-dap"] == custom
    (lldb,) = [r for r in results if r.adapter_id == "lldb-dap"]
    assert lldb.status == "kept"


def test_update_adapters_same_as_path_is_kept(isolated_config, fake_tools):
    tools = fake_tools("gdb")
    isolated_config.mkdir()
    cfg = isolated_config / "config.json"
    cfg.write_text(json.dumps({"adapters": {"gdb": tools["gdb"]}}))

    results, _ = persist.update_adapters()

    (gdb,) = [r for r in results if r.adapter_id == "gdb"]
    assert gdb.status == "kept"


def test_update_adapters_flags_missing_entries(
    isolated_config, fake_tools, tmp_path, capsys
):
    tools = fake_tools("lldb-dap")
    isolated_config.mkdir()
    cfg = isolated_config / "config.json"
    gone_gdb = str(tmp_path / "removed" / "gdb")
    gone_lldb = str(tmp_path / "removed" / "lldb-dap")
    gone_pses = str(tmp_path / "removed" / "PowerShellEditorServices")
    cfg.write_text(
        json.dumps(
            {"adapters": {"gdb": gone_gdb, "lldb-dap": gone_lldb, "pses": gone_pses}}
        )
    )

    code = _run_config(_ns("update-adapters"))

    out = capsys.readouterr().out
    assert code == 0
    adapters = json.loads(cfg.read_text())["adapters"]
    assert adapters["gdb"] is None
    assert adapters["lldb-dap"] == tools["lldb-dap"]
    assert adapters["pses"] is None
    assert f"WARNING: {gone_gdb} is no longer present; set to null" in out
    assert f"WARNING: {gone_lldb} is no longer present; now {tools['lldb-dap']}" in out
    assert f"WARNING: {gone_pses} is no longer present" in out
    assert str(cfg) in out


def test_update_adapters_keeps_existing_pses_directory(
    isolated_config, fake_tools, tmp_path
):
    fake_tools()
    pses = tmp_path / "PowerShellEditorServices"
    pses.mkdir()
    isolated_config.mkdir()
    cfg = isolated_config / "config.json"
    cfg.write_text(json.dumps({"adapters": {"pses": str(pses)}}))

    persist.update_adapters()

    assert json.loads(cfg.read_text())["adapters"]["pses"] == str(pses)


def test_update_adapters_without_config_file_writes_one(isolated_config, fake_tools):
    tools = fake_tools("bash")
    results, written = persist.update_adapters()
    data = json.loads(written.read_text())
    assert data["adapters"] == {"bash": tools["bash"]}
    assert data["keybindings"] == "vim"


def test_update_adapters_refuses_to_clobber_unparseable_config(
    isolated_config, fake_tools, capsys
):
    fake_tools("gdb")
    isolated_config.mkdir()
    cfg = isolated_config / "config.json"
    cfg.write_text("{not json")

    code = _run_config(_ns("update-adapters"))

    assert code == 1
    assert cfg.read_text() == "{not json"
    assert "cannot update adapters" in capsys.readouterr().err

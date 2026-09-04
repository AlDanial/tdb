from pathlib import Path

import pytest

from tdb.adapters.powershell.locate import (
    PSES_ENV_VAR,
    PSES_RELEASE,
    START_SCRIPT,
    find_pses,
    find_pwsh,
)


def _make_pses(root: Path) -> Path:
    d = root / "PowerShellEditorServices"
    d.mkdir(parents=True)
    (d / START_SCRIPT).write_text("# stub\n")
    return d


def test_override_dir_wins(tmp_path):
    d = _make_pses(tmp_path / "cfg")
    _make_pses(tmp_path / "envdir")
    assert (
        find_pses(str(d), env={PSES_ENV_VAR: str(tmp_path / "envdir")}, home=tmp_path)
        == d
    )


def test_override_accepts_unzip_root(tmp_path):
    d = _make_pses(tmp_path / "unzipped")
    assert find_pses(str(tmp_path / "unzipped"), env={}, home=tmp_path) == d


def test_env_var_used_when_no_override(tmp_path):
    d = _make_pses(tmp_path / "envdir")
    assert (
        find_pses(None, env={PSES_ENV_VAR: str(tmp_path / "envdir")}, home=tmp_path)
        == d
    )


def test_vscode_extension_newest_version_wins(tmp_path):
    ext = tmp_path / ".vscode" / "extensions"
    old = _make_pses(ext / "ms-vscode.powershell-2024.2.1" / "modules")
    new = _make_pses(ext / "ms-vscode.powershell-2025.10.0" / "modules")
    assert old != new
    assert find_pses(None, env={}, home=tmp_path) == new


def test_vscode_insiders_and_server_dirs_are_searched(tmp_path):
    d = _make_pses(
        tmp_path
        / ".vscode-server"
        / "extensions"
        / "ms-vscode.powershell-2025.1.0"
        / "modules"
    )
    assert find_pses(None, env={}, home=tmp_path) == d


def test_not_found_message_is_actionable(tmp_path):
    with pytest.raises(FileNotFoundError) as ei:
        find_pses(None, env={}, home=tmp_path)
    msg = str(ei.value)
    assert PSES_RELEASE in msg
    assert "PowerShellEditorServices.zip" in msg
    assert '"pses"' in msg and PSES_ENV_VAR in msg


def test_override_without_start_script_names_the_path(tmp_path):
    bogus = tmp_path / "nope"
    bogus.mkdir()
    with pytest.raises(FileNotFoundError, match=str(bogus)):
        find_pses(str(bogus), env={}, home=tmp_path)


def test_find_pwsh_override(tmp_path):
    exe = tmp_path / "pwsh"
    exe.write_text("")
    exe.chmod(0o755)
    assert find_pwsh(str(exe)) == str(exe)


def test_find_pwsh_missing_override_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="pwsh"):
        find_pwsh(str(tmp_path / "missing"))


def test_find_pwsh_uses_path(monkeypatch):
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/pwsh" if name == "pwsh" else None
    )
    assert find_pwsh(None) == "/usr/bin/pwsh"


def test_find_pwsh_not_on_path_hint(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(FileNotFoundError, match="aka.ms/powershell"):
        find_pwsh(None)

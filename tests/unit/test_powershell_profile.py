import shutil
import sys
from pathlib import Path

import pytest

from tdb.dap.types import Capabilities
from tdb.languages import registry
from tdb.languages.base import AdapterNotFoundError, LanguageNotSupportedError
from tdb.languages.powershell import PsesAdapter, build_powershell_profile


def test_profile_shape():
    p = build_powershell_profile()
    assert p.id == "powershell"
    assert p.display_name == "PowerShell"
    assert p.adapter.id == "pses"
    assert p.presentation.lexer == "powershell"
    assert p.presentation.frame_placeholder == "<ScriptBlock>"
    assert p.capabilities.compute_step_units is None
    assert p.capabilities.task_inspection is False
    assert p.capabilities.child_process_strategy is None
    assert p.capabilities.pause_while_running is True
    assert p.adapter.quirks.attach_via_adapter is False
    assert p.adapter.quirks.pre_arm_pause_on_attach is False


def test_registered_in_registry():
    assert "powershell" in registry.known_languages()
    assert registry.resolve("powershell").id == "powershell"


def test_unknown_adapter_rejected():
    with pytest.raises(LanguageNotSupportedError, match="known: pses"):
        build_powershell_profile(adapter="bogus")


@pytest.fixture
def found(tmp_path, monkeypatch):
    """pwsh + PSES both resolvable, so command() gets past its lookups."""
    pwsh = tmp_path / "pwsh"
    pwsh.write_text("#!/bin/sh\n")
    pses = tmp_path / "PowerShellEditorServices"
    pses.mkdir()
    (pses / "Start-EditorServices.ps1").write_text("# stub\n")
    return {"pwsh": str(pwsh), "pses": str(pses)}


def test_command_is_bundled_proxy(found):
    adapter = PsesAdapter(pwsh_executable=found["pwsh"], pses_dir=found["pses"])
    assert adapter.command() == [
        sys.executable,
        "-m",
        "tdb.adapters.powershell",
    ]


def test_command_raises_when_pwsh_is_missing(found, monkeypatch):
    """A missing interpreter must surface as AdapterNotFoundError: the CLI
    prints its hint and exits 2, whereas a proxy-side launch failure never
    reaches the TUI (spec addendum 3.1)."""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    adapter = PsesAdapter(pses_dir=found["pses"])
    with pytest.raises(AdapterNotFoundError) as exc:
        adapter.command()
    assert "pwsh" in exc.value.hint


def test_command_raises_when_pses_is_missing(found, monkeypatch, tmp_path):
    monkeypatch.delenv("TDB_PSES_PATH", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "nohome"))
    adapter = PsesAdapter(pwsh_executable=found["pwsh"])
    with pytest.raises(AdapterNotFoundError) as exc:
        adapter.command()
    assert "PowerShellEditorServices.zip" in exc.value.hint


def test_launch_body_carries_overrides():
    body = PsesAdapter(pwsh_executable="/opt/pwsh", pses_dir="/opt/pses").launch_body(
        program="/x/p.ps1",
        args=["a"],
        cwd="/x",
        env={"K": "V"},
        stop_on_entry=True,
        console="internalConsole",
        opts={},
    )
    assert body == {
        "type": "powershell",
        "request": "launch",
        "program": "/x/p.ps1",
        "args": ["a"],
        "cwd": "/x",
        "stopOnEntry": True,
        "console": "internalConsole",
        "env": {"K": "V"},
        "pwsh": "/opt/pwsh",
        "pses": "/opt/pses",
    }


def test_launch_body_omits_optional_keys():
    body = PsesAdapter().launch_body(
        program="/x/p.ps1",
        args=[],
        cwd="/x",
        env=None,
        stop_on_entry=False,
        console="internalConsole",
        opts={},
    )
    assert "env" not in body and "pwsh" not in body and "pses" not in body
    assert body["stopOnEntry"] is False


def test_overrides_come_from_adapter_paths():
    p = build_powershell_profile(adapter_paths={"pwsh": "/p", "pses": "/s"})
    body = p.adapter.launch_body(
        program="/x/p.ps1",
        args=[],
        cwd="/x",
        env=None,
        stop_on_entry=True,
        console="internalConsole",
        opts={},
    )
    assert body["pwsh"] == "/p" and body["pses"] == "/s"


def test_external_terminal_rejected():
    with pytest.raises(LanguageNotSupportedError, match="--terminal is not supported"):
        PsesAdapter().launch_body(
            program="/x/p.ps1",
            args=[],
            cwd="/x",
            env=None,
            stop_on_entry=True,
            console="externalTerminal",
            opts={},
        )


def test_attach_rejected():
    with pytest.raises(LanguageNotSupportedError):
        PsesAdapter().attach_body(host="h", port=1, opts={})


def test_no_exception_filters():
    caps = Capabilities(exception_breakpoint_filters=[{"filter": "x", "default": True}])
    assert PsesAdapter().pick_exception_filters(caps) == []


def test_cli_rejects_terminal_for_powershell(tmp_path, monkeypatch):
    from tdb import cli

    script = tmp_path / "s.ps1"
    script.write_text("Write-Host 1\n")
    with pytest.raises(SystemExit):
        cli.parse_args(["--terminal", "xterm", str(script)])

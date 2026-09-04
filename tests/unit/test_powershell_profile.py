import sys

import pytest

from tdb.dap.types import Capabilities
from tdb.languages import registry
from tdb.languages.base import LanguageNotSupportedError
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


def test_command_is_bundled_proxy():
    assert PsesAdapter().command() == [
        sys.executable,
        "-m",
        "tdb.adapters.powershell",
    ]


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

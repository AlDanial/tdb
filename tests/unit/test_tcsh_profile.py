"""The tcsh LanguageProfile: registration, adapter command, launch body."""

import sys

import pytest

from tdb.languages import LanguageNotSupportedError
from tdb.languages import registry


def test_tcsh_is_registered():
    assert "tcsh" in registry.known_languages()


def test_profile_shape():
    profile = registry.resolve("tcsh")
    assert profile.id == "tcsh"
    assert profile.display_name == "Tcsh"
    assert profile.presentation.lexer == "tcsh"
    assert profile.presentation.parse_error is None
    assert profile.capabilities.compute_step_units is None
    assert profile.capabilities.child_process_strategy is None
    assert profile.capabilities.task_inspection is False


def test_adapter_command_is_bundled_module(monkeypatch):
    profile = registry.resolve("tcsh")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/tcsh")
    assert profile.adapter.command() == [sys.executable, "-m", "tdb.adapters.tcsh"]


def test_adapter_command_missing_tcsh_hint(monkeypatch):
    from tdb.languages.base import AdapterNotFoundError

    profile = registry.resolve("tcsh")
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(AdapterNotFoundError, match="install tcsh"):
        profile.adapter.command()


def test_adapter_command_skips_path_check_with_override(monkeypatch):
    profile = registry.resolve("tcsh", adapter_paths={"tcsh": "/opt/tcsh"})
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert profile.adapter.command() == [sys.executable, "-m", "tdb.adapters.tcsh"]


def test_launch_body_defaults():
    profile = registry.resolve("tcsh")
    body = profile.adapter.launch_body(
        program="/tmp/x.csh",
        args=["a"],
        cwd="/tmp",
        env=None,
        stop_on_entry=True,
        console="internal",
        opts={},
    )
    assert body == {
        "type": "tcsh",
        "request": "launch",
        "program": "/tmp/x.csh",
        "args": ["a"],
        "cwd": "/tmp",
        "stopOnEntry": True,
        "console": "internal",
    }


def test_launch_body_env_and_tcsh_override():
    profile = registry.resolve("tcsh", adapter_paths={"tcsh": "/opt/tcsh"})
    body = profile.adapter.launch_body(
        program="/tmp/x.csh",
        args=[],
        cwd="/tmp",
        env={"K": "V"},
        stop_on_entry=False,
        console="internal",
        opts={},
    )
    assert body["env"] == {"K": "V"}
    assert body["tcshPath"] == "/opt/tcsh"
    assert body["stopOnEntry"] is False


def test_no_remote_attach():
    profile = registry.resolve("tcsh")
    with pytest.raises(LanguageNotSupportedError):
        profile.adapter.attach_body(host="localhost", port=5678, opts={})


def test_launch_body_carries_console():
    profile = registry.resolve("tcsh")
    body = profile.adapter.launch_body(
        program="/tmp/x.csh",
        args=[],
        cwd="/tmp",
        env=None,
        stop_on_entry=True,
        console="externalTerminal",
        opts={},
    )
    assert body["console"] == "externalTerminal"


def test_unknown_adapter_rejected():
    with pytest.raises(LanguageNotSupportedError):
        registry.resolve("tcsh", adapter="tcshdb")

"""The bash LanguageProfile: registration, adapter command, launch body."""

import sys

import pytest

from tdb.languages import LanguageNotSupportedError
from tdb.languages import registry


def test_bash_is_registered():
    assert "bash" in registry.known_languages()


def test_profile_shape():
    profile = registry.resolve("bash")
    assert profile.id == "bash"
    assert profile.display_name == "Bash"
    assert profile.presentation.lexer == "bash"
    assert profile.presentation.frame_placeholder == "main"
    assert profile.presentation.parse_error is None
    assert profile.capabilities.compute_step_units is None
    assert profile.capabilities.child_process_strategy is None
    assert profile.capabilities.task_inspection is False


def test_adapter_command_is_bundled_module():
    profile = registry.resolve("bash")
    assert profile.adapter.command() == [sys.executable, "-m", "tdb.adapters.bash"]


def test_launch_body_defaults():
    profile = registry.resolve("bash")
    body = profile.adapter.launch_body(
        program="/tmp/x.sh",
        args=["a"],
        cwd="/tmp",
        env=None,
        stop_on_entry=True,
        console="internal",
        opts={},
    )
    assert body == {
        "type": "bash",
        "request": "launch",
        "program": "/tmp/x.sh",
        "args": ["a"],
        "cwd": "/tmp",
        "stopOnEntry": True,
        "console": "internal",
    }


def test_launch_body_env_and_bash_override():
    profile = registry.resolve("bash", adapter_paths={"bash": "/opt/bash"})
    body = profile.adapter.launch_body(
        program="/tmp/x.sh",
        args=[],
        cwd="/tmp",
        env={"K": "V"},
        stop_on_entry=False,
        console="internal",
        opts={},
    )
    assert body["env"] == {"K": "V"}
    assert body["bash"] == "/opt/bash"
    assert body["stopOnEntry"] is False


def test_launch_body_carries_console():
    profile = registry.resolve("bash")
    body = profile.adapter.launch_body(
        program="/tmp/x.sh",
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
        registry.resolve("bash", adapter="bashdb")

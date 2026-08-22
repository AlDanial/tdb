import sys

import pytest

from tdb.dap.types import Capabilities
from tdb.languages import registry
from tdb.languages.base import LanguageNotSupportedError
from tdb.languages.ruby import RdbgAdapter, build_ruby_profile


def test_profile_shape():
    p = build_ruby_profile()
    assert p.id == "ruby"
    assert p.display_name == "Ruby"
    assert p.adapter.id == "rdbg"
    assert p.presentation.lexer == "ruby"
    assert p.presentation.frame_placeholder == "<main>"
    assert p.presentation.parse_error is not None
    assert p.capabilities.compute_step_units is None
    assert p.capabilities.task_inspection is False
    assert p.capabilities.child_process_strategy is None
    assert p.capabilities.pause_while_running is True
    # remote attach is DIRECT (rdbg is a DAP server), unlike perl
    assert p.adapter.quirks.attach_via_adapter is False
    assert p.adapter.quirks.pre_arm_pause_on_attach is False


def test_registered_in_registry():
    assert "ruby" in registry.known_languages()
    assert registry.resolve("ruby").id == "ruby"


def test_command_is_bundled_proxy():
    assert RdbgAdapter().command() == [sys.executable, "-m", "tdb.adapters.ruby"]


def test_launch_body_carries_rdbg_override():
    body = RdbgAdapter(rdbg_executable="/opt/bin/rdbg").launch_body(
        program="/x/p.rb",
        args=["a"],
        cwd="/x",
        env={"K": "V"},
        stop_on_entry=True,
        console="internalConsole",
        opts={},
    )
    assert body == {
        "type": "ruby",
        "request": "launch",
        "program": "/x/p.rb",
        "args": ["a"],
        "cwd": "/x",
        "stopOnEntry": True,
        "console": "internalConsole",
        "env": {"K": "V"},
        "rdbg": "/opt/bin/rdbg",
    }


def test_launch_body_omits_optional_keys():
    body = RdbgAdapter().launch_body(
        program="/x/p.rb",
        args=[],
        cwd="/x",
        env=None,
        stop_on_entry=False,
        console="internalConsole",
        opts={},
    )
    assert "env" not in body and "rdbg" not in body
    assert body["stopOnEntry"] is False


def test_attach_body_minimal():
    body = RdbgAdapter().attach_body(host="devbox", port=5678, opts={})
    assert body == {"type": "ruby", "request": "attach"}


def test_attach_body_rejects_path_mappings():
    with pytest.raises(LanguageNotSupportedError):
        RdbgAdapter().attach_body(
            host="devbox",
            port=5678,
            opts={"path_mappings": [("/local", "/remote")]},
        )


def test_adapter_paths_names_rdbg():
    p = build_ruby_profile(adapter_paths={"rdbg": "/opt/bin/rdbg"})
    body = p.adapter.launch_body(
        program="/x/p.rb",
        args=[],
        cwd="/x",
        env=None,
        stop_on_entry=False,
        console="internalConsole",
        opts={},
    )
    assert body["rdbg"] == "/opt/bin/rdbg"


def test_unknown_adapter_rejected():
    with pytest.raises(LanguageNotSupportedError):
        build_ruby_profile(adapter="byebug")


def test_no_exception_filters():
    assert build_ruby_profile().adapter.pick_exception_filters(Capabilities()) == []

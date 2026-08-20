"""The Ruby LanguageProfile: registration, adapter command, launch body."""

import pytest

from tdb.dap.types import Capabilities
from tdb.languages import LanguageNotSupportedError
from tdb.languages import registry
from tdb.languages.ruby import RdbgAdapter


def test_ruby_is_registered():
    assert "ruby" in registry.known_languages()


def test_profile_shape():
    profile = registry.resolve("ruby")
    assert profile.id == "ruby"
    assert profile.adapter.id == "rdbg"
    assert profile.display_name == "Ruby"
    assert profile.presentation.lexer == "ruby"
    assert profile.presentation.frame_placeholder == "<main>"
    assert profile.presentation.parse_error is not None  # parse_ruby_error
    assert profile.capabilities.compute_step_units is None
    assert profile.capabilities.child_process_strategy is None
    assert profile.capabilities.task_inspection is False
    assert profile.capabilities.pause_while_running is True


def test_launch_body_defaults():
    profile = registry.resolve("ruby")
    body = profile.adapter.launch_body(
        program="/tmp/hello.rb",
        args=["arg1"],
        cwd="/tmp",
        env=None,
        stop_on_entry=True,
        console="integratedTerminal",
        opts={},
    )
    assert body == {
        "type": "rdbg",
        "request": "launch",
        "program": "/tmp/hello.rb",
        "args": ["arg1"],
        "cwd": "/tmp",
        "stopOnEntry": True,
        "console": "integratedTerminal",
    }


def test_launch_body_with_env():
    profile = registry.resolve("ruby")
    body = profile.adapter.launch_body(
        program="/tmp/hello.rb",
        args=[],
        cwd="/tmp",
        env={"RUBY_ENV": "test"},
        stop_on_entry=False,
        console="integratedTerminal",
        opts={},
    )
    assert body["env"] == {"RUBY_ENV": "test"}
    assert body["stopOnEntry"] is False


def test_launch_body_with_bundler_option():
    profile = registry.resolve("ruby")
    body = profile.adapter.launch_body(
        program="/tmp/app.rb",
        args=[],
        cwd="/tmp",
        env=None,
        stop_on_entry=False,
        console="integratedTerminal",
        opts={"use_bundler": True},
    )
    assert body["useBundler"] is True


def test_launch_body_with_debug_port():
    profile = registry.resolve("ruby")
    body = profile.adapter.launch_body(
        program="/tmp/app.rb",
        args=[],
        cwd="/tmp",
        env=None,
        stop_on_entry=False,
        console="integratedTerminal",
        opts={"debug_port": 38898},
    )
    assert body["debugPort"] == 38898


def test_launch_body_with_protocol_messages():
    profile = registry.resolve("ruby")
    body = profile.adapter.launch_body(
        program="/tmp/app.rb",
        args=[],
        cwd="/tmp",
        env=None,
        stop_on_entry=False,
        console="integratedTerminal",
        opts={"show_protocol_messages": True},
    )
    assert body["showProtocolMessages"] is True


def test_attach_body_defaults():
    profile = registry.resolve("ruby")
    body = profile.adapter.attach_body(host="127.0.0.1", port=38898, opts={})
    assert body == {
        "type": "rdbg",
        "request": "attach",
        "host": "127.0.0.1",
        "port": 38898,
    }


def test_attach_body_with_path_mappings():
    profile = registry.resolve("ruby")
    body = profile.adapter.attach_body(
        host="localhost",
        port=9999,
        opts={
            "path_mappings": [
                ("/local/app", "/remote/app"),
                ("/local/lib", "/remote/lib"),
            ]
        },
    )
    assert body["pathMappings"] == [
        {"localRoot": "/local/app", "remoteRoot": "/remote/app"},
        {"localRoot": "/local/lib", "remoteRoot": "/remote/lib"},
    ]


def test_ruby_file_detection():
    """Test that .rb, .erb, .rake, .jbuilder files are detected as Ruby."""
    assert registry.detect("script.rb") == "ruby"
    assert registry.detect("template.erb") == "ruby"
    assert registry.detect("Rakefile.rake") == "ruby"
    assert registry.detect("users.jbuilder") == "ruby"


def test_ruby_shebang_detection(tmp_path):
    script = tmp_path / "bin-task"
    script.write_text("#!/usr/bin/env ruby\nputs 'ok'\n")
    assert registry.detect(str(script)) == "ruby"


def test_command_uses_bundled_bridge_and_rdbg_override():
    assert RdbgAdapter().command() == [
        __import__("sys").executable,
        "-m",
        "tdb.adapters.ruby",
    ]


def test_exception_filter_uses_debug_gem_name():
    caps = Capabilities.from_dict(
        {"exceptionBreakpointFilters": [{"filter": "any", "label": "any"}]}
    )
    assert RdbgAdapter().pick_exception_filters(caps) == ["any"]
    assert RdbgAdapter("/opt/bin/rdbg").command() == [
        __import__("sys").executable,
        "-m",
        "tdb.adapters.ruby",
        "--rdbg",
        "/opt/bin/rdbg",
    ]


def test_exception_filter_falls_back_to_default_marked_filters():
    """Without an `any` filter, the adapter selects the `default: True` ones.

    debug.gem can expose only exception-class filters (e.g. RuntimeError)
    rather than `any`; the fallback picks whatever the server marks as
    the default set.
    """
    caps = Capabilities.from_dict(
        {
            "exceptionBreakpointFilters": [
                {"filter": "RuntimeError", "label": "RuntimeError", "default": True},
                {"filter": "NoMethodError", "label": "NoMethodError", "default": False},
            ]
        }
    )
    assert RdbgAdapter().pick_exception_filters(caps) == ["RuntimeError"]


def test_exception_filter_empty_when_no_defaults():
    """No `any` and no `default`-marked filters → empty selection, no crash."""
    caps = Capabilities.from_dict(
        {
            "exceptionBreakpointFilters": [
                {"filter": "RuntimeError", "label": "RuntimeError"}
            ]
        }
    )
    assert RdbgAdapter().pick_exception_filters(caps) == []


def test_unknown_adapter_rejected():
    with pytest.raises(LanguageNotSupportedError):
        registry.resolve("ruby", adapter="byebug")

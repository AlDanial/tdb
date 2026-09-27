from importlib import resources

import pytest

from tdb.languages.base import LanguageNotSupportedError
from tdb.languages.cpp import HOOK_STOP_FUNCTION
from tdb.languages.rust import RustGdbAdapter, RustLldbAdapter, build_rust_profile


def test_probe_scripts_are_package_resources():
    root = resources.files("tdb.rust_concurrency.probes")
    assert root.joinpath("gdb_script.py").is_file()
    assert root.joinpath("lldb_script.py").is_file()


def test_rust_profile_defaults_by_platform(monkeypatch):
    monkeypatch.setattr("tdb.languages.rust.sys.platform", "linux")
    assert build_rust_profile().adapter.id == "gdb"
    monkeypatch.setattr("tdb.languages.rust.sys.platform", "darwin")
    assert build_rust_profile().adapter.id == "lldb-dap"


def test_rust_profile_capabilities():
    profile = build_rust_profile(adapter="lldb-dap")
    assert profile.id == "rust"
    assert profile.presentation.lexer == "rust"
    assert profile.capabilities.pause_while_running is True
    assert profile.capabilities.concurrency_inspection == "rust"


def test_rust_adapters_share_native_local_launch_behavior():
    gdb = RustGdbAdapter(executable="/opt/gdb")
    lldb = RustLldbAdapter(executable="/opt/lldb-dap")
    assert gdb.command()[0] == "/opt/gdb"
    assert gdb.command()[1:3] == ["-iex", "set width unlimited"]
    assert gdb.command()[3] == "-iex"
    assert gdb.command()[-2:] == ["-i", "dap"]
    assert lldb.command() == ["/opt/lldb-dap"]


def test_rust_rejects_unknown_adapter():
    with pytest.raises(
        LanguageNotSupportedError, match="unknown adapter 'codelldb' for rust"
    ):
        build_rust_profile(adapter="codelldb")


def test_rust_gdb_attach_body():
    body = RustGdbAdapter().attach_body(
        host="devbox",
        port=2345,
        opts={"program": "/local/app", "path_mappings": [("/src", "/remote/src")]},
    )
    assert body == {"program": "/local/app", "target": "devbox:2345"}


def test_rust_lldb_attach_body_with_source_map():
    body = RustLldbAdapter().attach_body(
        host="devbox",
        port=2345,
        opts={"program": "/local/app", "path_mappings": [("/src", "/remote/src")]},
    )
    assert body["gdb-remote-host"] == "devbox"
    assert body["gdb-remote-port"] == 2345
    assert body["program"] == "/local/app"
    assert body["sourceMap"] == [["/remote/src", "/src"]]


def test_rust_gdb_source_mapping_commands_escape_paths():
    adapter = RustGdbAdapter()
    assert adapter.pre_configuration_commands(
        [('/local\\path "quote"', '/remote\\path "quote"')]
    ) == (r'set substitute-path "/remote\\path \"quote\"" "/local\\path \"quote\""',)


def test_rust_gdb_launch_injects_rust_backtrace_env():
    body = RustGdbAdapter(executable="gdb").launch_body(
        program="/app",
        args=[],
        cwd="/",
        env=None,
        stop_on_entry=True,
        console="internalConsole",
        opts={},
    )
    assert body["env"]["RUST_BACKTRACE"] == "1"


def test_rust_gdb_launch_keeps_user_rust_backtrace():
    body = RustGdbAdapter(executable="gdb").launch_body(
        program="/app",
        args=[],
        cwd="/",
        env={"RUST_BACKTRACE": "full"},
        stop_on_entry=True,
        console="internalConsole",
        opts={},
    )
    assert body["env"]["RUST_BACKTRACE"] == "full"


def test_rust_lldb_launch_injects_rust_backtrace_env():
    body = RustLldbAdapter(executable="/opt/lldb-dap").launch_body(
        program="/app",
        args=[],
        cwd="/",
        env=None,
        stop_on_entry=True,
        console="internalConsole",
        opts={},
    )
    assert "RUST_BACKTRACE=1" in body["env"]


def test_rust_pid_attach_keeps_init_commands():
    p = build_rust_profile(adapter="lldb-dap", attach_pid=99)
    body = p.adapter.attach_body(
        host="127.0.0.1", port=0, opts={"program": "/bin/prog"}
    )
    assert body["pid"] == 99
    assert body["program"] == "/bin/prog"
    assert any("command script import" in c for c in body["initCommands"])
    assert p.adapter.hook_function_breakpoints() == (HOOK_STOP_FUNCTION,)
    g = build_rust_profile(adapter="gdb", attach_pid=99)
    assert g.adapter.attach_body(host="", port=0, opts={"program": "/bin/prog"}) == {
        "program": "/bin/prog",
        "pid": 99,
    }
    assert g.adapter.quirks.attach_stop_is_pausable is True

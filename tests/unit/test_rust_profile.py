import pytest

from tdb.languages.base import LanguageNotSupportedError
from tdb.languages.rust import RustGdbAdapter, RustLldbAdapter, build_rust_profile


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
    assert gdb.command() == ["/opt/gdb", "-i", "dap"]
    assert lldb.command() == ["/opt/lldb-dap"]


def test_rust_rejects_unknown_adapter():
    with pytest.raises(LanguageNotSupportedError, match="unknown adapter 'codelldb' for rust"):
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
        [("/local\\path \"quote\"", "/remote\\path \"quote\"")]
    ) == (
        r'set substitute-path "/remote\\path \"quote\"" "/local\\path \"quote\""',
    )

"""Contracts for the fixed, read-only LLDB Rust evidence probe."""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from tdb.languages.rust import RustLldbAdapter
from tdb.rust_concurrency.probes import probe_for_adapter
from tdb.rust_concurrency.probes.lldb import (
    LLDB_SNAPSHOT_COMMAND,
    LldbEvidenceProbe,
    parse_lldb_probe_output,
)
from tdb.rust_concurrency.probes import lldb_script


def load_fixture(name: str) -> str:
    return (
        Path(__file__).parents[1] / "fixtures" / "rust_concurrency" / name
    ).read_text()


def expected_script_path() -> str:
    return str(
        resources.files("tdb.rust_concurrency.probes").joinpath("lldb_script.py")
    )


def test_lldb_probe_uses_common_schema():
    result = parse_lldb_probe_output(load_fixture("lldb/rust-1.98.json"))

    assert result.rust_version == "1.98.0"
    assert result.threads[0].dap_thread_hint == "thread #1"
    assert result.primitive_states[0].primitive_id == "mutex:0x10"


async def test_lldb_probe_sends_only_the_fixed_snapshot_command():
    client = type(
        "Client",
        (),
        {
            "evaluate": AsyncMock(
                return_value=load_fixture("lldb/rust-1.98.json")
            )
        },
    )()

    result = await LldbEvidenceProbe().collect(client)

    client.evaluate.assert_awaited_once_with(LLDB_SNAPSHOT_COMMAND, context="repl")
    assert result.rust_version == "1.98.0"


def test_probe_factory_selects_lldb():
    assert isinstance(probe_for_adapter("lldb-dap"), LldbEvidenceProbe)


def test_rust_lldb_loads_probe_script_before_launch():
    body = RustLldbAdapter().launch_body(
        program="/app",
        args=[],
        cwd="/",
        env=None,
        stop_on_entry=True,
        console="externalTerminal",
        opts={},
    )

    assert body["initCommands"] == [
        f'command script import "{expected_script_path()}"'
    ]
    assert body["runInTerminal"] is True


def test_rust_lldb_attach_merges_probe_initialization_with_remote_options():
    body = RustLldbAdapter().attach_body(
        host="devbox",
        port=2345,
        opts={
            "program": "/local/app",
            "path_mappings": [("/src", "/remote/src")],
            "initCommands": ["settings set target.x86-disassembly-flavor intel"],
        },
    )

    assert body == {
        "program": "/local/app",
        "gdb-remote-host": "devbox",
        "gdb-remote-port": 2345,
        "sourceMap": [["/remote/src", "/src"]],
        "initCommands": [
            "settings set target.x86-disassembly-flavor intel",
            f'command script import "{expected_script_path()}"',
        ],
    }


def test_lldb_version_scan_rejects_ambiguous_embedded_versions(tmp_path):
    executable = tmp_path / "app"
    executable.write_bytes(b"rustc version 1.98.0\0rustc version 1.97.1")
    target = Mock()
    target.GetNumModules.return_value = 0
    target.GetExecutable.return_value.GetPath.return_value = str(executable)

    version, warnings = lldb_script._rust_version(target)

    assert version is None
    assert warnings == ("local executable Rust producer version is ambiguous",)


def test_lldb_version_scan_does_not_authorize_layout_from_embedded_string(tmp_path):
    executable = tmp_path / "app"
    executable.write_bytes(b"user text: rustc version 1.98.0")
    target = Mock()
    target.GetNumModules.return_value = 0
    target.GetExecutable.return_value.GetPath.return_value = str(executable)

    version, warnings = lldb_script._rust_version(target)

    assert version is None
    assert warnings == (
        "unverified local executable Rust version candidate 1.98.0; "
        "layout evidence remains disabled",
    )


def test_lldb_snapshot_does_not_scan_version_until_inferior_is_stopped(monkeypatch):
    process = Mock()
    process.GetState.return_value = 7
    target = Mock()
    target.GetProcess.return_value = process
    debugger = Mock()
    debugger.GetSelectedTarget.return_value = target
    result = Mock()
    version_probe = Mock(side_effect=AssertionError("must not be called"))
    monkeypatch.setattr(lldb_script, "lldb", SimpleNamespace(eStateStopped=5))
    monkeypatch.setattr(lldb_script, "_rust_version", version_probe)

    lldb_script.tdb_rust_snapshot(
        debugger, "--format json", result, internal_dict={}
    )

    version_probe.assert_not_called()
    payload = result.PutCString.call_args.args[0]
    assert '"threads":[]' in payload
    assert "inferior is not stopped" in payload


def test_lldb_snapshot_degrades_global_enumeration_failure_to_warning(monkeypatch):
    process = Mock()
    process.GetState.return_value = 5
    process.GetNumThreads.side_effect = RuntimeError("threads failed")
    target = Mock()
    target.GetProcess.return_value = process
    debugger = Mock()
    debugger.GetSelectedTarget.return_value = target
    result = Mock()
    monkeypatch.setattr(lldb_script, "lldb", SimpleNamespace(eStateStopped=5))
    monkeypatch.setattr(lldb_script, "_rust_version", Mock(return_value=("1.98.0", ())))

    lldb_script.tdb_rust_snapshot(
        debugger, "--format json", result, internal_dict={}
    )

    payload = result.PutCString.call_args.args[0]
    assert '"threads":[]' in payload
    assert "LLDB thread enumeration unavailable: threads failed" in payload


def test_lldb_snapshot_keeps_threads_before_index_failure(monkeypatch):
    thread = Mock()
    thread.IsValid.return_value = True
    thread.GetThreadID.return_value = 42
    thread.GetIndexID.return_value = 1
    thread.GetFrameAtIndex.return_value.IsValid.return_value = False
    process = Mock()
    process.GetState.return_value = 5
    process.GetNumThreads.return_value = 2
    process.GetThreadAtIndex.side_effect = [thread, RuntimeError("index failed")]
    target = Mock()
    target.GetProcess.return_value = process
    debugger = Mock()
    debugger.GetSelectedTarget.return_value = target
    result = Mock()
    monkeypatch.setattr(lldb_script, "lldb", SimpleNamespace(eStateStopped=5))
    monkeypatch.setattr(lldb_script, "_rust_version", Mock(return_value=("1.98.0", ())))

    lldb_script.tdb_rust_snapshot(
        debugger, "--format json", result, internal_dict={}
    )

    payload = result.PutCString.call_args.args[0]
    assert '"os_thread_id":"42"' in payload
    assert "LLDB thread index 1 unavailable: index failed" in payload

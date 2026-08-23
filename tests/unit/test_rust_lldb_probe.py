"""Contracts for the fixed, read-only LLDB Rust evidence probe."""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from unittest.mock import AsyncMock

from tdb.languages.rust import RustLldbAdapter
from tdb.rust_concurrency.probes import probe_for_adapter
from tdb.rust_concurrency.probes.lldb import (
    LLDB_SNAPSHOT_COMMAND,
    LldbEvidenceProbe,
    parse_lldb_probe_output,
)


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

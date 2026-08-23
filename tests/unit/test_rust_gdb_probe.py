"""Contracts for the fixed, read-only GDB Rust evidence probe."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from tdb.languages.rust import RustGdbAdapter
from tdb.rust_concurrency.models import Confidence
from tdb.rust_concurrency.probes import probe_for_adapter
from tdb.rust_concurrency.probes.base import gate_supported_layout, parse_probe_output
from tdb.rust_concurrency.probes.gdb import GDB_SNAPSHOT_COMMAND, GdbEvidenceProbe
from tdb.rust_concurrency.probes import gdb_script


def load_fixture(name: str) -> str:
    return (
        Path(__file__).parents[1] / "fixtures" / "rust_concurrency" / name
    ).read_text()


def _envelope(
    *,
    rust_version: str = "1.98.0",
    primitive_states: list[dict[str, object]] | None = None,
) -> str:
    states = primitive_states if primitive_states is not None else []
    return (
        "console noise\n"
        "TDB_RUST_JSON:{"
        f'"rust_version":"{rust_version}",'
        '"threads":[{"dap_thread_hint":"1","os_thread_id":"101"}],'
        f'"primitive_states":{states!r},'
        '"warnings":[]}'
        "\nmore console noise\n"
    ).replace("'", '"')


def test_gdb_probe_parses_marker_wrapped_json():
    raw = 'console noise\nTDB_RUST_JSON:{"rust_version":"1.98.0","threads":[]}\n'

    result = parse_probe_output(raw)

    assert result.rust_version == "1.98.0"
    assert result.threads == ()


def test_probe_parser_preserves_typed_layout_evidence():
    raw = _envelope(
        primitive_states=[
            {
                "primitive_id": "mutex:0x10",
                "owner_os_thread_ids": ["101"],
                "raw_state": "locked",
                "evidence": [
                    {
                        "confidence": "confirmed",
                        "source": "gdb",
                        "detail": "mutex owner",
                    }
                ],
            }
        ]
    )

    result = parse_probe_output(raw)

    assert result.primitive_states[0].primitive_id == "mutex:0x10"
    assert result.primitive_states[0].evidence[0].confidence is Confidence.CONFIRMED


@pytest.mark.parametrize(
    "raw",
    [
        "TDB_RUST_JSON:{not json}\n",
        'TDB_RUST_JSON:{"rust_version": 198}\n',
        'TDB_RUST_JSON:{"rust_version":"1.98.0","threads":[{"dap_thread_hint":1,"os_thread_id":"1"}]}\n',
        'TDB_RUST_JSON:{"rust_version":"1.98.0","primitive_states":[{"primitive_id":"mutex:123","owner_os_thread_ids":[],"raw_state":"locked","evidence":[]}]}\n',
    ],
)
def test_invalid_probe_envelopes_degrade_to_warnings(raw: str):
    result = parse_probe_output(raw)

    assert result.threads == ()
    assert result.primitive_states == ()
    assert result.warnings


def test_duplicate_primitive_ids_are_rejected():
    state = {
        "primitive_id": "mutex:0x10",
        "owner_os_thread_ids": [],
        "raw_state": "locked",
        "evidence": [],
    }

    result = parse_probe_output(_envelope(primitive_states=[state, state]))

    assert result.primitive_states == ()
    assert "duplicate primitive_id" in result.warnings[0]


def test_unsupported_rust_version_disables_layout_evidence():
    result = parse_probe_output(load_fixture("gdb/rust-1.97.json"))

    gated = gate_supported_layout(result, supported="1.98.0")

    assert gated.primitive_states == ()
    assert "unsupported Rust 1.97.0" in gated.warnings[0]


def test_exact_supported_rust_version_keeps_layout_evidence():
    result = parse_probe_output(load_fixture("gdb/rust-1.98.json"))

    assert gate_supported_layout(result, supported="1.98.0").primitive_states


async def test_gdb_probe_sends_only_the_fixed_snapshot_command():
    client = type(
        "Client",
        (),
        {"evaluate": AsyncMock(return_value=(_envelope(), 0))},
    )()

    result = await GdbEvidenceProbe().collect(client)

    client.evaluate.assert_awaited_once_with(GDB_SNAPSHOT_COMMAND, context="repl")
    assert result.rust_version == "1.98.0"


def test_probe_factory_selects_gdb_and_rejects_unknown_adapters():
    assert isinstance(probe_for_adapter("gdb"), GdbEvidenceProbe)
    assert probe_for_adapter("unknown") is None


@pytest.mark.parametrize("adapter_id", ["gdb", "lldb-dap"])
async def test_collector_uses_the_selected_adapter_probe_by_default(
    monkeypatch, adapter_id
):
    from tdb.dap.types import StackFrame, Thread
    from tdb.languages.rust import build_rust_profile
    from tdb.rust_concurrency.collector import RustConcurrencyCollector
    from tdb.rust_concurrency.models import ProbeResult
    from tdb.session.state import DebugState, SessionPhase

    state = DebugState()
    state.transition_to(SessionPhase.STOPPED)
    client = type(
        "Client",
        (),
        {
            "threads": AsyncMock(return_value=[Thread(1, "worker")]),
            "stack_trace": AsyncMock(return_value=[StackFrame(1, "worker::run")]),
        },
    )()
    controller = type(
        "Controller",
        (),
        {
            "client": client,
            "state": state,
            "profile": build_rust_profile(adapter_id),
        },
    )()
    probe = type(
        "Probe", (), {"collect": AsyncMock(return_value=ProbeResult("1.98.0"))}
    )()
    monkeypatch.setattr(
        "tdb.rust_concurrency.collector.probe_for_adapter",
        lambda selected_adapter_id: (
            probe if selected_adapter_id == adapter_id else None
        ),
    )

    snapshot = await RustConcurrencyCollector().collect_and_analyze(controller)

    probe.collect.assert_awaited_once_with(client)
    assert snapshot.rust_version == "1.98.0"


def test_rust_gdb_sources_probe_script_before_starting_dap():
    command = RustGdbAdapter(executable="gdb").command()

    assert command[0] == "gdb"
    assert command[1:3] == ["-iex", "set width unlimited"]
    assert command[3] == "-iex"
    assert command[4].startswith("source ")
    assert command[-2:] == ["-i", "dap"]


def test_rust_gdb_passes_probe_script_path_raw_to_source(monkeypatch):
    # gdb's `source` takes the rest of the line literally (no backslash
    # unescaping), so a path with spaces must NOT be escaped.
    class ResourcePath:
        def joinpath(self, _name):
            return "/tmp/tdb package/gdb_script.py"

    monkeypatch.setattr(
        "tdb.languages.rust.resources.files", lambda _package: ResourcePath()
    )

    command = RustGdbAdapter(executable="gdb").command()

    assert command[4] == "source /tmp/tdb package/gdb_script.py"


@pytest.mark.skipif(shutil.which("gdb") is None, reason="gdb is not installed")
def test_packaged_probe_registers_in_real_gdb():
    command = RustGdbAdapter(executable=shutil.which("gdb") or "gdb").command()
    completed = subprocess.run(
        command[:-2] + ["-batch", "-ex", "tdb-rust-snapshot --format json"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "TDB_RUST_JSON:" in completed.stdout


@pytest.mark.parametrize("response", [None, 42, object()])
async def test_gdb_probe_non_string_response_degrades(response):
    client = type("Client", (), {"evaluate": AsyncMock(return_value=response)})()

    result = await GdbEvidenceProbe().collect(client)

    assert result.rust_version is None
    assert result.warnings == ("invalid Rust probe response",)


def test_gdb_visible_mutex_guard_emits_confirmed_owner(monkeypatch):
    class Address:
        def __str__(self):
            return "0x1234"

    lock = SimpleNamespace(
        referenced_value=lambda: SimpleNamespace(address=Address()), address=None
    )

    class Guard:
        type = "std::sync::poison::mutex::MutexGuard<i32>"

        def __getitem__(self, name):
            assert name == "lock"
            return lock

    symbol = object()
    frame = SimpleNamespace(block=lambda: (symbol,), read_var=lambda _symbol: Guard())
    monkeypatch.setattr(gdb_script, "gdb", SimpleNamespace(error=RuntimeError))

    states = gdb_script._guard_ownership(frame, "101")

    assert states[0]["primitive_id"] == "mutex:0x1234"
    assert states[0]["owner_os_thread_ids"] == ["101"]
    assert states[0]["evidence"][0]["confidence"] == "confirmed"

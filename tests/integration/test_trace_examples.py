"""End to end: two traced example services, a driver, and ``--trace-merge``.

Every tdb runs in its own session (``tdb_proc``), so nothing the services
start outlives a test.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tdb.trace.capture import load_capture
from tests.integration.test_trace_launch import (
    _kill_group,
    calls,
    readline,
    tdb,
    tdb_proc,
)
from tests.trace_checks import assert_nested

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="sys.monitoring")

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "Python" / "trace"


def _port(proc: subprocess.Popen) -> int:
    line = readline(proc, timeout=30)
    assert line.startswith("listening on "), line
    return int(line.rsplit(" ", 1)[1])


def _wait(proc: subprocess.Popen, what: str) -> None:
    try:
        assert proc.wait(timeout=60) == 0, (what, proc.stderr.read())
    except subprocess.TimeoutExpired:
        raise AssertionError(f"{what} did not exit within 60 s") from None


def run_walkthrough(tmp_path: Path) -> Path:
    """The docs' walkthrough in tmp_path: both services traced, the driver
    sends four orders, then ``--trace-merge``.  Returns all.perfetto.json;
    inv.jsonl and ord.jsonl are next to it."""
    spec = str(EXAMPLES / "checkout.yaml")

    def traced(name, script, *args):
        return tdb_proc(
            tmp_path,
            "--trace",
            spec,
            "--trace-name",
            name,
            "--trace-out",
            name[:3],
            str(EXAMPLES / script),
            "--",
            "--port",
            "0",
            "--max-requests",
            "4",
            *args,
        )

    with traced("inventory", "inventory_service.py") as inv:
        inv_port = _port(inv)
        with traced(
            "orders", "orders_service.py", "--inventory-port", str(inv_port)
        ) as orders:
            orders_port = _port(orders)
            sent = _send_orders(tmp_path, orders_port, 4)
            assert [ln.split(": ")[1] for ln in sent] == ["OK", "OK", "NO", "NO"]
            _wait(orders, "orders")
        _wait(inv, "inventory")

    merged = tdb(
        tmp_path, "--trace-merge", "inv.jsonl", "ord.jsonl", "--trace-out", "all"
    )
    assert merged.returncode == 0, merged.stderr
    return tmp_path / "all.perfetto.json"


def test_two_services_merge(tmp_path):
    out = run_walkthrough(tmp_path)
    trace = json.loads(out.read_text(encoding="utf-8"))
    assert_nested(trace)
    process_names = [
        e["args"]["name"]
        for e in trace["traceEvents"]
        if e.get("ph") == "M" and e["name"] == "process_name"
    ]
    assert any(n.startswith("inventory · ") for n in process_names), process_names
    assert any(n.startswith("orders · ") for n in process_names), process_names

    inv_cap = load_capture(tmp_path / "inv.jsonl")
    on_db = [e for e in calls(inv_cap, "lookup") if e["thread"].startswith("db")]
    assert len(on_db) == 4
    assert len(calls(inv_cap, "handle_client")) == 4
    ord_cap = load_capture(tmp_path / "ord.jsonl")
    handled = calls(ord_cap, "handle_order")
    assert len(handled) == 4
    assert all(e["task_id"] is not None for e in handled)
    assert not (tmp_path / "inv.parts").exists()
    assert not (tmp_path / "ord.parts").exists()


def _send_orders(tmp_path: Path, port: int, count: int) -> list[str]:
    """Run the driver in its own session, with a deadline; its output lines."""
    proc = subprocess.Popen(
        [
            sys.executable,
            str(EXAMPLES / "send_orders.py"),
            "--port",
            str(port),
            "--count",
            str(count),
        ],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=60)
    finally:
        _kill_group(proc)
        proc.wait()
    assert proc.returncode == 0, err
    return out.splitlines()

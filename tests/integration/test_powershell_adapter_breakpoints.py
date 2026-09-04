"""DAP-level: line, conditional, hit-count and log breakpoints; entry cleanup."""

import pytest

from tests.integration.powershell_adapter_harness import (
    FIXTURES,
    launch_stopped,
    output_text,
    pwsh_ok,
    start_powershell_adapter,
)

pytestmark = pytest.mark.skipif(not pwsh_ok(), reason="needs pwsh + PSES")
FUNCS = str(FIXTURES / "functions.ps1")


async def _top(client):
    st = await client.request("stackTrace", {"threadId": 1, "levels": 20})
    return st["body"]["stackFrames"]


async def test_line_breakpoint_inside_function():
    client = await start_powershell_adapter()
    try:
        await launch_stopped(
            client, FUNCS, breakpoints=[{"line": 2}], stop_on_entry=False
        )
        ev = await client.wait_event("stopped")
        assert ev["body"]["reason"] == "breakpoint"
        frames = await _top(client)
        assert frames[0]["line"] == 2
        assert [f["name"] for f in frames[:3]] == ["<Breakpoint>", "Add", "Outer"]
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
        assert "sum=3" in output_text(client)
    finally:
        await client.stop()


async def test_conditional_breakpoint(tmp_path):
    p = tmp_path / "cond.ps1"
    p.write_text("foreach ($i in 1..5) {\n    Write-Host $i\n}\n")
    client = await start_powershell_adapter()
    try:
        await launch_stopped(
            client,
            str(p),
            breakpoints=[{"line": 2, "condition": "$i -eq 4"}],
            stop_on_entry=False,
        )
        await client.wait_event("stopped")
        resp = await client.request("evaluate", {"expression": "$i", "context": "repl"})
        assert resp["body"]["result"] == "4"
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
    finally:
        await client.stop()


async def test_hit_count_breakpoint(tmp_path):
    p = tmp_path / "hit.ps1"
    p.write_text("foreach ($i in 1..5) {\n    Write-Host $i\n}\n")
    client = await start_powershell_adapter()
    try:
        await launch_stopped(
            client,
            str(p),
            breakpoints=[{"line": 2, "hitCondition": "3"}],
            stop_on_entry=False,
        )
        await client.wait_event("stopped")
        resp = await client.request("evaluate", {"expression": "$i", "context": "repl"})
        assert resp["body"]["result"] == "3"
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
    finally:
        await client.stop()


async def test_logpoint_prints_without_stopping(tmp_path):
    p = tmp_path / "log.ps1"
    p.write_text("foreach ($i in 1..3) {\n    $j = $i\n}\nWrite-Host done\n")
    client = await start_powershell_adapter()
    try:
        await launch_stopped(
            client,
            str(p),
            breakpoints=[{"line": 2, "logMessage": "i is $i"}],
            stop_on_entry=False,
        )
        await client.wait_event("terminated")
        assert not [e for e in client.events if e["event"] == "stopped"]
        text = output_text(client)
        assert "i is 1" in text and "i is 3" in text and "done" in text
    finally:
        await client.stop()


async def test_set_breakpoint_while_stopped_then_hit():
    client = await start_powershell_adapter()
    try:
        await launch_stopped(client, FUNCS)
        await client.wait_event("stopped")  # entry
        await client.request(
            "setBreakpoints", {"source": {"path": FUNCS}, "breakpoints": [{"line": 6}]}
        )
        await client.request("continue", {"threadId": 1})
        ev = await client.wait_event("stopped")
        assert ev["body"]["reason"] == "breakpoint"
        assert (await _top(client))[0]["line"] == 6
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
    finally:
        await client.stop()


async def test_entry_breakpoint_is_gone_after_entry_stop(tmp_path):
    """The launcher breakpoint that produced the entry stop is cleared
    before the stepIn, so nothing extra can stop the script again -- here,
    a loop that re-runs the script's first lines."""
    p = tmp_path / "twice.ps1"
    p.write_text("$n = 0\nwhile ($n -lt 2) { $n++ }\nWrite-Host done\n")
    client = await start_powershell_adapter()
    try:
        await launch_stopped(client, str(p))
        ev = await client.wait_event("stopped")
        assert ev["body"]["reason"] == "entry"
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
        assert not [e for e in client.events if e["event"] == "stopped"]
    finally:
        await client.stop()

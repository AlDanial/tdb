"""Breakpoints (plain + conditional) and stepping through the proxy."""

import pytest

from tests.integration.ruby_adapter_harness import (
    FIXTURES,
    launch_stopped,
    rdbg_ok,
    start_ruby_adapter,
)

pytestmark = pytest.mark.skipif(not rdbg_ok(), reason="needs rdbg (debug gem >= 1.9)")

VARS = str(FIXTURES / "ruby_vars.rb")


async def test_breakpoint_hit_and_continue_to_exit():
    client = await start_ruby_adapter()
    try:
        await launch_stopped(
            client, VARS, breakpoints=[{"line": 12}], stop_on_entry=False
        )
        ev = await client.wait_event("stopped")
        assert ev["body"]["reason"] == "breakpoint"
        st = await client.request("stackTrace", {"threadId": 1})
        assert st["body"]["stackFrames"][0]["line"] == 12
        await client.request("continue", {"threadId": 1})
        await client.wait_event("stopped")  # second loop iteration
        await client.request(
            "setBreakpoints", {"source": {"path": VARS}, "breakpoints": []}
        )
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
    finally:
        await client.stop()


async def test_conditional_breakpoint():
    client = await start_ruby_adapter()
    try:
        await launch_stopped(
            client,
            VARS,
            breakpoints=[{"line": 12, "condition": "i == 3"}],
            stop_on_entry=False,
        )
        await client.wait_event("stopped")
        resp = await client.request("evaluate", {"expression": "i", "context": "repl"})
        assert resp["body"]["result"].strip() == "3"
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
    finally:
        await client.stop()


async def test_step_in_and_out():
    client = await start_ruby_adapter()
    try:
        await launch_stopped(
            client, VARS, breakpoints=[{"line": 12}], stop_on_entry=False
        )
        await client.wait_event("stopped")
        await client.request("stepIn", {"threadId": 1})
        ev = await client.wait_event("stopped")
        assert ev["body"]["reason"] in ("step", "pause")
        st = await client.request("stackTrace", {"threadId": 1})
        names = [f["name"] for f in st["body"]["stackFrames"]]
        assert any("outer" in n for n in names)
        await client.request("stepOut", {"threadId": 1})
        await client.wait_event("stopped")
        await client.request("continue", {"threadId": 1})
        # remaining breakpoint hits: clear and run out
        await client.wait_event("stopped")
        await client.request(
            "setBreakpoints", {"source": {"path": VARS}, "breakpoints": []}
        )
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
    finally:
        await client.stop()

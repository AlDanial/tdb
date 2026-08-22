"""Stack/scopes/variables/evaluate/completions through the proxy."""

import pytest

from tests.integration.ruby_adapter_harness import (
    FIXTURES,
    launch_stopped,
    rdbg_ok,
    start_ruby_adapter,
)

pytestmark = pytest.mark.skipif(not rdbg_ok(), reason="needs rdbg (debug gem >= 1.9)")

VARS = str(FIXTURES / "ruby_vars.rb")


async def _stop_at(client, line):
    await launch_stopped(
        client, VARS, breakpoints=[{"line": line}], stop_on_entry=False
    )
    return await client.wait_event("stopped")


async def test_scopes_and_variables():
    client = await start_ruby_adapter()
    try:
        await _stop_at(client, 3)  # inside inner(); m is defined
        st = await client.request("stackTrace", {"threadId": 1})
        frame_id = st["body"]["stackFrames"][0]["id"]
        scopes = await client.request("scopes", {"frameId": frame_id})
        assert scopes["success"]
        ref = scopes["body"]["scopes"][0]["variablesReference"]
        vs = await client.request("variables", {"variablesReference": ref})
        names = {v["name"] for v in vs["body"]["variables"]}
        assert "m" in names or "%self" in names  # rdbg lists %self too
        await client.request(
            "setBreakpoints", {"source": {"path": VARS}, "breakpoints": []}
        )
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
    finally:
        await client.stop()


async def test_evaluate_in_frame():
    client = await start_ruby_adapter()
    try:
        await _stop_at(client, 3)
        st = await client.request("stackTrace", {"threadId": 1})
        frame_id = st["body"]["stackFrames"][0]["id"]
        resp = await client.request(
            "evaluate",
            {"expression": "m + 40", "frameId": frame_id, "context": "repl"},
        )
        assert resp["success"]
        assert resp["body"]["result"].strip() == "42"  # m == 2 at first hit
        await client.request(
            "setBreakpoints", {"source": {"path": VARS}, "breakpoints": []}
        )
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
    finally:
        await client.stop()


async def test_completions():
    client = await start_ruby_adapter()
    try:
        await _stop_at(client, 3)
        st = await client.request("stackTrace", {"threadId": 1})
        frame_id = st["body"]["stackFrames"][0]["id"]
        resp = await client.request(
            "completions",
            {"text": "tot", "column": 4, "frameId": frame_id},
        )
        assert resp["success"]
        await client.request(
            "setBreakpoints", {"source": {"path": VARS}, "breakpoints": []}
        )
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
    finally:
        await client.stop()

"""DAP-level: scopes/variables, evaluate, setVariable, threads."""

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


async def _stopped_in_add(client):
    await launch_stopped(client, FUNCS, breakpoints=[{"line": 3}], stop_on_entry=False)
    await client.wait_event("stopped")


async def test_scopes_and_locals():
    client = await start_powershell_adapter()
    try:
        await _stopped_in_add(client)
        sc = await client.request("scopes", {"frameId": 0})
        names = [s["name"] for s in sc["body"]["scopes"]]
        assert "Local" in names and "Script" in names and "Global" in names
        local = next(s for s in sc["body"]["scopes"] if s["name"] == "Local")
        vs = await client.request(
            "variables", {"variablesReference": local["variablesReference"]}
        )
        byname = {v["name"]: v["value"] for v in vs["body"]["variables"]}
        assert byname["$a"] == "1" and byname["$b"] == "2" and byname["$s"] == "3"
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
    finally:
        await client.stop()


async def test_evaluate_returns_values_in_repl_context():
    client = await start_powershell_adapter()
    try:
        await _stopped_in_add(client)
        resp = await client.request(
            "evaluate", {"expression": "$s * 10", "context": "repl"}
        )
        assert resp["body"]["result"] == "30"
        assert "30" not in output_text(client)  # not printed to the console
        resp = await client.request(
            "evaluate", {"expression": "$nope.Foo()", "context": "repl"}
        )
        assert resp["success"]  # PSES reports failures as empty results
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
    finally:
        await client.stop()


async def test_set_variable_changes_result():
    client = await start_powershell_adapter()
    try:
        await _stopped_in_add(client)
        sc = await client.request("scopes", {"frameId": 0})
        local = next(s for s in sc["body"]["scopes"] if s["name"] == "Local")
        resp = await client.request(
            "setVariable",
            {
                "variablesReference": local["variablesReference"],
                "name": "$s",
                "value": "40",
            },
        )
        assert resp["success"]
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
        assert "sum=40" in output_text(client)
    finally:
        await client.stop()


async def test_threads():
    client = await start_powershell_adapter()
    try:
        await _stopped_in_add(client)
        th = await client.request("threads")
        assert len(th["body"]["threads"]) == 1
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
    finally:
        await client.stop()

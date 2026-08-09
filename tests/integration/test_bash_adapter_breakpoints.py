"""DAP-level: setBreakpoints (config-phase + re-set while stopped), conditions."""

import asyncio

import pytest

from tests.integration.bash_adapter_harness import (
    FIXTURES,
    bash_ok,
    launch_stopped,
    start_bash_adapter,
)

pytestmark = pytest.mark.skipif(not bash_ok(), reason="needs bash >= 4.4")


@pytest.mark.asyncio
async def test_breakpoint_from_config_phase():
    client = await start_bash_adapter()
    try:
        program = str(FIXTURES / "bash_loop.sh")
        await launch_stopped(
            client, program, breakpoints=[{"line": 5}], stop_on_entry=False
        )
        ev = await client.wait_event("stopped")
        assert ev["body"]["reason"] == "breakpoint"
        frames = (await client.request("stackTrace", {"threadId": 1}))["body"][
            "stackFrames"
        ]
        assert frames[0]["line"] == 5
        await client.request("continue", {"threadId": 1})
        await client.wait_event("exited")
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_conditional_breakpoint_via_dap():
    client = await start_bash_adapter()
    try:
        program = str(FIXTURES / "bash_loop.sh")
        await launch_stopped(
            client,
            program,
            breakpoints=[{"line": 3, "condition": "(( i == 4 ))"}],
            stop_on_entry=False,
        )
        await client.wait_event("stopped")
        result = (
            await client.request(
                "evaluate",
                {
                    "expression": 'echo "i=$i"',
                    "context": "repl",
                },
            )
        )["body"]["result"]
        assert "i=4" in result
        await client.request("continue", {"threadId": 1})
        await client.wait_event("exited")
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_setbreakpoints_while_running_stops_at_new_line():
    """Finding 6: DAP-level mirror of test_live_breakpoint_add_while_running
    (session level) -- the running-state setBreakpoints branch in
    _on_setBreakpoints (session.set_breakpoint_nowait, the drain fast
    path) must actually produce a stop, not just be accepted."""
    client = await start_bash_adapter()
    try:
        program = str(FIXTURES / "bash_loop.sh")
        await launch_stopped(client, program, breakpoints=None, stop_on_entry=False)
        await asyncio.sleep(0.3)  # running, inside the slow loop, no bps yet
        await client.request(
            "setBreakpoints",
            {"source": {"path": program}, "breakpoints": [{"line": 11}]},
        )
        ev = await client.wait_event("stopped")
        assert ev["body"]["reason"] == "breakpoint"
        frames = (await client.request("stackTrace", {"threadId": 1}))["body"][
            "stackFrames"
        ]
        assert frames[0]["line"] == 11
        await client.request("continue", {"threadId": 1})
        await client.wait_event("exited")
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_reset_breakpoints_while_stopped():
    client = await start_bash_adapter()
    try:
        program = str(FIXTURES / "bash_loop.sh")
        await launch_stopped(
            client, program, breakpoints=[{"line": 3}], stop_on_entry=False
        )
        await client.wait_event("stopped")
        # replace with an empty set -> free run to exit
        await client.request(
            "setBreakpoints", {"source": {"path": program}, "breakpoints": []}
        )
        await client.request("continue", {"threadId": 1})
        await client.wait_event("exited")
    finally:
        await client.stop()

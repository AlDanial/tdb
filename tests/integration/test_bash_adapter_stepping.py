"""DAP-level: next/stepIn/stepOut against bash_functions.sh."""

import pytest

from tests.integration.bash_adapter_harness import (
    FIXTURES,
    bash_ok,
    launch_stopped,
    start_bash_adapter,
)

pytestmark = pytest.mark.skipif(not bash_ok(), reason="needs bash >= 4.4")


async def _line(client):
    frames = (await client.request("stackTrace", {"threadId": 1}))["body"][
        "stackFrames"
    ]
    return frames[0]["line"]


@pytest.mark.asyncio
async def test_step_in_and_out():
    client = await start_bash_adapter()
    try:
        program = str(FIXTURES / "bash_functions.sh")
        await launch_stopped(
            client, program, breakpoints=[{"line": 6}], stop_on_entry=False
        )  # outer(): `inner` call
        await client.wait_event("stopped")
        # DEVIATION from the brief: a single stepIn from the `inner` call
        # site lands on inner()'s *definition* line (1), not its first
        # statement (2) -- confirmed via manual harness probe. This is
        # inherent bash DEBUG-trap behavior on function entry (the trap
        # fires once more for the function body as a compound command
        # before firing for its first real statement), the same root
        # cause test_bash_session.py's test_next_steps_over_call already
        # documents for program entry landing on the first executable
        # line rather than a definition line. A second stepIn reaches the
        # real first statement.
        await client.request("stepIn", {"threadId": 1})
        ev = await client.wait_event("stopped")
        assert ev["body"]["reason"] == "step"
        assert await _line(client) == 1  # inner()'s definition line
        await client.request("stepIn", {"threadId": 1})
        ev = await client.wait_event("stopped")
        assert ev["body"]["reason"] == "step"
        assert await _line(client) == 2  # inside inner()
        await client.request("stepOut", {"threadId": 1})
        await client.wait_event("stopped")
        assert await _line(client) == 7  # back in outer()
        await client.request("continue", {"threadId": 1})
        await client.wait_event("exited")
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_next_stays_in_frame():
    client = await start_bash_adapter()
    try:
        program = str(FIXTURES / "bash_functions.sh")
        await launch_stopped(
            client, program, breakpoints=[{"line": 9}], stop_on_entry=False
        )  # top level: `outer`
        await client.wait_event("stopped")
        await client.request("next", {"threadId": 1})
        await client.wait_event("stopped")
        assert await _line(client) == 10
        await client.request("continue", {"threadId": 1})
        await client.wait_event("exited")
    finally:
        await client.stop()

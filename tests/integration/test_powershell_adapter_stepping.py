"""DAP-level: next / stepIn / stepOut and the launcher frame's position."""

import pytest

from tests.integration.powershell_adapter_harness import (
    FIXTURES,
    launch_stopped,
    pwsh_ok,
    start_powershell_adapter,
)

pytestmark = pytest.mark.skipif(not pwsh_ok(), reason="needs pwsh + PSES")
FUNCS = str(FIXTURES / "functions.ps1")


async def _line_and_names(client):
    st = await client.request("stackTrace", {"threadId": 1, "levels": 20})
    frames = st["body"]["stackFrames"]
    return frames[0]["line"], [f["name"] for f in frames]


async def _step(client, cmd):
    await client.request(cmd, {"threadId": 1})
    ev = await client.wait_event("stopped")
    assert ev["body"]["reason"] == "step"


async def test_next_stepin_stepout():
    client = await start_powershell_adapter()
    try:
        await launch_stopped(client, FUNCS)
        await client.wait_event("stopped")
        line, _ = await _line_and_names(client)
        assert line == 9  # $x = 1
        await _step(client, "next")
        assert (await _line_and_names(client))[0] == 10
        await _step(client, "next")
        assert (await _line_and_names(client))[0] == 11  # $y = Outer $x
        await _step(client, "stepIn")
        line, names = await _line_and_names(client)
        assert line == 6 and names[1] == "Outer"
        await _step(client, "stepIn")
        line, names = await _line_and_names(client)
        assert line == 2 and names[1:3] == ["Add", "Outer"]
        await _step(client, "stepOut")
        line, names = await _line_and_names(client)
        assert names[1] == "Outer"
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
    finally:
        await client.stop()


async def test_launcher_frame_is_at_the_bottom():
    client = await start_powershell_adapter()
    try:
        await launch_stopped(
            client, FUNCS, breakpoints=[{"line": 2}], stop_on_entry=False
        )
        await client.wait_event("stopped")
        st = await client.request("stackTrace", {"threadId": 1, "levels": 20})
        frames = st["body"]["stackFrames"]
        # PSES appends a source-less "Interactive Session" frame below the
        # launcher, so the launcher is the bottom-most frame *with* a source.
        sourced = [f for f in frames if f.get("source")]
        assert sourced[-1]["source"]["path"].endswith("tdb_launch.ps1")
        assert frames[0]["source"]["path"] == FUNCS
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
    finally:
        await client.stop()

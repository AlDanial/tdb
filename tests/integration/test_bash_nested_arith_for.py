"""bash reports an arithmetic `for` loop that contains another arithmetic
`for` at the INNER loop's line (one global arith_for_lineno in parse.y).
The adapter's fixup table (arith_for.py + the harness's `fixup` command)
puts those stops back on the outer header's line."""

import asyncio

import pytest

from tests.integration.bash_adapter_harness import (
    FIXTURES,
    bash_ok,
    launch_stopped,
    start_bash_adapter,
)
from tests.integration.test_bash_session import Recorder, _launch

pytestmark = pytest.mark.skipif(not bash_ok(), reason="needs bash >= 4.4")

OUTER, INNER = 3, 5  # header lines in bash_nested_arith_for.sh


@pytest.mark.asyncio
async def test_breakpoint_on_outer_header_hits():
    rec = Recorder()
    fixture = FIXTURES / "bash_nested_arith_for.sh"
    session = await _launch(fixture, rec)
    await session.set_breakpoint(str(fixture), OUTER)
    session.resume("continue")
    reason, _, line = await rec.wait_stop()
    assert (reason, line) == ("breakpoint", OUTER)  # ((o=0))
    rc, out = await session.evaluate("echo $BASH_COMMAND")
    assert out.strip() == "((o=0))"
    # every outer-loop evaluation stops there: o=0, then (o<2, o++) x2, o<2
    while True:
        session.resume("continue")
        done, _ = await asyncio.wait(
            [
                asyncio.ensure_future(rec.stop_event.wait()),
                asyncio.ensure_future(rec.exit_event.wait()),
            ],
            timeout=10,
            return_when=asyncio.FIRST_COMPLETED,
        )
        assert done, "neither stopped nor exited"
        if rec.exit_event.is_set():
            break
        rec.stop_event.clear()
    assert [(r, ln) for r, _, ln in rec.stops] == [("breakpoint", OUTER)] * 6
    assert "total=42" in rec.stdout()
    await session.stop()


@pytest.mark.asyncio
async def test_breakpoint_on_inner_header_skips_outer_loop_commands():
    rec = Recorder()
    fixture = FIXTURES / "bash_nested_arith_for.sh"
    session = await _launch(fixture, rec)
    await session.set_breakpoint(str(fixture), INNER)
    session.resume("continue")
    reason, _, line = await rec.wait_stop()
    assert (reason, line) == ("breakpoint", INNER)
    rc, out = await session.evaluate("echo $BASH_COMMAND")
    assert out.strip() == "((i=0))"  # not the outer loop's ((o=0))/((o<2))
    session.resume("continue")
    await session.stop()


@pytest.mark.asyncio
async def test_stepping_reports_outer_header_line():
    rec = Recorder()
    fixture = FIXTURES / "bash_nested_arith_for.sh"
    session = await _launch(fixture, rec)
    session.resume("step")
    _, _, line = await rec.wait_stop()
    assert line == 2  # total=0
    lines = []
    for _ in range(4):
        session.resume("next")
        _, _, line = await rec.wait_stop()
        lines.append(line)
    # ((o=0)) ((o<2)) total=... ((i=0))
    assert lines == [OUTER, OUTER, 4, INNER]
    await session.stop()


@pytest.mark.asyncio
async def test_sourced_file_gets_fixups_on_first_stop():
    """No breakpoint was ever set in the sourced file, so its fixups are
    only pushed when the harness first stops inside it."""
    rec = Recorder()
    main = FIXTURES / "bash_nested_arith_for_main.sh"
    session = await _launch(main, rec)
    session.resume("step")
    _, path, line = await rec.wait_stop()
    assert path.endswith("_main.sh") and line == 2
    session.resume("step")
    _, path, line = await rec.wait_stop()
    assert path.endswith("bash_nested_arith_for.sh") and line == 2
    session.resume("step")
    _, _, line = await rec.wait_stop()
    assert line == OUTER  # fixups arrived before the mis-reported stop
    await session.stop()


@pytest.mark.asyncio
async def test_dap_breakpoint_on_outer_header():
    client = await start_bash_adapter()
    try:
        program = str(FIXTURES / "bash_nested_arith_for.sh")
        await launch_stopped(
            client, program, breakpoints=[{"line": OUTER}], stop_on_entry=False
        )
        ev = await client.wait_event("stopped")
        assert ev["body"]["reason"] == "breakpoint"
        frames = (await client.request("stackTrace", {"threadId": 1}))["body"][
            "stackFrames"
        ]
        assert frames[0]["line"] == OUTER
    finally:
        await client.stop()

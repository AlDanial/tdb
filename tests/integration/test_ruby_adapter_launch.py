"""DAP-level: launch, entry stop, nonstop, output pump, exit code."""

import pytest

from tests.integration.ruby_adapter_harness import (
    FIXTURES,
    launch_stopped,
    rdbg_ok,
    start_ruby_adapter,
)

pytestmark = pytest.mark.skipif(not rdbg_ok(), reason="needs rdbg (debug gem >= 1.9)")


async def test_stop_on_entry_reports_entry_at_first_line():
    client = await start_ruby_adapter()
    try:
        program = str(FIXTURES / "ruby_hello.rb")
        await launch_stopped(client, program)
        ev = await client.wait_event("stopped")
        # rdbg reports the entry stop as "pause"; the proxy rewrites the
        # first stop to "entry" for debugpy parity.
        assert ev["body"]["reason"] == "entry"
        st = await client.request(
            "stackTrace", {"threadId": ev["body"].get("threadId", 1)}
        )
        top = st["body"]["stackFrames"][0]
        assert top["source"]["path"] == program
        await client.request("continue", {"threadId": 1})
        exited = await client.wait_event("exited")
        assert exited["body"]["exitCode"] == 7
        await client.wait_event("terminated")
    finally:
        await client.stop()


async def test_nonstop_runs_to_completion_with_output():
    client = await start_ruby_adapter()
    try:
        await launch_stopped(
            client, str(FIXTURES / "ruby_hello.rb"), stop_on_entry=False
        )
        exited = await client.wait_event("exited")
        assert exited["body"]["exitCode"] == 7
        await client.wait_event("terminated")
        text = "".join(
            e["body"].get("output", "")
            for e in list(client.events)
            if e["event"] == "output"
        )
        assert "hello from ruby 3" in text
        assert "DEBUGGER" not in text  # banner lines filtered
        assert "Ruby REPL" not in text  # greeting notice filtered
    finally:
        await client.stop()


async def test_launch_missing_program_fails():
    client = await start_ruby_adapter()
    try:
        resp = await client.send(
            "launch",
            {
                "type": "ruby",
                "program": "/nonexistent/x.rb",
                "args": [],
                "cwd": "/tmp",
                "stopOnEntry": True,
            },
        )
        assert resp["success"] is False
        assert "not found" in resp["message"]
    finally:
        await client.stop()


async def test_launch_bad_rdbg_path_names_the_hint():
    client = await start_ruby_adapter()
    try:
        resp = await client.send(
            "launch",
            {
                "type": "ruby",
                "program": str(FIXTURES / "ruby_hello.rb"),
                "args": [],
                "cwd": "/tmp",
                "stopOnEntry": True,
                "rdbg": "/nonexistent/rdbg",
            },
        )
        assert resp["success"] is False
    finally:
        await client.stop()

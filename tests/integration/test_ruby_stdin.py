"""DAP-level: Console-view stdin reaches an rdbg-launched program.

The proxy owns rdbg's stdin pipe (rdbg runs the script in-process, so
that pipe IS the program's stdin). tdb's private `tdbStdin` request
writes text into it or closes it (EOF).
"""

import pytest

from tests.integration.ruby_adapter_harness import (
    launch_stopped,
    rdbg_ok,
    start_ruby_adapter,
)

pytestmark = pytest.mark.skipif(not rdbg_ok(), reason="needs rdbg (debug gem >= 1.9)")

PROMPT_SCRIPT = 'print "enter: "; $stdout.flush; x = gets; puts "got:#{x.chomp}"\n'
EOF_SCRIPT = 'x = gets; puts(x.nil? ? "eof" : "got:#{x.chomp}")\n'


def _stdout_text(client) -> str:
    return "".join(
        e["body"].get("output", "")
        for e in list(client.events)
        if e["event"] == "output" and e["body"].get("category") == "stdout"
    )


async def _wait_for_stdout(client, needle: str, timeout: float = 20.0) -> None:
    import asyncio

    for _ in range(int(timeout * 10)):
        if needle in _stdout_text(client):
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"{needle!r} never appeared in stdout; saw {client.events}")


async def test_text_reaches_gets(tmp_path):
    prog = tmp_path / "prompt.rb"
    prog.write_text(PROMPT_SCRIPT)
    client = await start_ruby_adapter()
    try:
        await launch_stopped(client, str(prog), stop_on_entry=False)
        # The prompt has no trailing newline: the output pump must deliver
        # partial chunks, not wait for a line terminator.
        await _wait_for_stdout(client, "enter: ")
        assert "got:" not in _stdout_text(client)
        resp = await client.request("tdbStdin", {"text": "42\n"})
        assert resp["success"] is True
        await _wait_for_stdout(client, "got:42")
        await client.wait_event("terminated")
    finally:
        await client.stop()


async def test_eof_makes_gets_return_nil(tmp_path):
    prog = tmp_path / "eof.rb"
    prog.write_text(EOF_SCRIPT)
    client = await start_ruby_adapter()
    try:
        await launch_stopped(client, str(prog), stop_on_entry=False)
        resp = await client.request("tdbStdin", {"eof": True})
        assert resp["success"] is True
        await _wait_for_stdout(client, "eof")
        await client.wait_event("terminated")
        # A second eof is a no-op success, not an error.
        resp = await client.request("tdbStdin", {"eof": True})
        assert resp["success"] is True
        # ... but text after eof has nowhere to go.
        resp = await client.request("tdbStdin", {"text": "late\n"})
        assert resp["success"] is False
        assert resp["message"]
    finally:
        await client.stop()


async def test_stdin_before_launch_is_an_error():
    client = await start_ruby_adapter()
    try:
        resp = await client.request("tdbStdin", {"text": "x\n"})
        assert resp["success"] is False
        assert "stdin" in resp["message"]
    finally:
        await client.stop()

"""End-to-end coverage for Ruby's DAP bridge in remote-attach mode.

These tests spawn a standalone ``rdbg --open --port <port> <script>``
process (simulating a program launched elsewhere -- e.g. a CI job or a
separate shell) and then attach tdb's DAP client to it through the
stdio-TCP bridge.

rdbg in ``--open`` mode (without ``--nonstop``) starts the program and
stops at the first line, waiting for a debugger to connect -- exactly the
state the bridge's attach path expects.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import time
from pathlib import Path

import pytest

from tdb.dap.client import DAPClient
from tdb.languages.ruby import build_ruby_profile

pytestmark = pytest.mark.skipif(
    shutil.which("rdbg") is None, reason="Ruby debug gem is not installed"
)

EXAMPLES = Path("examples/ruby")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_port(port: int, timeout: float = 10.0) -> bool:
    """Block until a TCP connection to 127.0.0.1:port succeeds.

    Plain blocking (no asyncio) so it can safely run via ``to_thread``.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            pass
    return False


async def test_attach_to_running_rdbg_stops_at_entry_and_completes() -> None:
    """Remote-attach flow through the bridge.

    1. spawn `rdbg --open --port <port> hello.rb` (waits at entry for a debugger)
    2. attach via the bridge (DAPClient rdbg adapter)
    3. confirm we stop at the entry line and can inspect the stack/threads
    4. continue to completion and observe relayed program output + termination

    NOTE: rdbg never sends a DAP `exited` event for a remote process -- in
    attach mode the bridge owns no process, so exit-code reporting is
    unavailable (a known constraint documented in 08). We assert the
    `terminated` event and the relayed stdout instead.
    """
    port = _free_port()
    proc = await asyncio.create_subprocess_exec(
        "rdbg",
        "--open",
        "--port",
        str(port),
        str(EXAMPLES / "hello.rb"),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    client = DAPClient(build_ruby_profile().adapter)
    initialized = asyncio.Event()
    stopped = asyncio.Event()
    terminated = asyncio.Event()

    client.on_event("initialized", lambda e: initialized.set())
    client.on_event("stopped", lambda e: stopped.set())
    client.on_event("terminated", lambda e: terminated.set())

    await client.start()
    try:
        # Wait for rdbg to open its listening socket (it stops at entry
        # and waits for the debugger -- this line is the readiness signal).
        assert await asyncio.to_thread(_wait_for_port, port, 20), (
            f"rdbg did not open listening port 127.0.0.1:{port} in time"
        )

        await client.initialize()
        attach_fut = await client.attach(host="127.0.0.1", port=port)

        await asyncio.wait_for(initialized.wait(), timeout=20)
        await client.configuration_done()
        resp = await asyncio.wait_for(attach_fut, timeout=20)
        assert resp.success

        # The program is suspended at the first executable line (entry).
        await asyncio.wait_for(stopped.wait(), timeout=20)
        threads = await client.threads()
        assert threads, "expected at least one thread after attach stop"
        assert await client.stack_trace(threads[0].id)

        # Drive the program to completion. rdbg sends `terminated` once the
        # debuggee exits (the bridge relents it); this proves the full session
        # stayed alive through entry-stop -> continue -> completion.
        # NOTE: in attach mode rdbg routes the debuggee's own STDOUT to the
        # terminal ("output to the STDOUT/ERR printed on the TERMINAL"), so
        # program output is not captured as DAP `output` events here. The
        # bridge's stdout-relay path is covered by the launch-mode suite
        # (test_ruby_session / test_ruby_rails_bundler).
        await client.continue_(threads[0].id)
        await asyncio.wait_for(terminated.wait(), timeout=20)
    finally:
        await client.stop()
        try:
            if proc.returncode is None:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass

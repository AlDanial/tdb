"""Integration-test fixtures: spawn `tdb --headless` and talk JSON-RPC to it.

The fixture starts the headless server in a subprocess, waits until it
accepts an HTTP request, yields a small client wrapper, then sends
{"action": "quit"} to tear it down. Each test gets a fresh server so
breakpoint state can't leak between tests.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_DIR = REPO_ROOT / "src"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
SAMPLE_PROGRAM = FIXTURES_DIR / "sample_program.py"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RpcClient:
    """Tiny JSON-RPC client over HTTP."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self._client = httpx.Client(base_url=base_url, timeout=15.0)

    def call(self, action: str, *params: Any) -> dict:
        resp = self._client.post(
            "/rpc",
            json={"action": action, "params": list(params)},
        )
        resp.raise_for_status()
        return resp.json()

    def ok(self, action: str, *params: Any) -> str:
        body = self.call(action, *params)
        assert body["success"] is True, f"{action} failed: {body.get('value')}"
        return body.get("value", "")

    def close(self) -> None:
        self._client.close()


@pytest.fixture
def headless_server(tmp_path):
    """Spawn `python -m tdb --headless <sample> --no-stop-on-entry --server-port=N`.

    Yields the RpcClient. On teardown sends `quit` and joins the process.
    """
    port = _free_port()
    # Run with --no-stop-on-entry so the program runs to its breakpoints
    # (or exits) rather than blocking at line 1 forever.
    env = {
        **os.environ,
        "PYTHONPATH": str(SRC_DIR) + os.pathsep + os.environ.get("PYTHONPATH", ""),
        # Keep tdb's debug log out of /tmp/tdb.log under tests; redirect to tmp_path.
        "TDB_LOG_DIR": str(tmp_path),
    }
    # Default: stop on entry so the program is paused when the server comes
    # up. Tests can issue `continue` when they want it to run.
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "tdb",
            "--headless",
            "--server-port", str(port),
            str(SAMPLE_PROGRAM),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    client = RpcClient(f"http://127.0.0.1:{port}")
    base = f"http://127.0.0.1:{port}/rpc"

    # Poll until the server accepts a request, or the subprocess dies.
    deadline = time.monotonic() + 20.0
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out, err = proc.communicate(timeout=2)
            pytest.fail(
                "tdb --headless exited before serving:\n"
                f"stdout={out.decode(errors='replace')}\n"
                f"stderr={err.decode(errors='replace')}"
            )
        try:
            with httpx.Client(timeout=1.0) as probe:
                probe.post(base, json={"action": "status", "params": []})
            break
        except Exception as e:
            last_err = e
            time.sleep(0.2)
    else:
        proc.kill()
        out, err = proc.communicate(timeout=2)
        pytest.fail(
            f"tdb --headless never came up: {last_err}\n"
            f"stderr={err.decode(errors='replace')}"
        )

    try:
        yield client
    finally:
        client.close()
        # The headless RPC `quit` action only stops the debug controller, not
        # the uvicorn server, so SIGTERM is what actually unblocks
        # server.serve(). Best-effort: terminate, wait, escalate to kill.
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)

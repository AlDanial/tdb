"""Disconnect/terminate must kill the rdbg process tree — no orphans."""

import asyncio
import os

import pytest

from tdb.adapters.tcsh.guardian import _process_is_gone
from tests.integration.ruby_adapter_harness import (
    FIXTURES,
    launch_stopped,
    rdbg_ok,
    start_ruby_adapter,
)

pytestmark = pytest.mark.skipif(not rdbg_ok(), reason="needs rdbg (debug gem >= 1.9)")

SLEEPER = str(FIXTURES / "ruby_sleep.rb")


async def _rdbg_pids_of(proxy_pid: int) -> list[int]:
    """Direct children of the proxy (the rdbg process).

    /proc first (works on BusyBox/Alpine CI, where `ps` lacks --ppid),
    procps `ps --ppid` as the fallback for /proc-less platforms.
    """
    try:
        text = "".join(
            open(f"/proc/{proxy_pid}/task/{t}/children").read()
            for t in os.listdir(f"/proc/{proxy_pid}/task")
        )
        pids = [int(p) for p in text.split()]
        if pids:
            return pids
    except OSError:
        pass
    proc = await asyncio.create_subprocess_exec(
        "ps",
        "-o",
        "pid=",
        "--ppid",
        str(proxy_pid),
        stdout=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    return [int(p) for p in out.split()]


@pytest.mark.skipif(os.name == "nt", reason="ps/kill-based checks")
async def test_disconnect_kills_rdbg():
    client = await start_ruby_adapter()
    try:
        await launch_stopped(client, SLEEPER, stop_on_entry=False)
        await asyncio.sleep(0.3)
        children = await _rdbg_pids_of(client.proc.pid)
        assert children, "expected a live rdbg child"
        resp = await client.request("disconnect", {})
        assert resp["success"]
        await asyncio.sleep(0.5)
        for pid in children:
            assert _process_is_gone(pid), f"rdbg {pid} survived disconnect"
    finally:
        await client.stop()


@pytest.mark.skipif(os.name == "nt", reason="ps/kill-based checks")
async def test_terminate_kills_rdbg_and_reports_termination():
    client = await start_ruby_adapter()
    try:
        await launch_stopped(client, SLEEPER, stop_on_entry=False)
        await asyncio.sleep(0.3)
        children = await _rdbg_pids_of(client.proc.pid)
        assert children, "expected a live rdbg child"
        resp = await client.request("terminate", {})
        assert resp["success"]
        await client.wait_event("terminated")
        await asyncio.sleep(0.5)
        for pid in children:
            assert _process_is_gone(pid), f"rdbg {pid} survived terminate"
    finally:
        await client.stop()


@pytest.mark.skipif(os.name == "nt", reason="ps/kill-based checks")
async def test_proxy_death_kills_rdbg():
    """The run() finally-block must reap rdbg when tdb kills the proxy."""
    client = await start_ruby_adapter()
    try:
        await launch_stopped(client, SLEEPER, stop_on_entry=False)
        await asyncio.sleep(0.3)
        children = await _rdbg_pids_of(client.proc.pid)
        assert children, "expected a live rdbg child"
        client.proc.stdin.close()  # EOF -> run() exits -> finally kills group
        await asyncio.sleep(1.5)
        for pid in children:
            assert _process_is_gone(pid), f"rdbg {pid} survived proxy stdin EOF"
    finally:
        await client.stop()

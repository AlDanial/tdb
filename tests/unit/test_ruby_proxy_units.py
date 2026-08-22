"""Pure-logic tests for the ruby proxy: seq translation, transport
selection, and the launch-handshake retry machinery. No Ruby/rdbg
required -- rdbg itself is faked out with plain asyncio primitives."""

import asyncio
import contextlib
import json
import os

import pytest

from tdb.adapters.ruby.server import (
    CAPABILITIES,
    MIN_DEBUG_GEM,
    RubyDapServer,
    SeqTranslator,
    _free_port,
    pick_transport,
)


def test_client_request_roundtrip():
    t = SeqTranslator()
    fwd = t.client_request_to_rdbg({"seq": 41, "type": "request", "command": "next"})
    assert fwd["seq"] == 1 and fwd["command"] == "next"
    resp = t.rdbg_response_to_client(
        {
            "seq": 9,
            "type": "response",
            "request_seq": fwd["seq"],
            "command": "next",
            "success": True,
        }
    )
    assert resp["request_seq"] == 41
    assert resp["seq"] == 1  # first message the proxy sends to the client


def test_proxy_originated_response_is_swallowed():
    t = SeqTranslator()
    # a response to a request the proxy sent itself (no client mapping)
    assert (
        t.rdbg_response_to_client(
            {
                "seq": 1,
                "type": "response",
                "request_seq": 999,
                "command": "initialize",
                "success": True,
            }
        )
        is None
    )


def test_events_are_resequenced_monotonically():
    t = SeqTranslator()
    e1 = t.rdbg_event_to_client({"seq": 50, "type": "event", "event": "output"})
    e2 = t.rdbg_event_to_client({"seq": 51, "type": "event", "event": "stopped"})
    assert (e1["seq"], e2["seq"]) == (1, 2)


def test_reverse_request_roundtrip():
    t = SeqTranslator()
    fwd = t.rdbg_request_to_client(
        {"seq": 7, "type": "request", "command": "runInTerminal"}
    )
    back = t.client_response_to_rdbg(
        {
            "seq": 3,
            "type": "response",
            "request_seq": fwd["seq"],
            "command": "runInTerminal",
            "success": True,
        }
    )
    assert back["request_seq"] == 7


def test_client_response_without_mapping_is_swallowed():
    t = SeqTranslator()
    assert (
        t.client_response_to_rdbg(
            {
                "seq": 3,
                "type": "response",
                "request_seq": 123,
                "command": "x",
                "success": True,
            }
        )
        is None
    )


def test_capabilities_omit_step_back():
    # rdbg advertises supportsStepBack; tdb has no step-back UI, so the
    # proxy's static capability dict must not re-advertise it.
    assert "supportsStepBack" not in CAPABILITIES
    assert CAPABILITIES["supportsConfigurationDoneRequest"] is True
    assert CAPABILITIES["supportsConditionalBreakpoints"] is True
    assert CAPABILITIES["supportsCompletionsRequest"] is True


def test_free_port_is_bindable():
    import socket

    port = _free_port()
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))  # racy in theory; fine as a smoke test


@pytest.mark.skipif(os.name == "nt", reason="unix-socket branch")
def test_pick_transport_prefers_unix_socket():
    tr = pick_transport()
    try:
        assert tr.rdbg_args[0] in ("--sock-path", "--port")
        if tr.rdbg_args[0] == "--sock-path":
            assert len(tr.rdbg_args[1]) < 90
    finally:
        tr.cleanup()


def test_min_debug_gem():
    assert MIN_DEBUG_GEM == (1, 9)


# ---- launch-handshake retry machinery (task F: rdbg handshake stall) ----
#
# rdbg (debug gem 1.11) has a startup race in UI_ServerBase (server.rb):
# `activate` sets `@sock` *inside* the `@accept_m.synchronize` block
# *before* calling `greeting`, which is what decides DAP-vs-REPL mode; but
# `UI_ServerBase#sock` opens with `if s = @sock` -- no mutex -- so once
# `@sock` is set, ANY caller (including the debuggee's SESSION thread
# doing its first `readline`) gets the fast path without waiting for
# `greeting` to finish. If that happens while `@repl` is still its
# default `true`, `readline` writes a raw REPL-protocol line onto what
# should be a pure DAP socket, and the launch handshake silently never
# completes. `_await_handshake`/`_reset_for_retry` in server.py retry the
# launch once against a fresh rdbg process to work around it. These tests
# fake out rdbg entirely (bare asyncio primitives standing in for the
# rdbg-socket pump task and the child-process handle) so they can run
# without rdbg installed.


class SinkWriter:
    def __init__(self):
        self.chunks: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.chunks.append(data)

    async def drain(self) -> None:
        pass


def _messages(writer: SinkWriter) -> list[dict]:
    blob = b"".join(writer.chunks)
    out = []
    while blob:
        header, _, rest = blob.partition(b"\r\n\r\n")
        length = int(header.split(b":")[1])
        out.append(json.loads(rest[:length]))
        blob = rest[length:]
    return out


def _server() -> RubyDapServer:
    return RubyDapServer(asyncio.StreamReader(), SinkWriter())


async def _cancel_and_reap(task: asyncio.Future) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


# ---- _await_handshake ----


async def test_await_handshake_true_when_event_already_set():
    server = _server()
    event = asyncio.Event()
    event.set()
    pump = server._spawn_task(asyncio.sleep(10))
    try:
        assert await server._await_handshake(event, pump, timeout=1.0) is True
    finally:
        await _cancel_and_reap(pump)


async def test_await_handshake_true_when_event_set_during_the_wait():
    server = _server()
    event = asyncio.Event()
    pump = server._spawn_task(asyncio.sleep(10))

    async def setter():
        await asyncio.sleep(0.01)
        event.set()

    setter_task = asyncio.ensure_future(setter())
    try:
        assert await server._await_handshake(event, pump, timeout=1.0) is True
    finally:
        await _cancel_and_reap(pump)
        await setter_task


async def test_await_handshake_false_on_timeout_when_pump_still_alive():
    server = _server()
    event = asyncio.Event()
    pump = server._spawn_task(asyncio.sleep(10))
    try:
        assert await server._await_handshake(event, pump, timeout=0.05) is False
        # the stalled pump is untouched by _await_handshake itself -- only
        # _reset_for_retry (tested below) tears it down
        assert not pump.done()
    finally:
        await _cancel_and_reap(pump)


async def test_await_handshake_false_when_pump_dies_before_the_event():
    """The malformed-frame / EOF path: _pump_rdbg ends without the
    handshake event ever being set."""
    server = _server()
    event = asyncio.Event()

    async def dies_immediately():
        return None

    pump = server._spawn_task(dies_immediately())
    assert await server._await_handshake(event, pump, timeout=1.0) is False


async def test_await_handshake_does_not_leak_a_pending_waiter_task():
    """The internal `handshake_event.wait()` waiter must be fully
    cancelled -- not just cancel()-requested -- before _await_handshake
    returns, or it can log a 'Task was destroyed but it is pending'
    warning once garbage collected."""
    server = _server()
    event = asyncio.Event()
    pump = server._spawn_task(asyncio.sleep(10))
    try:
        before = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}
        await server._await_handshake(event, pump, timeout=0.02)
        after = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}
        # no new, still-pending task left behind by this call
        assert after - before == set() or all(t.done() for t in after - before)
    finally:
        await _cancel_and_reap(pump)


# ---- _reset_for_retry ----


async def test_reset_for_retry_is_idempotent_with_no_active_launch():
    server = _server()
    await server._reset_for_retry()
    await server._reset_for_retry()
    assert server._proc is None
    assert server._pump_tasks == []
    assert server._watch_exit_task is None
    assert server._rdbg_pump_task is None


async def test_reset_for_retry_clears_session_flags():
    server = _server()
    server._launched = True
    server._sent_exited = True
    server._sent_terminated = True
    server._last_stopped_thread_id = 7
    server._handshake_event = asyncio.Event()

    await server._reset_for_retry()

    assert server._launched is False
    assert server._sent_exited is False
    assert server._sent_terminated is False
    assert server._last_stopped_thread_id is None
    assert server._handshake_event is None


async def test_reset_for_retry_awaits_task_cancellation_before_returning():
    """The tasks _reset_for_retry cancels must actually be finished (not
    merely cancel()-requested) by the time it returns -- otherwise a
    still-unwinding task from the failed attempt can keep running
    concurrently with the next attempt's setup."""
    server = _server()

    started = asyncio.Event()

    async def never_ends():
        started.set()
        await asyncio.Event().wait()

    pump_task = server._spawn_task(never_ends())
    watch_task = server._spawn_task(never_ends())
    server._pump_tasks = [pump_task]
    server._watch_exit_task = watch_task
    server._rdbg_pump_task = pump_task
    await started.wait()
    assert not pump_task.done() and not watch_task.done()

    await server._reset_for_retry()

    assert pump_task.cancelled()
    assert watch_task.cancelled()
    assert server._pump_tasks == []
    assert server._watch_exit_task is None
    assert server._rdbg_pump_task is None


@pytest.mark.skipif(os.name == "nt", reason="fakes os.killpg (POSIX-only kill path)")
async def test_reset_for_retry_does_not_let_a_cancelled_watch_exit_emit_stray_events(
    monkeypatch,
):
    """Regression test for the task-F fix: without awaiting the
    cancelled `_watch_exit` task's unwind, killing the stuck rdbg process
    (below) can make its `proc.wait()` resolve and let it race ahead to
    send exited/terminated for the ABANDONED attempt -- which the client
    would see as "your session just ended" while the retry is still
    setting up the next attempt. _reset_for_retry must prevent that."""
    server = _server()
    server._launched = True

    class FakeProc:
        def __init__(self):
            self.pid = 999999
            self.returncode = None
            self._exited = asyncio.Event()

        async def wait(self):
            await self._exited.wait()
            return self.returncode

        def resolve(self, code: int) -> None:
            self.returncode = code
            self._exited.set()

    proc = FakeProc()
    server._proc = proc

    def fake_killpg(pid, sig):
        proc.resolve(-15)

    monkeypatch.setattr(os, "killpg", fake_killpg)

    watch_task = server._spawn_task(server._watch_exit())
    server._watch_exit_task = watch_task
    server._pump_tasks = [watch_task]
    await asyncio.sleep(0)  # let _watch_exit start awaiting proc.wait()
    assert not watch_task.done()

    await server._reset_for_retry()

    assert watch_task.cancelled()
    # the abandoned attempt's exit must never reach the client
    out = _messages(server._writer)
    assert not any(m.get("event") in ("exited", "terminated") for m in out)


async def test_reset_for_retry_cancels_rdbg_pump_task_even_without_owned_proc():
    """externalTerminal launches have no `_proc` (see `_finish_launch`),
    so `_pump_tasks` stays empty on that path -- `_reset_for_retry` must
    still find and cancel the rdbg-socket pump via `_rdbg_pump_task`, or a
    failed terminal-mode handshake leaks a task that keeps forwarding
    whatever the orphaned rdbg process writes to the client."""
    server = _server()
    server._proc = None  # externalTerminal: no owned child process

    started = asyncio.Event()

    async def never_ends():
        started.set()
        await asyncio.Event().wait()

    task = server._spawn_task(never_ends())
    server._rdbg_pump_task = task
    server._pump_tasks = []  # exactly what _finish_launch leaves it at
    await started.wait()
    assert not task.done()

    await server._reset_for_retry()

    assert task.cancelled()
    assert server._rdbg_pump_task is None


class FakeRdbgWriter:
    """Stands in for `self._rdbg_writer` -- the write side of the socket
    to rdbg, distinct from `self._writer` (the client's stdio)."""

    def __init__(self):
        self.chunks: list[bytes] = []
        self.closed = False
        self.drain_calls = 0

    def write(self, data: bytes) -> None:
        self.chunks.append(data)

    async def drain(self) -> None:
        self.drain_calls += 1

    def close(self) -> None:
        self.closed = True


async def test_reset_for_retry_sends_terminate_and_closes_writer_for_terminal_launch():
    """Regression test (review finding on commit dce6700): externalTerminal
    launches have no `_proc`, so `_ensure_rdbg_dead` is a no-op --
    `_on_disconnect`/`_on_terminate`'s own comment names the
    proxy-originated `terminate` request as "the only channel that kills a
    terminal-mode debuggee the proxy has no process handle for". The
    original fix nulled `_rdbg_writer` in `_reset_for_retry` without ever
    using it, silently removing that channel: a failed terminal-mode
    handshake left the rdbg process (and the debuggee under it) running
    forever in the user's terminal, surviving both the retry and the
    eventual client disconnect (which finds `_rdbg_writer` already `None`
    and so sends nothing either). `_reset_for_retry` must send `terminate`
    over the writer -- same shape as `_on_disconnect`/`_on_terminate` --
    and close it, before losing the handle."""
    server = _server()
    server._proc = None  # externalTerminal: no owned child process
    rdbg_writer = FakeRdbgWriter()
    server._rdbg_writer = rdbg_writer

    await server._reset_for_retry()

    assert rdbg_writer.closed is True
    assert rdbg_writer.drain_calls >= 1
    sent = _messages(rdbg_writer)
    terminate_msgs = [m for m in sent if m.get("command") == "terminate"]
    assert len(terminate_msgs) == 1
    assert terminate_msgs[0]["type"] == "request"
    assert server._rdbg_writer is None


async def test_reset_for_retry_closes_rdbg_writer_for_proxy_owned_launch_too():
    """The writer-close fix applies unconditionally, not just to the
    externalTerminal path: a proxy-owned launch's `_rdbg_writer` must also
    be closed on retry, or the old (now-dead) socket's asyncio
    StreamWriter is left for GC to eventually close instead of being torn
    down deterministically."""
    server = _server()

    class FakeProc:
        pid = 424242
        returncode = 0  # already dead -- keeps _ensure_rdbg_dead a no-op

        async def wait(self):
            return 0

    server._proc = FakeProc()
    rdbg_writer = FakeRdbgWriter()
    server._rdbg_writer = rdbg_writer

    await server._reset_for_retry()

    assert rdbg_writer.closed is True
    sent = _messages(rdbg_writer)
    assert any(m.get("command") == "terminate" for m in sent)

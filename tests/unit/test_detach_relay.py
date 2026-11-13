"""The detach relay loop, driven with a fake controller and a real
ConsoleRunHandler. Signal tests raise real signals at the pytest process
only after the handler is installed."""

from __future__ import annotations

import asyncio
import io
import os
import signal

import pytest

from tdb.detach_relay import INTERRUPT_EXIT, relay
from tdb.run_mode import ConsoleRunHandler


class FakeController:
    def __init__(self):
        self.supports_stdin = False
        self.is_remote_attach = False
        self.used_terminal = False
        self.survives = True
        self.confirmed = True
        self.disconnects = 0
        self.interrupt_result = True
        self.continues = 0
        self.stops = 0
        self.interrupts = 0
        self.written: list[str] = []
        self.stdin_closed = False
        self.write_error: Exception | None = None
        self.continue_error: Exception | None = None
        self.interrupt_error: Exception | None = None

    async def continue_(self):
        self.continues += 1
        if self.continue_error:
            raise self.continue_error

    async def stop(self):
        self.stops += 1

    async def detach_disconnect(self):
        if not self.survives:
            return False
        if not self.confirmed:
            from tdb.session.controller import DetachNotConfirmed

            raise DetachNotConfirmed
        self.disconnects += 1
        return True

    async def interrupt_debuggee(self):
        self.interrupts += 1
        if self.interrupt_error:
            raise self.interrupt_error
        return self.interrupt_result

    async def write_stdin(self, text):
        if self.write_error:
            raise self.write_error
        self.written.append(text)

    async def close_stdin(self):
        self.stdin_closed = True


@pytest.fixture
def ctl():
    return FakeController()


@pytest.fixture
def console():
    return ConsoleRunHandler()


async def _run(ctl, console, **kw):
    kw.setdefault("install_signals", False)
    return await asyncio.wait_for(relay(ctl, console, **kw), 5)


async def _after(delay, fn):
    await asyncio.sleep(delay)
    fn()


async def test_returns_program_exit_code(ctl, console):
    asyncio.get_running_loop().call_later(0.05, console.on_exited, 3)
    assert await _run(ctl, console) == 3
    assert ctl.stops == 1


async def test_terminated_without_exit_code_is_1(ctl, console):
    asyncio.get_running_loop().call_later(0.05, console.on_terminated)
    assert await _run(ctl, console) == 1


async def test_already_exited_returns_immediately(ctl, console):
    console.on_exited(7)
    assert await _run(ctl, console) == 7
    assert ctl.stops == 1


async def test_stop_is_auto_continued(ctl, console):
    task = asyncio.create_task(relay(ctl, console, install_signals=False))
    for _ in range(3):
        console.on_stopped(1, "breakpoint")
        await asyncio.sleep(0.05)
    assert ctl.continues == 3
    console.on_exited(0)
    assert await asyncio.wait_for(task, 5) == 0


async def test_idle_relay_does_not_spin(ctl, console):
    task = asyncio.create_task(relay(ctl, console, install_signals=False))
    await asyncio.sleep(0.2)
    assert ctl.continues == 0
    console.on_exited(0)
    assert await asyncio.wait_for(task, 5) == 0


async def test_stdin_forwarded_then_eof(ctl, console):
    ctl.supports_stdin = True
    task = asyncio.create_task(
        relay(ctl, console, stdin=io.BytesIO(b"a\nb\n"), install_signals=False)
    )
    for _ in range(100):
        if ctl.stdin_closed:
            break
        await asyncio.sleep(0.02)
    assert "".join(ctl.written) == "a\nb\n"
    assert ctl.stdin_closed
    console.on_exited(0)
    assert await asyncio.wait_for(task, 5) == 0


async def test_stdin_ignored_without_route(ctl, console):
    buf = io.BytesIO(b"hello")
    console.on_exited(0)
    task = asyncio.create_task(relay(ctl, console, stdin=buf, install_signals=False))
    await asyncio.sleep(0.1)
    await task
    assert buf.tell() == 0
    assert ctl.written == []


async def test_stdin_ignored_without_route_while_running(ctl, console):
    buf = io.BytesIO(b"hello")
    task = asyncio.create_task(relay(ctl, console, stdin=buf, install_signals=False))
    await asyncio.sleep(0.1)
    assert buf.tell() == 0
    console.on_exited(0)
    await asyncio.wait_for(task, 5)


async def test_write_stdin_runtime_error_is_quiet(ctl, console):
    ctl.supports_stdin = True
    ctl.write_error = RuntimeError("closed")
    task = asyncio.create_task(
        relay(ctl, console, stdin=io.BytesIO(b"x\n"), install_signals=False)
    )
    await asyncio.sleep(0.2)
    console.on_exited(4)
    assert await asyncio.wait_for(task, 5) == 4


posix = pytest.mark.skipif(os.name == "nt", reason="POSIX signals")


async def _signal_when_installed(signum, original):
    for _ in range(200):
        if signal.getsignal(signum) is not original or _has_loop_handler(signum):
            break
        await asyncio.sleep(0.01)
    os.kill(os.getpid(), signum)


def _has_loop_handler(signum):
    loop = asyncio.get_running_loop()
    return signum in getattr(loop, "_signal_handlers", {})


@posix
async def test_sigint_delivered_to_program(ctl, console):
    original = signal.getsignal(signal.SIGINT)
    task = asyncio.create_task(relay(ctl, console))
    try:
        await _signal_when_installed(signal.SIGINT, original)
        for _ in range(100):
            if ctl.interrupts:
                break
            await asyncio.sleep(0.02)
        assert ctl.interrupts == 1
        assert not task.done()
        console.on_exited(0)
        assert await asyncio.wait_for(task, 5) == 0
    finally:
        if not task.done():
            task.cancel()
        signal.signal(signal.SIGINT, original)


@posix
async def test_sigint_fallback_stops_and_returns_130(ctl, console):
    ctl.interrupt_result = False
    original = signal.getsignal(signal.SIGINT)
    task = asyncio.create_task(relay(ctl, console))
    try:
        await _signal_when_installed(signal.SIGINT, original)
        assert await asyncio.wait_for(task, 5) == INTERRUPT_EXIT == 130
        assert ctl.stops == 1
    finally:
        if not task.done():
            task.cancel()
        signal.signal(signal.SIGINT, original)


@posix
async def test_sigterm_stops_and_returns_143(ctl, console):
    original = signal.getsignal(signal.SIGTERM)
    task = asyncio.create_task(relay(ctl, console))
    try:
        await _signal_when_installed(signal.SIGTERM, original)
        assert await asyncio.wait_for(task, 5) == 143
        assert ctl.stops == 1
    finally:
        if not task.done():
            task.cancel()
        signal.signal(signal.SIGTERM, original)


@posix
async def test_signal_handlers_restored(ctl, console):
    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    console.on_exited(0)
    await asyncio.wait_for(relay(ctl, console), 5)
    after = {s: signal.getsignal(s) for s in before}
    assert before == after
    # also restored on the exit-after-wait path
    task = asyncio.create_task(relay(ctl, console := ConsoleRunHandler()))
    await asyncio.sleep(0.1)
    console.on_exited(0)
    await asyncio.wait_for(task, 5)
    assert {s: signal.getsignal(s) for s in before} == before


async def test_continue_failure_stops_and_returns_1(ctl, console):
    ctl.continue_error = RuntimeError("boom")
    console.on_stopped(1, "breakpoint")
    assert await _run(ctl, console) == 1
    assert ctl.stops == 1


@posix
async def test_interrupt_failure_stops_and_returns_130(ctl, console):
    ctl.interrupt_error = RuntimeError("boom")
    original = signal.getsignal(signal.SIGINT)
    task = asyncio.create_task(relay(ctl, console))
    try:
        await _signal_when_installed(signal.SIGINT, original)
        assert await asyncio.wait_for(task, 5) == 130
        assert ctl.stops == 1
    finally:
        if not task.done():
            task.cancel()
        signal.signal(signal.SIGINT, original)


@posix
def test_no_abort_at_shutdown_with_open_stdin():
    """A reader still blocked on a held-open stdin must not abort Python."""
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent("""
        import asyncio, sys
        sys.path.insert(0, %r)
        from tests.unit.test_detach_relay import FakeController
        from tdb.detach_relay import relay
        from tdb.run_mode import ConsoleRunHandler

        async def main():
            ctl = FakeController(); ctl.supports_stdin = True
            con = ConsoleRunHandler()
            asyncio.get_running_loop().call_later(0.3, con.on_exited, 5)
            return await relay(ctl, con, install_signals=False)

        sys.exit(asyncio.run(main()))
    """) % os.getcwd()
    r, w = os.pipe()
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code], stdin=r, capture_output=True, timeout=30,
            cwd=os.getcwd(),
        )
    finally:
        os.close(r)
        os.close(w)
    assert proc.returncode == 5, proc.stderr
    assert proc.stderr == b""


@posix
def test_stdin_from_a_file_reaches_the_program_then_eof(tmp_path):
    """tdb's own stdin is a regular file (`tdb prog < input.txt`), not a
    tty: its content is forwarded and the end of file closes the program's
    stdin. (The TUI itself cannot run on such a stdin, so this is covered
    at the relay level rather than through the pty harness.)"""
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent("""
        import asyncio, sys
        sys.path.insert(0, %r)
        from tests.unit.test_detach_relay import FakeController
        from tdb.detach_relay import relay
        from tdb.run_mode import ConsoleRunHandler

        async def main():
            ctl = FakeController(); ctl.supports_stdin = True
            con = ConsoleRunHandler()
            asyncio.get_running_loop().call_later(0.5, con.on_exited, 5)
            code = await relay(ctl, con, install_signals=False)
            print(repr("".join(ctl.written)), ctl.stdin_closed)
            return code

        sys.exit(asyncio.run(main()))
    """) % os.getcwd()
    infile = tmp_path / "input.txt"
    infile.write_text("one\ntwo\n")
    with open(infile, "rb") as f:
        proc = subprocess.run(
            [sys.executable, "-c", code], stdin=f, capture_output=True, timeout=30,
            cwd=os.getcwd(),
        )
    assert proc.returncode == 5, proc.stderr
    assert proc.stdout.decode().strip() == "'one\\ntwo\\n' True"


async def test_continue_failure_on_terminated_session_returns(ctl, console):
    from types import SimpleNamespace

    ctl.state = SimpleNamespace(is_terminated=True, last_exit_code=4)
    ctl.continue_error = RuntimeError("session ended")
    asyncio.get_running_loop().call_later(0.05, console.on_stopped, 1, "breakpoint")
    assert await _run(ctl, console) == 4


@pytest.mark.skipif(os.name == "nt", reason="POSIX status")
async def test_signal_killed_program_maps_to_shell_code(ctl, console):
    asyncio.get_running_loop().call_later(0.05, console.on_exited, -2)
    assert await _run(ctl, console) == 130


@pytest.mark.skipif(os.name == "nt", reason="POSIX status")
async def test_interrupted_program_exit_254_maps_to_130(ctl, console):
    async def interrupt():
        ctl.interrupts += 1
        console.on_exited(254)
        return True

    ctl.interrupt_debuggee = interrupt
    asyncio.get_running_loop().call_later(0.05, os.kill, os.getpid(), signal.SIGINT)
    assert await asyncio.wait_for(relay(ctl, console), 5) == 130


@pytest.mark.skipif(os.name == "nt", reason="POSIX status")
async def test_exit_254_without_interrupt_is_kept(ctl, console):
    asyncio.get_running_loop().call_later(0.05, console.on_exited, 254)
    assert await _run(ctl, console) == 254


@pytest.mark.skipif(os.name == "nt", reason="POSIX status")
@pytest.mark.parametrize("code", [0, 2])
async def test_adapter_sigint_death_code_maps_to_130_only_after_interrupt(
    ctl, console, code
):
    from types import SimpleNamespace

    ctl.profile = SimpleNamespace(
        adapter=SimpleNamespace(quirks=SimpleNamespace(sigint_death_codes=(code,)))
    )

    async def interrupt():
        ctl.interrupts += 1
        console.on_exited(code)
        return True

    ctl.interrupt_debuggee = interrupt
    asyncio.get_running_loop().call_later(0.05, os.kill, os.getpid(), signal.SIGINT)
    assert await asyncio.wait_for(relay(ctl, console), 5) == 130


@pytest.mark.skipif(os.name == "nt", reason="POSIX status")
async def test_adapter_sigint_death_code_kept_without_interrupt(ctl, console):
    from types import SimpleNamespace

    ctl.profile = SimpleNamespace(
        adapter=SimpleNamespace(quirks=SimpleNamespace(sigint_death_codes=(0,)))
    )
    asyncio.get_running_loop().call_later(0.05, console.on_exited, 0)
    assert await _run(ctl, console) == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX job-control signals")
async def test_relay_neutralises_textual_sigcont_handler(ctl, console):
    """Textual's driver leaves SIGCONT/SIGTSTP handlers behind that would
    restart the closed TUI when gdb resumes the program."""
    seen = {}
    old = {s: signal.getsignal(s) for s in (signal.SIGCONT, signal.SIGTSTP)}
    for s in old:
        signal.signal(s, lambda *_: None)
    try:

        async def probe():
            seen.update({s: signal.getsignal(s) for s in old})
            console.on_exited(0)

        asyncio.get_running_loop().call_later(0.05, lambda: asyncio.ensure_future(probe()))
        await asyncio.wait_for(relay(ctl, console), 5)
        assert all(h == signal.SIG_DFL for h in seen.values())
    finally:
        for s, h in old.items():
            signal.signal(s, h)


@pytest.mark.skipif(os.name == "nt", reason="POSIX status")
@pytest.mark.parametrize("delay,expected", [(0.5, 130), (2.0, 130), (2.5, 0), (600.0, 0)])
async def test_sigint_death_code_only_inside_the_grace_window(
    ctl, console, monkeypatch, delay, expected
):
    from types import SimpleNamespace

    from tdb import detach_relay

    clock = [1000.0]
    # Replace the module's `time` only: patching time.monotonic itself
    # would freeze asyncio's own timers.
    monkeypatch.setattr(
        detach_relay, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    ctl.profile = SimpleNamespace(
        adapter=SimpleNamespace(quirks=SimpleNamespace(sigint_death_codes=(0,)))
    )

    async def interrupt():
        ctl.interrupts += 1
        return True  # delivered; the program is still running

    ctl.interrupt_debuggee = interrupt

    def exit_later():
        clock[0] += delay
        console.on_exited(0)

    loop = asyncio.get_running_loop()
    loop.call_later(0.05, os.kill, os.getpid(), signal.SIGINT)
    loop.call_later(0.3, exit_later)
    assert await asyncio.wait_for(relay(ctl, console), 5) == expected


async def test_adapter_connection_lost_ends_relay_nonzero(ctl, console, monkeypatch):
    from tdb import detach_relay

    monkeypatch.setattr(detach_relay, "LIVENESS_POLL", 0.05)
    ctl.connection_lost = False
    asyncio.get_running_loop().call_later(0.1, setattr, ctl, "connection_lost", True)
    assert await _run(ctl, console) == 1
    assert ctl.stops == 1

"""PerlSession.interrupt() over the attach-mode control channel."""

import pytest

from tdb.adapters.perl.session import PerlSession


class FakeWriter:
    def __init__(self):
        self.written = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written += data

    def close(self) -> None:
        self.closed = True


def _session() -> PerlSession:
    return PerlSession(on_output=lambda *_: None, on_stop=lambda: None)


def test_interrupt_without_child_or_control_is_unavailable():
    assert _session().interrupt() is False


def test_interrupt_writes_pause_byte_on_control_channel():
    s = _session()
    ctl = FakeWriter()
    s.attach_control(ctl)
    assert s.interrupt() is True
    assert ctl.written == b"p"


@pytest.mark.asyncio
async def test_stop_closes_control_channel():
    s = _session()
    ctl = FakeWriter()
    s.attach_control(ctl)
    await s.stop()
    assert ctl.closed is True


# --- external-terminal launches (Codex #8) ------------------------------
# No owned subprocess and no control channel: the debuggee pid came from
# perl5db's own `p $$`. The live perl5db socket (no EOF yet) is what
# proves that pid is still our debuggee and not a reused one.

posix_only = pytest.mark.skipif(
    __import__("os").name == "nt", reason="terminal launches are POSIX-only"
)


def _terminal_session(monkeypatch, *, eof: bool):
    import tdb.adapters.perl.session as mod

    sent = []
    monkeypatch.setattr(mod.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    s = _session()
    s.debuggee_pid = 1 << 30
    s._eof = eof
    return s, sent


@posix_only
def test_interrupt_signals_terminal_debuggee_while_connected(monkeypatch):
    import signal

    s, sent = _terminal_session(monkeypatch, eof=False)
    assert s.interrupt() is True
    assert sent == [(1 << 30, signal.SIGINT)]


@posix_only
def test_interrupt_refuses_a_terminal_pid_after_the_debuggee_is_gone(monkeypatch):
    s, sent = _terminal_session(monkeypatch, eof=True)
    assert s.interrupt() is False
    assert sent == []


@posix_only
@pytest.mark.asyncio
async def test_stop_does_not_signal_a_terminal_pid_after_eof(monkeypatch):
    """After EOF the debuggee has exited and its pid may name an
    unrelated process: stop() must not SIGTERM/SIGKILL it."""
    s, sent = _terminal_session(monkeypatch, eof=True)
    await s.stop()
    assert sent == []


@posix_only
@pytest.mark.asyncio
async def test_stop_still_ends_a_connected_terminal_debuggee(monkeypatch):
    import signal

    s, sent = _terminal_session(monkeypatch, eof=False)
    await s.stop()
    assert sent and sent[0] == (1 << 30, signal.SIGTERM)

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

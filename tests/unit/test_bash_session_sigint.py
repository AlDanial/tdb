"""BashSession.send_sigint in --terminal mode signals the debuggee's pid
only, and never an untrustworthy one."""

from __future__ import annotations

import signal

import pytest

from tdb.adapters.bash import session as session_mod
from tdb.adapters.bash.session import BashProtocolError, BashSession


def _session(pid):
    session = BashSession(
        on_output=lambda *a: None, on_stop=lambda *a: None, on_exit=lambda *a: None
    )
    session.debuggee_pid = pid
    return session


@pytest.mark.parametrize("pid", [0, 1, -1, -4242])
def test_send_sigint_refuses_untrustworthy_pid(monkeypatch, pid):
    killed = []
    monkeypatch.setattr(session_mod.os, "kill", lambda p, s: killed.append((p, s)))
    with pytest.raises(BashProtocolError):
        _session(pid).send_sigint()
    assert killed == []


def test_send_sigint_signals_terminal_debuggee_pid(monkeypatch):
    killed = []
    monkeypatch.setattr(session_mod.os, "kill", lambda p, s: killed.append((p, s)))
    _session(4242).send_sigint()
    assert killed == [(4242, signal.SIGINT)]

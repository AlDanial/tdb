"""Unit tests for tdb.breakpoint_hook (no-op guards, subprocess reuse).

These tests verify the hook's pre-spawn logic without actually starting
debugpy or a tdb subprocess. We replace the relevant std-lib hooks
(`sys.stdin`, `sys.stdout`, `subprocess.Popen`) and confirm that the
hook short-circuits or reuses as expected.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest


@pytest.fixture
def hook(monkeypatch):
    """Reload breakpoint_hook so each test starts with a clean module global."""
    import tdb.breakpoint_hook as h
    h = importlib.reload(h)
    return h


def _fake_stdio(*, tty: bool):
    return SimpleNamespace(isatty=lambda: tty)


def test_no_op_when_stdin_not_tty(hook, monkeypatch):
    """If stdin is not a TTY, breakpoint() must return without importing debugpy."""
    monkeypatch.setattr("sys.stdin", _fake_stdio(tty=False))
    monkeypatch.setattr("sys.stdout", _fake_stdio(tty=True))

    def boom(*a, **k):  # pragma: no cover
        raise AssertionError("debugpy must not be imported")

    # If the hook tried to import debugpy, this monkeypatch would fail
    # to be reached — but a working short-circuit never gets here at all.
    monkeypatch.setattr("subprocess.Popen", boom)
    hook.breakpoint()  # must not raise


def test_no_op_when_stdout_not_tty(hook, monkeypatch):
    monkeypatch.setattr("sys.stdin", _fake_stdio(tty=True))
    monkeypatch.setattr("sys.stdout", _fake_stdio(tty=False))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("Popen called"))
    hook.breakpoint()


def test_reuses_existing_subprocess_when_alive(hook, monkeypatch):
    """If a previous tdb subprocess is still alive, skip Popen + wait_for_client."""
    monkeypatch.setattr("sys.stdin", _fake_stdio(tty=True))
    monkeypatch.setattr("sys.stdout", _fake_stdio(tty=True))

    # Pretend listen() already happened.
    hook._server_port = 5555

    # Pretend a previous tdb is still running (poll() returns None).
    fake_alive_proc = SimpleNamespace(poll=lambda: None)
    hook._subprocess = fake_alive_proc

    pause_calls = []
    wait_calls = []
    popen_calls = []

    fake_debugpy = SimpleNamespace(
        breakpoint=lambda: pause_calls.append(1),
        wait_for_client=lambda: wait_calls.append(1),
        listen=lambda *a, **k: pytest.fail("listen must not be called again"),
        configure=lambda **k: None,
    )
    monkeypatch.setitem(__import__("sys").modules, "debugpy", fake_debugpy)
    monkeypatch.setattr(
        "subprocess.Popen",
        lambda *a, **k: popen_calls.append(1) or pytest.fail("Popen must not be called"),
    )

    hook.breakpoint()

    assert pause_calls == [1]
    assert wait_calls == []
    assert popen_calls == []


def test_spawns_when_previous_subprocess_exited(hook, monkeypatch):
    """If the previous tdb has exited, spawn a fresh one and wait_for_client."""
    monkeypatch.setattr("sys.stdin", _fake_stdio(tty=True))
    monkeypatch.setattr("sys.stdout", _fake_stdio(tty=True))

    hook._server_port = 5555
    # Previous subprocess exited (poll() returns the exit code).
    hook._subprocess = SimpleNamespace(poll=lambda: 0)

    pause_calls = []
    wait_calls = []
    popen_calls = []

    fake_debugpy = SimpleNamespace(
        breakpoint=lambda: pause_calls.append(1),
        wait_for_client=lambda: wait_calls.append(1),
        listen=lambda *a, **k: (None, 5555),
        configure=lambda **k: None,
    )
    monkeypatch.setitem(__import__("sys").modules, "debugpy", fake_debugpy)

    fresh_proc = SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(
        "subprocess.Popen",
        lambda *a, **k: popen_calls.append(1) or fresh_proc,
    )

    hook.breakpoint()

    assert popen_calls == [1]
    assert wait_calls == [1]
    assert pause_calls == [1]
    assert hook._subprocess is fresh_proc


def test_first_call_starts_listener(hook, monkeypatch):
    """First call (no _server_port yet) must call debugpy.listen and Popen tdb."""
    monkeypatch.setattr("sys.stdin", _fake_stdio(tty=True))
    monkeypatch.setattr("sys.stdout", _fake_stdio(tty=True))

    assert hook._server_port is None
    assert hook._subprocess is None

    listen_calls = []
    configure_calls = []

    fake_debugpy = SimpleNamespace(
        breakpoint=lambda: None,
        wait_for_client=lambda: None,
        listen=lambda addr: listen_calls.append(addr) or (None, 4242),
        configure=lambda **k: configure_calls.append(k),
    )
    monkeypatch.setitem(__import__("sys").modules, "debugpy", fake_debugpy)

    fake_proc = SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: fake_proc)

    hook.breakpoint()

    assert listen_calls == [(hook._SERVER_HOST, 0)]
    # subProcess=False is required so debugpy doesn't try to debug the spawned tdb.
    assert configure_calls and configure_calls[0]["subProcess"] is False
    assert hook._server_port == 4242
    assert hook._subprocess is fake_proc

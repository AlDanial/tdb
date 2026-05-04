"""Unit tests for the capability surface on DebugController.

`is_remote_attach`, `supports_restart`, `session_lock`, and
`get_child_client` were extracted to replace direct access to private
attributes (`_is_remote_attach`, `_child_clients`) from app.py and
server/handlers.py. These tests pin the contract.
"""

from __future__ import annotations

import asyncio

from tdb.dap.client import DAPClient
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController


def _controller() -> DebugController:
    return DebugController(ServerEventHandler())


def test_default_is_not_remote_attach():
    ctrl = _controller()
    assert ctrl.is_remote_attach is False


def test_supports_restart_default():
    """A freshly-launched session can be restarted."""
    assert _controller().supports_restart is True


def test_supports_restart_false_after_remote_attach_flag():
    ctrl = _controller()
    # remote_attach() flips this; tests don't run network so just set it.
    ctrl._is_remote_attach = True
    assert ctrl.is_remote_attach is True
    assert ctrl.supports_restart is False


def test_session_lock_returns_an_asyncio_lock():
    ctrl = _controller()
    assert isinstance(ctrl.session_lock, asyncio.Lock)
    # Same lock returned every call — must not be a freshly minted instance.
    assert ctrl.session_lock is ctrl.session_lock


def test_get_child_client_missing_pid_returns_none():
    ctrl = _controller()
    assert ctrl.get_child_client(99999) is None


def test_get_child_client_finds_tracked_pid():
    ctrl = _controller()
    fake = DAPClient()
    ctrl._child_clients[42] = fake
    assert ctrl.get_child_client(42) is fake
    assert ctrl.has_child_clients() is True


def test_has_child_clients_false_when_empty():
    assert _controller().has_child_clients() is False

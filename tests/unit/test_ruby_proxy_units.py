"""Pure-logic tests for the ruby proxy: seq translation and transport
selection. No Ruby/rdbg required."""

import os

import pytest

from tdb.adapters.ruby.server import (
    CAPABILITIES,
    MIN_DEBUG_GEM,
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

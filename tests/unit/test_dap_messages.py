"""Unit tests for tdb.dap.messages and tdb.dap.protocol."""

from __future__ import annotations

import json

import pytest

from tdb.dap.messages import Event, Request, Response, parse_message
from tdb.dap.protocol import encode_message


def test_request_to_dict_no_args():
    r = Request(seq=1, command="initialize")
    assert r.to_dict() == {
        "seq": 1,
        "type": "request",
        "command": "initialize",
    }


def test_request_to_dict_with_args():
    r = Request(seq=2, command="launch", arguments={"program": "/x.py"})
    d = r.to_dict()
    assert d["arguments"] == {"program": "/x.py"}
    assert d["type"] == "request"


def test_response_from_dict_success():
    r = Response.from_dict(
        {
            "seq": 5,
            "request_seq": 4,
            "command": "launch",
            "success": True,
            "body": {"x": 1},
        }
    )
    assert r.success is True
    assert r.body == {"x": 1}
    assert r.message is None


def test_response_from_dict_failure():
    r = Response.from_dict(
        {
            "seq": 5,
            "request_seq": 4,
            "command": "launch",
            "success": False,
            "message": "boom",
        }
    )
    assert r.success is False
    assert r.message == "boom"
    assert r.body == {}


def test_event_from_dict():
    e = Event.from_dict(
        {
            "seq": 7,
            "event": "stopped",
            "body": {"reason": "breakpoint"},
        }
    )
    assert e.event == "stopped"
    assert e.body["reason"] == "breakpoint"


def test_parse_message_dispatches_response():
    m = parse_message(
        {
            "type": "response",
            "seq": 1,
            "request_seq": 1,
            "command": "x",
            "success": True,
        }
    )
    assert isinstance(m, Response)


def test_parse_message_dispatches_event():
    m = parse_message({"type": "event", "seq": 1, "event": "stopped"})
    assert isinstance(m, Event)


def test_parse_message_dispatches_request():
    m = parse_message({"type": "request", "seq": 1, "command": "runInTerminal"})
    assert isinstance(m, Request)


def test_parse_message_rejects_unknown_type():
    with pytest.raises(ValueError):
        parse_message({"type": "telemetry", "seq": 1})


def test_encode_message_framing():
    raw = encode_message({"seq": 1, "type": "request", "command": "x"})
    assert raw.startswith(b"Content-Length: ")
    header, _, body = raw.partition(b"\r\n\r\n")
    declared = int(header.split(b":", 1)[1].strip())
    assert declared == len(body)
    parsed = json.loads(body.decode("utf-8"))
    assert parsed["command"] == "x"

"""Unit tests for tdb.server.rpc_types."""

from __future__ import annotations

import json

from tdb.server.rpc_types import RpcRequest, RpcResponse


def test_rpc_request_default_params():
    req = RpcRequest(action="status")
    assert req.params == []


def test_rpc_request_with_params():
    req = RpcRequest(action="set_breakpoint", params=["/x.py:5"])
    assert req.action == "set_breakpoint"
    assert req.params == ["/x.py:5"]


def test_rpc_response_ok_defaults():
    resp = RpcResponse.ok()
    assert resp.success is True
    assert resp.value == ""
    assert resp.timestamp


def test_rpc_response_ok_with_value():
    resp = RpcResponse.ok("hello")
    assert resp.success is True
    assert resp.value == "hello"


def test_rpc_response_error():
    resp = RpcResponse.error("nope")
    assert resp.success is False
    assert resp.value == "nope"
    assert resp.timestamp


def test_rpc_response_ok_data_keeps_legacy_value():
    response = RpcResponse.ok_data({"threads": []})

    assert response.success is True
    assert response.data == {"threads": []}
    assert json.loads(response.value) == {"threads": []}

"""JSON-RPC request/response types for the debug server."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

from pydantic import BaseModel


class RpcRequest(BaseModel):
    action: str
    params: list[Any] = []


class RpcResponse(BaseModel):
    timestamp: str
    success: bool
    value: str = ""
    data: dict[str, Any] | None = None

    @classmethod
    def ok(cls, value: str = "") -> RpcResponse:
        return cls(
            timestamp=datetime.now(timezone.utc).isoformat(),
            success=True,
            value=value,
        )

    @classmethod
    def ok_data(cls, data: dict[str, Any], value: str = "") -> RpcResponse:
        """Return a structured payload in ``data``.

        ``value`` stays the short human-readable line legacy string
        clients render; duplicating the whole payload there would double
        every response body for no consumer.
        """
        return cls(
            timestamp=datetime.now(timezone.utc).isoformat(),
            success=True,
            value=value or json.dumps(data, sort_keys=True),
            data=data,
        )

    @classmethod
    def error(cls, message: str) -> RpcResponse:
        return cls(
            timestamp=datetime.now(timezone.utc).isoformat(),
            success=False,
            value=message,
        )

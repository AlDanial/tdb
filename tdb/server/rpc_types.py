"""JSON-RPC request/response types for the debug server."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel


class RpcRequest(BaseModel):
    action: str
    params: list[Any] = []


class RpcResponse(BaseModel):
    timestamp: str
    success: bool
    value: str = ""

    @classmethod
    def ok(cls, value: str = "") -> RpcResponse:
        return cls(
            timestamp=datetime.now(timezone.utc).isoformat(),
            success=True,
            value=value,
        )

    @classmethod
    def error(cls, message: str) -> RpcResponse:
        return cls(
            timestamp=datetime.now(timezone.utc).isoformat(),
            success=False,
            value=message,
        )

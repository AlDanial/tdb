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
    def ok_data(cls, data: dict[str, Any]) -> RpcResponse:
        """Return structured data while preserving legacy string clients."""
        return cls(
            timestamp=datetime.now(timezone.utc).isoformat(),
            success=True,
            value=json.dumps(data, sort_keys=True),
            data=data,
        )

    @classmethod
    def error(cls, message: str) -> RpcResponse:
        return cls(
            timestamp=datetime.now(timezone.utc).isoformat(),
            success=False,
            value=message,
        )

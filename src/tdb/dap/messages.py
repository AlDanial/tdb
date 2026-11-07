"""DAP message types and factory methods."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tdb.terminal_text import clean_message


@dataclass
class Request:
    seq: int
    command: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "seq": self.seq,
            "type": "request",
            "command": self.command,
        }
        if self.arguments:
            d["arguments"] = self.arguments
        return d


@dataclass
class Response:
    seq: int
    request_seq: int
    command: str
    success: bool
    body: dict[str, Any] = field(default_factory=dict)
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "seq": self.seq,
            "type": "response",
            "request_seq": self.request_seq,
            "command": self.command,
            "success": self.success,
        }
        if self.body:
            d["body"] = self.body
        if self.message is not None:
            d["message"] = self.message
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Response:
        return cls(
            seq=data["seq"],
            request_seq=data["request_seq"],
            command=data["command"],
            success=data["success"],
            body=data.get("body", {}),
            message=data.get("message"),
        )


@dataclass
class Event:
    seq: int
    event: str
    body: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"seq": self.seq, "type": "event", "event": self.event}
        if self.body:
            d["body"] = self.body
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Event:
        return cls(
            seq=data["seq"],
            event=data["event"],
            body=data.get("body", {}),
        )


def parse_message(data: dict[str, Any]) -> Request | Response | Event:
    msg_type = data["type"]
    # Text from the debuggee, on its way to tdb's own terminal.
    if msg_type == "response":
        return Response.from_dict(clean_message(data))
    elif msg_type == "event":
        cleaned = clean_message(data)
        if data.get("event") == "output" and "output" in data.get("body", {}):
            # ConsoleView keeps the colours, safely.
            cleaned["body"]["output"] = data["body"]["output"]
        return Event.from_dict(cleaned)
    elif msg_type == "request":
        return Request(
            seq=data["seq"],
            command=data["command"],
            arguments=data.get("arguments", {}),
        )
    raise ValueError(f"Unknown message type: {msg_type}")

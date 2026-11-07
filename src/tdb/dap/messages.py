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


class MalformedMessage(ValueError):
    """A DAP message tdb cannot use. Under `tdb -a PID` the target's own
    debugpy sends them, so one must cost that message, not the session."""


_PAYLOAD = {"response": "body", "event": "body", "request": "arguments"}


def parse_message(data: Any) -> Request | Response | Event:
    if not isinstance(data, dict):
        raise MalformedMessage(f"not an object: {type(data).__name__}")
    msg_type = data.get("type")
    payload = _PAYLOAD.get(msg_type)  # type: ignore[arg-type]
    if payload is None:
        raise MalformedMessage(f"unknown message type: {msg_type!r}")
    if data.get(payload) is None:
        data = {**data, payload: {}}
    elif not isinstance(data[payload], dict):
        raise MalformedMessage(f"{msg_type} {payload} is not an object")
    try:
        return _parse(msg_type, data)
    except (KeyError, TypeError, RecursionError) as exc:
        raise MalformedMessage(f"{msg_type}: {exc!r}") from exc


def _parse(msg_type: str, data: dict[str, Any]) -> Request | Response | Event:
    # Text from the debuggee, on its way to tdb's own terminal.
    if msg_type == "response":
        return Response.from_dict(clean_message(data))
    if msg_type == "event":
        cleaned = clean_message(data)
        output = data["body"].get("output")
        if data.get("event") == "output" and isinstance(output, str):
            # ConsoleView keeps the colours, safely.
            cleaned["body"]["output"] = output
        return Event.from_dict(cleaned)
    return Request(
        seq=data["seq"],
        command=data["command"],
        arguments=data["arguments"],
    )

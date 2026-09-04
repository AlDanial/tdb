"""Seq renumbering between the two sides of a DAP proxy.

Each side sees a gapless seq space owned by the proxy. A forwarded
request remembers the originator's seq so the answering side's response
can be restamped with it; responses to requests the proxy itself
originated have no mapping and translate to None (the proxy swallows or
routes those itself). Generic twin of the ruby proxy's SeqTranslator
("rdbg" -> "upstream"); ruby keeps its own copy for now.
"""

from __future__ import annotations


class SeqTranslator:
    def __init__(self) -> None:
        self._client_seq = 0
        self._upstream_seq = 0
        self._from_client: dict[int, int] = {}  # upstream seq -> client seq
        self._from_upstream: dict[int, int] = {}  # client seq -> upstream seq

    def next_client_seq(self) -> int:
        self._client_seq += 1
        return self._client_seq

    def next_upstream_seq(self) -> int:
        self._upstream_seq += 1
        return self._upstream_seq

    def client_request_to_upstream(self, msg: dict) -> dict:
        out = dict(msg)
        out["seq"] = self.next_upstream_seq()
        self._from_client[out["seq"]] = msg["seq"]
        return out

    def upstream_response_to_client(self, msg: dict) -> dict | None:
        orig = self._from_client.pop(msg.get("request_seq", -1), None)
        if orig is None:
            return None
        out = dict(msg)
        out["seq"] = self.next_client_seq()
        out["request_seq"] = orig
        return out

    def upstream_event_to_client(self, msg: dict) -> dict:
        out = dict(msg)
        out["seq"] = self.next_client_seq()
        return out

    def upstream_request_to_client(self, msg: dict) -> dict:
        out = dict(msg)
        out["seq"] = self.next_client_seq()
        self._from_upstream[out["seq"]] = msg["seq"]
        return out

    def client_response_to_upstream(self, msg: dict) -> dict | None:
        orig = self._from_upstream.pop(msg.get("request_seq", -1), None)
        if orig is None:
            return None
        out = dict(msg)
        out["seq"] = self.next_upstream_seq()
        out["request_seq"] = orig
        return out

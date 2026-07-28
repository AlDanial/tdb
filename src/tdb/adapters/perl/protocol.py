"""Incremental parser for the perl5db socket stream.

Contract: the adapter interprets exactly two shapes — the perl5db
prompt (end of a command's response / an unsolicited stop) and
TDB>>>{json}<<<TDB lines printed by the injected helpers. Everything
else is passthrough text.
"""

from __future__ import annotations

import json
import re

PROMPT_RE = re.compile(rb"\r?\n?\s*DB<+\d+>+ $")
MARK_RE = re.compile(rb"TDB>>>(.*?)<<<TDB\r?\n?", re.DOTALL)
# Longest prefix that could still grow into a marker or prompt; used to
# hold back tail bytes instead of flushing them as text prematurely.
_HOLD_RE = re.compile(
    rb"(?:TDB>?>?>?[^<]*(?:<(?:<(?:TDB?)?)?)?|\r?\n?\s*DB<*\d*>*\x20?)$"
)

Event = tuple[str, object]


class StreamParser:
    def __init__(self) -> None:
        self._buf = b""

    def feed(self, data: bytes) -> list[Event]:
        self._buf += data
        events: list[Event] = []
        while True:
            m = MARK_RE.search(self._buf)
            if m is None:
                break
            before = self._buf[: m.start()]
            if before:
                events.append(("text", before.decode("utf-8", errors="replace")))
            try:
                events.append(("json", json.loads(m.group(1).decode("utf-8"))))
            except (ValueError, UnicodeDecodeError):
                events.append(("text", m.group(0).decode("utf-8", errors="replace")))
            self._buf = self._buf[m.end() :]
        pm = PROMPT_RE.search(self._buf)
        if pm is not None:
            before = self._buf[: pm.start()]
            match_bytes = pm.group(0)
            # Count leading \r and \n bytes that should stay in text
            consumed_newlines = 0
            for b in match_bytes:
                if b in (ord("\r"), ord("\n")):
                    consumed_newlines += 1
                else:
                    break
            # Include consumed newlines in the text event
            if consumed_newlines > 0:
                before += match_bytes[:consumed_newlines]
            if before:
                events.append(("text", before.decode("utf-8", errors="replace")))
            events.append(("prompt", None))
            self._buf = self._buf[pm.end() :]
        else:
            hold = _HOLD_RE.search(self._buf)
            flush_upto = hold.start() if hold else len(self._buf)
            if flush_upto:
                events.append(
                    ("text", self._buf[:flush_upto].decode("utf-8", errors="replace"))
                )
                self._buf = self._buf[flush_upto:]
        return self._coalesce(events)

    def _coalesce(self, events: list[Event]) -> list[Event]:
        """Merge adjacent text events."""
        if not events:
            return events
        coalesced: list[Event] = []
        for event_type, value in events:
            if event_type == "text" and coalesced and coalesced[-1][0] == "text":
                # Merge with previous text event
                prev_text = coalesced[-1][1]
                coalesced[-1] = ("text", prev_text + value)
            else:
                coalesced.append((event_type, value))
        return coalesced

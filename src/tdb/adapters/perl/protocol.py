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
_MARK_OPEN = b"TDB>>>"
# Proper prefixes of _MARK_OPEN, longest first, checked against the
# buffer tail to detect a marker opener that's still arriving.
_MARK_OPEN_PREFIXES = (b"TDB>>", b"TDB>", b"TDB", b"TD", b"T")
# Longest prefix that could still grow into a prompt; used to hold back
# tail bytes instead of flushing them as text prematurely. Partial
# marker openers are handled separately (see feed()) since a marker's
# JSON payload can legitimately contain '<' and so can't be represented
# by a single regex hold.
_HOLD_RE = re.compile(rb"\r?\n?\s*DB<*\d*>*\x20?$")

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

        # An incomplete TDB>>>...<<<TDB marker may still be growing (a
        # single helper payload can span multiple socket reads). Its
        # payload can contain '<' (e.g. Perl's <$fh> / <STDIN> syntax),
        # so once we know we're inside one we must hold the WHOLE
        # remainder verbatim rather than scanning it for a prompt or
        # flushing any of it as text -- the prompt always follows the
        # marker's own trailing newline, never inside the held region.
        idx = self._buf.find(_MARK_OPEN)
        if idx == -1:
            for prefix in _MARK_OPEN_PREFIXES:
                if self._buf.endswith(prefix):
                    idx = len(self._buf) - len(prefix)
                    break
        if idx != -1:
            before = self._buf[:idx]
            if before:
                events.append(("text", before.decode("utf-8", errors="replace")))
            self._buf = self._buf[idx:]
            return self._coalesce(events)

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

"""variablesReference bookkeeping for the Perl adapter.

DAP hands out integer handles; the debuggee-side helper keeps its own
stash of expandable refs (cleared per stop). This registry maps DAP
handles to either a scope root or a helper stash id. Reset on every
stop so stale handles can't resolve to freed objects.
"""

from __future__ import annotations


class RefRegistry:
    def __init__(self) -> None:
        self._next = 1
        self._entries: dict[int, dict] = {}

    def _add(self, entry: dict) -> int:
        ref = self._next
        self._next += 1
        self._entries[ref] = entry
        return ref

    def add_scope(self, frame: int, kind: str) -> int:
        return self._add({"kind": "scope", "frame": frame, "scope": kind})

    def add_object(self, helper_id: int) -> int:
        return self._add({"kind": "object", "helper_id": helper_id})

    def get(self, ref: int) -> dict | None:
        return self._entries.get(ref)

    def reset(self) -> None:
        self._next = 1
        self._entries.clear()

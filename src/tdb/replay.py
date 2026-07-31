"""Replay a --record session file through the headless RPC dispatch.

Spec: docs/superpowers/specs/2026-07-31-record-replay-design.md.
The file format is JSONL: line 1 is the header, every other line is one
{"t", "action", "params"} command identical in shape to a POST /rpc body.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class RecordingError(Exception):
    pass


@dataclass
class Recording:
    header: dict
    records: list[dict]


_LAUNCH_REQUIRED = ("language", "program", "cwd")
_ATTACH_REQUIRED = ("language", "host", "port")


def load_recording(path: str) -> Recording:
    from tdb.server.handlers import RpcHandlers

    known_actions = set(RpcHandlers.ACTIONS)
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    if not lines or not lines[0].strip():
        raise RecordingError(f"{path}: empty file (not a tdb recording)")

    def parse(lineno: int, text: str) -> dict:
        try:
            obj = json.loads(text)
        except ValueError as e:
            raise RecordingError(f"{path}:{lineno}: invalid JSON: {e}") from e
        if not isinstance(obj, dict):
            raise RecordingError(f"{path}:{lineno}: expected a JSON object")
        return obj

    header = parse(1, lines[0])
    if header.get("tdb_recording") != 1:
        raise RecordingError(
            f"{path}:1: not a tdb recording, or unsupported version "
            f"(tdb_recording={header.get('tdb_recording')!r}; this tdb reads v1)"
        )
    mode = header.get("mode")
    if mode not in ("launch", "remote-attach"):
        raise RecordingError(f"{path}:1: unknown mode {mode!r}")
    required = _LAUNCH_REQUIRED if mode == "launch" else _ATTACH_REQUIRED
    for key in required:
        if header.get(key) is None:
            raise RecordingError(f"{path}:1: header is missing {key!r}")

    records: list[dict] = []
    for lineno, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        rec = parse(lineno, line)
        if not isinstance(rec.get("t"), (int, float)):
            raise RecordingError(f"{path}:{lineno}: missing or non-numeric 't'")
        if rec.get("action") not in known_actions:
            raise RecordingError(
                f"{path}:{lineno}: unknown action {rec.get('action')!r} "
                "(recording from a newer tdb?)"
            )
        if not isinstance(rec.get("params"), list):
            raise RecordingError(f"{path}:{lineno}: 'params' must be a list")
        records.append(rec)
    return Recording(header=header, records=records)

"""Session recording: user debugging gestures as replayable JSON-RPC lines.

`--record FILE` captures each TUI gesture as one JSONL command record
(spec: docs/superpowers/specs/2026-07-31-record-replay-design.md).
Stdlib-only on purpose: the recorder must never entangle app/controller
imports, and a write failure must never take the debug session down.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Callable


class SessionRecorder:
    """Appends one flushed JSON line per recorded gesture."""

    def __init__(self, path: str, header: dict) -> None:
        self.on_error: Callable[[str], None] | None = None
        self._file = open(path, "w", encoding="utf-8")
        self._t0 = time.monotonic()
        self.active = True
        self._write_line(header)

    def _write_line(self, obj: dict) -> None:
        self._file.write(json.dumps(obj) + "\n")
        self._file.flush()

    def record(self, action: str, params: list) -> None:
        if not self.active:
            return
        try:
            self._write_line(
                {
                    "t": round(time.monotonic() - self._t0, 3),
                    "action": action,
                    "params": params,
                }
            )
        except (OSError, ValueError) as e:
            # ValueError covers writes to a closed file object. Degrade to
            # inert: the debug session must survive a dead recording.
            self.active = False
            try:
                self._file.close()
            except OSError:
                pass
            if self.on_error is not None:
                self.on_error(f"Recording stopped ({e}); session continues")

    def close(self) -> None:
        if self.active:
            self.active = False
            try:
                self._file.close()
            except OSError:
                pass


class NullRecorder:
    """No-op twin so gesture hooks never need an `if recording` guard."""

    def __init__(self) -> None:
        self.active = False
        self.on_error: Callable[[str], None] | None = None

    def record(self, action: str, params: list) -> None:
        pass

    def close(self) -> None:
        pass


def build_header(args, config) -> dict:
    """Header line for a new recording, from the parsed CLI namespace.

    `args` is argparse output post `parse_args()` (profile resolved,
    program path absolute); `config` is the loaded TdbConfig.
    """
    header = {
        "tdb_recording": 1,
        "created": datetime.now().isoformat(timespec="seconds"),
        "language": args.profile.id if args.profile else "python",
        "adapter": args.adapter,
        "step_mode": config.step_mode,
        "no_just_my_code": args.no_just_my_code,
    }
    if args.attach_host:
        header.update(
            mode="remote-attach",
            host=args.attach_host,
            port=args.attach_port,
            path_mappings=[list(pm) for pm in (args.path_mappings or [])],
            # Rust native remote attach needs the local symbol-bearing
            # executable; None for languages that attach without one.
            program=args.program,
        )
    else:
        header.update(
            mode="launch",
            program=args.program,
            args=list(args.args or []),
            cwd=args.cwd or os.getcwd(),
            python=args.python,
        )
    return header

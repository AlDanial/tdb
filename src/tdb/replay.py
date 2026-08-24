"""Replay a --record session file through the headless RPC dispatch.

Spec: docs/superpowers/specs/2026-07-31-record-replay-design.md.
The file format is JSONL: line 1 is the header, every other line is one
{"t", "action", "params"} command identical in shape to a POST /rpc body.
"""

from __future__ import annotations

import asyncio
import json
import sys
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


BLOCKING_ACTIONS = {"next", "step_in", "step_out", "continue", "wait_for_stop"}


def _profile_from_header(header: dict):
    from tdb.languages import registry
    from tdb.persist import load_config

    config = load_config()
    lang = header.get("language") or "python"
    adapter = header.get("adapter") or config.default_adapters.get(lang)
    # header.get, not header[...]: attach-mode headers have no "program"
    # key (see _ATTACH_REQUIRED) -- resolve() already treats program=None
    # as "no local target", same as the CLI's attach path.
    return registry.resolve(
        lang,
        adapter=adapter,
        adapter_paths=config.adapters,
        program=header.get("program"),
    )


def _print_command(echo, rec: dict, success: bool, value: str) -> None:
    echo(f"[{rec['t']:8.3f}] {rec['action']} {json.dumps(rec['params'])}")
    prefix = "ok:" if success else "ERROR:"
    lines = (value or "").splitlines() or [""]
    echo(f"          {prefix} {lines[0]}".rstrip())
    for line in lines[1:]:
        echo(f"          {line}")


def _print_program_output(echo, text: str) -> None:
    echo("          --- program output ---")
    for line in text.splitlines():
        echo(f"          {line}")
    echo("          ----------------------")


async def run_replay(
    recording: Recording,
    timing: bool = False,
    replay_timeout: float = 30.0,
    echo=print,
) -> int:
    """Feed every record through the RPC dispatch table. Returns the
    number of failed commands (0 == clean replay)."""
    from tdb.server.handlers import ControllerRef, RpcHandlers
    from tdb.server.rpc_types import RpcResponse
    from tdb.server.runner import setup_headless_session

    h = recording.header
    if h["mode"] == "launch":
        controller, handler = await setup_headless_session(
            program=h["program"],
            args=list(h.get("args") or []),
            cwd=h["cwd"],
            # Always park at entry: the recording's own records install
            # breakpoints and (for originally non-entry-stop sessions)
            # carry the explicit `continue` that starts the program.
            stop_on_entry=True,
            just_my_code=not h.get("no_just_my_code", False),
            python=h.get("python"),
            profile=_profile_from_header(h),
            step_mode=h.get("step_mode"),
        )
    else:
        controller, handler = await setup_headless_session(
            # Rust native remote attach records the local symbol-bearing
            # executable in the header; other languages record None.
            program=h.get("program"),
            attach_host=h["host"],
            attach_port=h["port"],
            path_mappings=[tuple(pm) for pm in (h.get("path_mappings") or [])] or None,
            profile=_profile_from_header(h),
            step_mode=h.get("step_mode"),
        )

    handlers = RpcHandlers(ControllerRef(controller), handler)
    table = handlers.dispatch_table()
    errors = 0
    prev_t = 0.0
    saw_quit = False
    try:
        for rec in recording.records:
            if timing and rec["t"] > prev_t:
                await asyncio.sleep(rec["t"] - prev_t)
            prev_t = rec["t"]
            params = list(rec["params"])
            if rec["action"] in BLOCKING_ACTIONS and not params:
                params = [replay_timeout]
            try:
                resp = await table[rec["action"]](params)
            except Exception as e:
                # A handler exception (e.g. action_restart hitting an
                # attach controller's empty _launch_params) must not abort
                # the whole replay — convert it into a failed-command
                # transcript block so the spec's "execution continues past
                # failed commands" + summary line + exit-code contract
                # holds for internal errors too, not just RpcResponse.error
                # results.
                resp = RpcResponse.error(f"internal error: {e!r}")
            _print_command(echo, rec, resp.success, resp.value)
            if not resp.success:
                errors += 1
            if rec["action"] == "quit":
                saw_quit = True
            # Any debuggee output emitted between this drain and process
            # exit/replay end (e.g. right after `quit`, or after the loop's
            # final command) is never drained again — accepted: replay's
            # transcript is a record of commands, not a guarantee of
            # capturing every last byte of program output.
            pending = handler.drain_output()
            if pending:
                _print_program_output(echo, pending)
    finally:
        if not saw_quit:
            try:
                await controller.stop()
            except Exception:
                pass
    echo(f"{len(recording.records)} commands, {errors} errors")
    return errors


def replay_main(path: str, timing: bool, replay_timeout: float) -> None:
    try:
        recording = load_recording(path)
    except (OSError, RecordingError) as e:
        print(f"tdb: {e}", file=sys.stderr)
        sys.exit(2)
    errors = asyncio.run(
        run_replay(recording, timing=timing, replay_timeout=replay_timeout)
    )
    sys.exit(0 if errors == 0 else 1)

"""Persist debug state and configuration across runs."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from tdb.dap.types import SourceBreakpoint

log = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config" / "tdb"
STATE_FILE = CONFIG_DIR / "last_run.json"
CONFIG_FILE = CONFIG_DIR / "config.json"


def _encode_bps(
    breakpoints: dict[str, list[SourceBreakpoint]],
) -> dict[str, list[dict]]:
    out = {}
    for source_path, bps in breakpoints.items():
        if bps:
            out[source_path] = [
                {
                    "line": bp.line,
                    "condition": bp.condition,
                    "hit_condition": bp.hit_condition,
                }
                for bp in bps
            ]
    return out


def _decode_bps(raw: dict) -> dict[str, list[SourceBreakpoint]]:
    result: dict[str, list[SourceBreakpoint]] = {}
    for source_path, bp_list in raw.items():
        bps = []
        for entry in bp_list:
            bps.append(SourceBreakpoint(
                line=entry["line"],
                condition=entry.get("condition"),
                hit_condition=entry.get("hit_condition"),
            ))
        if bps:
            result[source_path] = bps
    return result


def _read_state() -> dict:
    if not STATE_FILE.is_file():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        log.exception("Failed to read state file %s", STATE_FILE)
        return {}


def save_breakpoints(
    breakpoints: dict[str, list[SourceBreakpoint]],
    program: str | None = None,
) -> None:
    """Write breakpoints to the state file, keyed by program path.

    When program is None, writes to the flat legacy format.
    """
    data = _encode_bps(breakpoints)
    try:
        existing = _read_state()
        if program:
            programs = existing.get("programs", {})
            if data:
                programs[program] = data
            else:
                programs.pop(program, None)
            existing["programs"] = programs
        else:
            existing["breakpoints"] = data
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(existing, indent=2) + "\n")
        log.debug("Saved %d breakpoint(s) for %s", sum(len(v) for v in data.values()), program or "(default)")
    except Exception:
        log.exception("Failed to save breakpoints to %s", STATE_FILE)


def load_breakpoints(
    program: str | None = None,
) -> dict[str, list[SourceBreakpoint]]:
    """Read breakpoints for a specific program. Returns empty dict on any error.

    When program is given, returns only breakpoints saved for that program.
    When program is None, returns the legacy flat "breakpoints" dict (used
    during migration — see migrate_legacy_breakpoints).
    """
    raw = _read_state()
    try:
        if program:
            programs = raw.get("programs", {})
            return _decode_bps(programs.get(program, {}))
        return _decode_bps(raw.get("breakpoints", {}))
    except Exception:
        log.exception("Failed to load breakpoints from %s", STATE_FILE)
        return {}


def save_config(keybindings: str | None = None) -> None:
    """Write user preferences to the config file.

    Merges with existing config so callers can update individual fields.
    """
    try:
        existing = load_config_raw()
        if keybindings is not None:
            existing["keybindings"] = keybindings
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(existing, indent=2) + "\n")
        log.debug("Saved config to %s", CONFIG_FILE)
    except Exception:
        log.exception("Failed to save config to %s", CONFIG_FILE)


def load_config_raw() -> dict:
    """Read raw config dict. Returns empty dict on any error."""
    if not CONFIG_FILE.is_file():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        log.exception("Failed to load config from %s", CONFIG_FILE)
        return {}


def load_keybinding_scheme() -> str | None:
    """Return the saved keybinding scheme name, or None if not set."""
    return load_config_raw().get("keybindings")

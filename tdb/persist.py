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


def save_breakpoints(breakpoints: dict[str, list[SourceBreakpoint]]) -> None:
    """Write breakpoints to the state file."""
    data = {}
    for source_path, bps in breakpoints.items():
        if bps:
            data[source_path] = [
                {
                    "line": bp.line,
                    "condition": bp.condition,
                    "hit_condition": bp.hit_condition,
                }
                for bp in bps
            ]
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({"breakpoints": data}, indent=2) + "\n")
        log.debug("Saved %d breakpoint(s) to %s", sum(len(v) for v in data.values()), STATE_FILE)
    except Exception:
        log.exception("Failed to save breakpoints to %s", STATE_FILE)


def load_breakpoints() -> dict[str, list[SourceBreakpoint]]:
    """Read breakpoints from the state file. Returns empty dict on any error."""
    if not STATE_FILE.is_file():
        return {}
    try:
        raw = json.loads(STATE_FILE.read_text())
        result: dict[str, list[SourceBreakpoint]] = {}
        for source_path, bp_list in raw.get("breakpoints", {}).items():
            bps = []
            for entry in bp_list:
                bps.append(SourceBreakpoint(
                    line=entry["line"],
                    condition=entry.get("condition"),
                    hit_condition=entry.get("hit_condition"),
                ))
            if bps:
                result[source_path] = bps
        log.debug("Loaded %d breakpoint(s) from %s", sum(len(v) for v in result.values()), STATE_FILE)
        return result
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

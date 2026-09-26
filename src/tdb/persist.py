"""Persist debug state and configuration across runs."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path

from tdb.dap.types import SourceBreakpoint

log = logging.getLogger(__name__)


def _default_config_dir() -> Path:
    """Per-platform config directory.

    Windows: %APPDATA%\\tdb (falls back to ~/AppData/Roaming/tdb if APPDATA
    is unset, matching what Windows normally points APPDATA at).
    Everywhere else: ~/.config/tdb (XDG-ish; we don't honor XDG_CONFIG_HOME
    yet).
    """
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "tdb"
        return Path.home() / "AppData" / "Roaming" / "tdb"
    return Path.home() / ".config" / "tdb"


CONFIG_DIR = _default_config_dir()
STATE_FILE = CONFIG_DIR / "breakpoints.json"
CONFIG_FILE = CONFIG_DIR / "config.json"
_LEGACY_STATE_FILE = CONFIG_DIR / "last_run.json"


def _encode_bps(
    breakpoints: dict[str, list[SourceBreakpoint]],
) -> dict[str, list[dict]]:
    out = {}
    for source_path, bps in breakpoints.items():
        encoded = [
            {
                "line": bp.line,
                "condition": bp.condition,
                "hit_condition": bp.hit_condition,
                "enabled": bp.enabled,
            }
            for bp in bps
            if bp.persist
        ]
        if encoded:
            out[source_path] = encoded
    return out


def _decode_bps(raw: dict) -> dict[str, list[SourceBreakpoint]]:
    result: dict[str, list[SourceBreakpoint]] = {}
    for source_path, bp_list in raw.items():
        bps = []
        for entry in bp_list:
            bps.append(
                SourceBreakpoint(
                    line=entry["line"],
                    condition=entry.get("condition"),
                    hit_condition=entry.get("hit_condition"),
                    enabled=entry.get("enabled", True),
                )
            )
        if bps:
            result[source_path] = bps
    return result


def _read_state() -> dict:
    # One-time migration from the old name (renamed 2026-05).
    if not STATE_FILE.is_file() and _LEGACY_STATE_FILE.is_file():
        try:
            _LEGACY_STATE_FILE.rename(STATE_FILE)
            log.info("Migrated %s -> %s", _LEGACY_STATE_FILE, STATE_FILE)
        except Exception:
            log.exception(
                "Failed to migrate %s to %s",
                _LEGACY_STATE_FILE,
                STATE_FILE,
            )
    if not STATE_FILE.is_file():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        log.exception("Failed to read state file %s", STATE_FILE)
        return {}


def save_breakpoints(
    breakpoints: dict[str, list[SourceBreakpoint]],
    program: str,
) -> None:
    """Write breakpoints to the state file, keyed by program path."""
    data = _encode_bps(breakpoints)
    try:
        existing = _read_state()
        programs = existing.get("programs", {})
        if data:
            programs[program] = data
        else:
            programs.pop(program, None)
        existing["programs"] = programs
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(existing, indent=2) + "\n")
        log.debug(
            "Saved %d breakpoint(s) for %s", sum(len(v) for v in data.values()), program
        )
    except Exception:
        log.exception("Failed to save breakpoints to %s", STATE_FILE)


def load_breakpoints(program: str) -> dict[str, list[SourceBreakpoint]]:
    """Read breakpoints saved for the given program. Empty dict on error."""
    raw = _read_state()
    try:
        programs = raw.get("programs", {})
        return _decode_bps(programs.get(program, {}))
    except Exception:
        log.exception("Failed to load breakpoints from %s", STATE_FILE)
        return {}


@dataclass
class TdbConfig:
    """User preferences persisted across runs (config.json).

    Adding a new persisted option = adding one field here. `from_dict`
    will tolerate older config files that don't have the key (defaults
    apply) and silently drop unknown keys (forward-compat).
    """

    # Keybinding scheme for the Code View: "vim" | "emacs" | "default".
    keybindings: str = "vim"

    # Textual theme name (e.g. "textual-dark"). None ⇒ textual's default.
    theme: str | None = None

    # Step granularity: "statement" (skip through multi-line expressions
    # as one step) or "line" (debugpy's native per-line trace).
    step_mode: str = "statement"

    # Debug-adapter executable overrides: adapter id -> path
    # (e.g. {"lldb-dap": "/opt/llvm/bin/lldb-dap"}). A None value (JSON
    # null) means "not set": tdb seeds gdb / lldb-dap here the first time
    # config.json is written, using null when they aren't on PATH, and a
    # null entry falls through to a PATH lookup exactly like a missing one.
    adapters: dict[str, str | None] = field(default_factory=dict)

    # Preferred adapter per language: language id -> adapter id
    # (e.g. {"cpp": "gdb"}).
    default_adapters: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> TdbConfig:
        """Build a TdbConfig from a raw dict (typically the parsed JSON).

        Unknown keys are dropped. Invalid values for constrained fields
        (step_mode) fall back to the default rather than raising — we
        never want a corrupt config file to brick the app.
        """
        kwargs: dict = {}
        if isinstance(data.get("keybindings"), str):
            kwargs["keybindings"] = data["keybindings"]
        if "theme" in data:
            theme = data["theme"]
            if theme is None or isinstance(theme, str):
                kwargs["theme"] = theme
        if data.get("step_mode") in ("statement", "line"):
            kwargs["step_mode"] = data["step_mode"]
        adapters = data.get("adapters")
        if isinstance(adapters, dict) and all(
            isinstance(k, str) and (v is None or isinstance(v, str))
            for k, v in adapters.items()
        ):
            kwargs["adapters"] = adapters
        default_adapters = data.get("default_adapters")
        if isinstance(default_adapters, dict) and all(
            isinstance(k, str) and isinstance(v, str)
            for k, v in default_adapters.items()
        ):
            kwargs["default_adapters"] = default_adapters
        return cls(**kwargs)

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def load_config() -> TdbConfig:
    """Read config.json into a TdbConfig. Always returns a usable config
    (defaults for missing/invalid fields, empty config on file errors).
    """
    if not CONFIG_FILE.is_file():
        return TdbConfig()
    try:
        raw = json.loads(CONFIG_FILE.read_text())
    except Exception:
        log.exception("Failed to load config from %s", CONFIG_FILE)
        return TdbConfig()
    if not isinstance(raw, dict):
        return TdbConfig()
    return TdbConfig.from_dict(raw)


def seed_native_adapters(config: TdbConfig) -> None:
    """Fill in ``adapters`` entries for the native debuggers (gdb,
    lldb-dap) that the user hasn't set, with the executable found on
    PATH or None when absent. Called once, when config.json is first
    written, so the file documents the keys a user can override."""
    from tdb.languages.native_tools import NATIVE_ADAPTER_IDS

    for adapter_id in NATIVE_ADAPTER_IDS:
        if adapter_id not in config.adapters:
            config.adapters[adapter_id] = shutil.which(adapter_id)


def log_file() -> Path:
    """Where tdb writes its log: ``$TDB_LOG_DIR/tdb.log`` when the
    variable is set (tests use it to keep noise out of the user's config
    dir), else ``CONFIG_DIR/tdb.log``."""
    return Path(os.environ.get("TDB_LOG_DIR") or CONFIG_DIR) / "tdb.log"


def save_config(config: TdbConfig) -> None:
    """Write a TdbConfig to config.json. Best-effort: I/O errors are
    logged and swallowed (we never want a failed save to crash tdb).

    The first write seeds ``adapters`` with gdb / lldb-dap (see
    ``seed_native_adapters``); later writes leave the map alone so a
    deliberately removed key stays removed."""
    try:
        if not CONFIG_FILE.exists():
            seed_native_adapters(config)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(config.to_dict(), indent=2) + "\n")
        log.debug("Saved config to %s", CONFIG_FILE)
    except Exception:
        log.exception("Failed to save config to %s", CONFIG_FILE)

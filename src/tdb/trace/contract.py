"""What tdb and the trace agent agree on: config file, environment, defaults.

tdb writes the agent config with ``write_config`` and starts the program
with ``build_env``.  The agent (``agent/_tdb_trace_agent.py``) reads the
same names and formats but never imports this module: it is stdlib-only
and runs inside the traced program.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent / "agent"

CONFIG_ENV = "TDB_TRACE_CONFIG"
PARTS_ENV = "TDB_TRACE_PARTS"
_ENV_PREFIX = "TDB_TRACE_"

DEFAULT_MAX_EVENTS = 5_000_000  # recorded events per process
DEFAULT_REPR_LIMIT = 200  # characters kept of a captured value
FLUSH_INTERVAL = 0.25  # seconds between part-file flushes
DISCOVER_CAP = 10_000  # calls after which discovery stops counting
CHILD_FLUSH_WAIT = 2.0  # seconds tdb waits for children to flush


def write_config(
    path: Path,
    *,
    mode: str,
    parts_dir: Path,
    targets: list[dict],
    max_events: int = DEFAULT_MAX_EVENTS,
    repr_limit: int = DEFAULT_REPR_LIMIT,
    flush_interval: float = FLUSH_INTERVAL,
    discover_cap: int = DISCOVER_CAP,
    user_roots: list[str] | tuple[str, ...] = (),
) -> None:
    """Write the agent config JSON (format: plan "Agent config JSON").

    ``user_roots`` (discover mode) are paths whose code is the program's
    own even under a library directory: the program's file and directory.
    """
    config = {
        "version": 1,
        "mode": mode,
        "parts": os.path.abspath(parts_dir),
        "agent_dir": str(AGENT_DIR),
        "targets": targets,
        "max_events": max_events,
        "repr_limit": repr_limit,
        "flush_interval": flush_interval,
        "discover_cap": discover_cap,
        "user_roots": list(user_roots),
    }
    Path(path).write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def _same_path(a: str | os.PathLike, b: str | os.PathLike) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def build_env(
    base: Mapping[str, str], *, config_path: Path, parts_dir: Path
) -> dict[str, str]:
    """Return the traced program's environment, built from ``base``.

    Inherited ``TDB_TRACE_*`` variables and earlier copies of the agent
    directory on ``PYTHONPATH`` are dropped first, so a traced program
    that itself runs ``tdb --trace`` starts a clean, separate trace.
    """
    env = {k: v for k, v in base.items() if not k.startswith(_ENV_PREFIX)}
    old = env.get("PYTHONPATH", "")
    # Keep other entries as they were, empty ones included: an empty
    # entry means "the cwd" to Python, so it must neither be dropped nor
    # added (which a trailing separator on an unset PYTHONPATH would do).
    kept = (
        [e for e in old.split(os.pathsep) if not (e and _same_path(e, AGENT_DIR))]
        if old
        else []
    )
    env["PYTHONPATH"] = os.pathsep.join([str(AGENT_DIR), *kept])
    env[CONFIG_ENV] = os.path.abspath(config_path)
    env[PARTS_ENV] = os.path.abspath(parts_dir)
    return env

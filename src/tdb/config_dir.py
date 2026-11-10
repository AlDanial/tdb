"""Apply the ``--config-dir`` switch / ``TDB_CONFIG_DIR`` override."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from tdb.persist import CONFIG_DIR_ENV, config_dir, config_dir_overridden
from tdb.tool_env import apply_tool_redirects


# The dir apply_config_dir last created/checked, so a repeat call for the
# same dir does not mkdir or warn again. Tool redirects are reapplied
# (they only fill unset variables, so that is a no-op the second time).
_applied: Path | None = None


def apply_config_dir(raw: str | None) -> Path | None:
    """Make ``raw`` (``--config-dir``) the active config dir for this
    process and its children via ``TDB_CONFIG_DIR``.

    The flag beats a pre-existing env var; the env var alone is honored
    too. Either way the value is made absolute (``~`` expanded) so
    children with another cwd, and tools that reject relative paths
    (Go's GOCACHE), see the same dir. Returns the active dir when
    overridden, else ``None``. A dir that cannot be created or is not
    writable only warns: config-dir writes elsewhere tolerate OSError
    already. Safe to call more than once.
    """
    global _applied
    if raw:
        os.environ[CONFIG_DIR_ENV] = raw
    if not config_dir_overridden():
        return None
    os.environ[CONFIG_DIR_ENV] = os.path.abspath(
        os.path.expanduser(os.environ[CONFIG_DIR_ENV])
    )
    d = config_dir()
    if d != _applied:
        _applied = d
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"tdb: warning: cannot create config dir {d}: {exc}", file=sys.stderr)
        else:
            if not os.access(d, os.W_OK):
                print(f"tdb: warning: config dir {d} is not writable", file=sys.stderr)
    # Tools tdb runs inherit os.environ: point their caches/history here
    # too, so nothing they write lands under $HOME.
    apply_tool_redirects(d)
    return d

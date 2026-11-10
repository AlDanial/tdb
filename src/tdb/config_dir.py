"""Apply the ``--config-dir`` switch / ``TDB_CONFIG_DIR`` override."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from tdb.persist import CONFIG_DIR_ENV, config_dir, config_dir_overridden


def apply_config_dir(raw: str | None) -> Path | None:
    """Make ``raw`` (``--config-dir``) the active config dir for this
    process and its children via ``TDB_CONFIG_DIR``.

    The flag beats a pre-existing env var; the env var alone is honored
    too. Returns the active dir when overridden, else ``None``. A dir that
    cannot be created only warns: config-dir writes elsewhere tolerate
    OSError already.
    """
    if raw:
        os.environ[CONFIG_DIR_ENV] = os.path.abspath(os.path.expanduser(raw))
    if not config_dir_overridden():
        return None
    d = config_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"tdb: warning: cannot create config dir {d}: {exc}", file=sys.stderr)
    return d

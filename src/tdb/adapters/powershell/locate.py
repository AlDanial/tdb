"""Find the pwsh interpreter and the PSES module directory.

PSES precedence (spec "Language profile, registry, CLI"):
  1. {"adapters": {"pses": DIR}} from tdb's config (the `override` arg)
  2. $TDB_PSES_PATH
  3. the newest VS Code PowerShell extension's bundled copy
DIR may be the module directory itself (contains Start-EditorServices.ps1)
or the unzip root one level above it.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Mapping

PSES_ENV_VAR = "TDB_PSES_PATH"
PSES_RELEASE = "v4.7.0"
START_SCRIPT = "Start-EditorServices.ps1"
_MODULE_DIR = "PowerShellEditorServices"

PWSH_HINT = (
    "pwsh (PowerShell 7) not found on PATH — install it from "
    "https://aka.ms/powershell, or set "
    '{"adapters": {"pwsh": "/path/to/pwsh"}} in tdb\'s config.json'
)

PSES_HINT = (
    "PowerShell Editor Services (PSES) not found. Download "
    f"PowerShellEditorServices.zip from https://github.com/PowerShell/"
    f"PowerShellEditorServices/releases/tag/{PSES_RELEASE}, unzip it, and "
    "point tdb at the PowerShellEditorServices directory with "
    '{"adapters": {"pses": "/path/to/PowerShellEditorServices"}} in '
    f"config.json or the {PSES_ENV_VAR} environment variable. tdb also "
    "finds the copy bundled with the VS Code PowerShell extension "
    "(~/.vscode/extensions/ms-vscode.powershell-*/modules)"
)

_VSCODE_DIRS = (".vscode", ".vscode-insiders", ".vscode-server")
_EXT_RE = re.compile(r"^ms-vscode\.powershell-(?P<ver>[\d.]+)")


def find_pwsh(override: str | None) -> str:
    if override:
        if os.path.isfile(override):
            return override
        raise FileNotFoundError(f"pwsh not found at {override!r} — {PWSH_HINT}")
    found = shutil.which("pwsh")
    if found is None:
        raise FileNotFoundError(PWSH_HINT)
    return found


def _module_dir(candidate: Path) -> Path | None:
    """`candidate` or its PowerShellEditorServices child, if it holds the
    start script."""
    if (candidate / START_SCRIPT).is_file():
        return candidate
    nested = candidate / _MODULE_DIR
    if (nested / START_SCRIPT).is_file():
        return nested
    return None


def _version_key(name: str) -> tuple[int, ...]:
    m = _EXT_RE.match(name)
    if m is None:
        return ()
    return tuple(int(p) for p in m.group("ver").split(".") if p.isdigit())


def _vscode_candidates(home: Path) -> list[Path]:
    """Extension module dirs, newest extension version first."""
    found: list[tuple[tuple[int, ...], Path]] = []
    for vs in _VSCODE_DIRS:
        ext_root = home / vs / "extensions"
        try:
            if not ext_root.is_dir():
                continue
            entries = list(ext_root.iterdir())
        except OSError:
            # Unreadable extensions dir (e.g. permission denied) — treat as
            # no candidates here and keep scanning the other VS Code roots.
            continue
        for entry in entries:
            key = _version_key(entry.name)
            if key:
                found.append((key, entry / "modules" / _MODULE_DIR))
    found.sort(key=lambda kv: kv[0], reverse=True)
    return [p for _, p in found]


def find_pses(
    override: str | None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    if override:
        d = _module_dir(Path(override))
        if d is None:
            raise FileNotFoundError(
                f"{START_SCRIPT} not found under {override!r} — {PSES_HINT}"
            )
        return d
    env_dir = env.get(PSES_ENV_VAR)
    if env_dir:
        d = _module_dir(Path(env_dir))
        if d is None:
            raise FileNotFoundError(
                f"{START_SCRIPT} not found under {PSES_ENV_VAR}={env_dir!r} — {PSES_HINT}"
            )
        return d
    for candidate in _vscode_candidates(home):
        d = _module_dir(candidate)
        if d is not None:
            return d
    raise FileNotFoundError(PSES_HINT)

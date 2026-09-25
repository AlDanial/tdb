"""Locating and describing the native debuggers tdb drives over DAP:
``gdb`` (``gdb -i dap``) and ``lldb-dap``.

Two consumers:

* ``tdb --info`` reports where each one lives and what version it is
  (``native_debugger_report``).
* ``--adapter /full/path/to/{gdb,lldb-dap,dlv}`` — the registry maps
  the path's basename back to an adapter id
  (``adapter_id_for_executable``) so the explicit executable wins over
  anything on ``$PATH`` or in ``config.json``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import PureWindowsPath

# Adapters `--adapter` accepts as an executable path. Order matters for
# prefix matching only in that no id is a prefix of another; listed
# longest-first anyway for safety.
PATH_ADAPTER_IDS: tuple[str, ...] = ("lldb-dap", "gdb", "dlv")

# The native debuggers `tdb --info` reports on.
NATIVE_ADAPTER_IDS: tuple[str, ...] = ("gdb", "lldb-dap")

_VERSION_TIMEOUT_S = 5.0


def is_executable_path(value: str) -> bool:
    """True when an ``--adapter`` value names a file rather than an adapter
    id. Any path separator qualifies; both ``/`` and ``\\`` count so a
    Windows path is recognized regardless of the host's ``os.sep``."""
    return "/" in value or "\\" in value


def _basename(path: str) -> str:
    # PureWindowsPath splits on both separators, so a Windows-style path
    # given on a POSIX host still yields the right basename.
    name = PureWindowsPath(path).name if "\\" in path else os.path.basename(path)
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name


def adapter_id_for_executable(path: str) -> str | None:
    """Map ``/usr/bin/gdb-multiarch`` -> ``"gdb"``, ``.../lldb-dap-21`` ->
    ``"lldb-dap"``, ``~/go/bin/dlv`` -> ``"dlv"``; None when the basename
    is not a flavor of one of PATH_ADAPTER_IDS.

    Matches an exact id or ``<id>`` followed by a ``-``/``.``/digit
    (distro-versioned builds like ``lldb-dap-21`` and variants like
    ``gdb-multiarch``), but not an arbitrary prefix (``gdbserver`` is
    not gdb)."""
    name = _basename(path)
    for adapter_id in PATH_ADAPTER_IDS:
        if name == adapter_id:
            return adapter_id
        if name.startswith(adapter_id):
            rest = name[len(adapter_id) :]
            if rest[0] in "-." or rest[0].isdigit():
                return adapter_id
    return None


def find_native_debugger(
    adapter_id: str, adapter_paths: dict[str, str] | None
) -> str | None:
    """The executable tdb would run for ``adapter_id``: the config.json
    ``adapters`` override when set, else the first hit on PATH."""
    override = (adapter_paths or {}).get(adapter_id)
    if override:
        return override
    return shutil.which(adapter_id)


def debugger_version(executable: str) -> str | None:
    """First non-blank line of ``<executable> --version`` (stdout, then
    stderr), or None if it can't be run. Bounded by a timeout so a wedged
    debugger never stalls ``tdb --info``."""
    try:
        proc = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_VERSION_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for stream in (proc.stdout, proc.stderr):
        for line in (stream or "").splitlines():
            if line.strip():
                return line.strip()
    return None


def native_debugger_report(adapter_paths: dict[str, str] | None) -> str:
    """Lines for ``tdb --info``, one per native debugger:

    gdb                          : /usr/bin/gdb
                                   GNU gdb (Ubuntu 17.1-2ubuntu1) 17.1
    """
    lines: list[str] = []
    for adapter_id in NATIVE_ADAPTER_IDS:
        label = f"{adapter_id:<29}: "
        exe = find_native_debugger(adapter_id, adapter_paths)
        if exe is None:
            lines.append(f"{label}not found on PATH")
            continue
        version = debugger_version(exe) or "version unknown"
        lines.append(f"{label}{exe}")
        lines.append(f"{' ' * len(label)}{version}")
    return "\n".join(lines)

"""Locating and describing the native debuggers tdb drives over DAP:
``gdb`` (``gdb -i dap``) and ``lldb-dap``.

Two consumers:

* ``tdb --info`` (``tdb.info``) reports where each one lives and what
  version it is; the same find + ``--version`` probe serves the
  interpreters tdb spawns (perl, ruby, bash, ...).
* ``--adapter /full/path/to/{gdb,lldb-dap,dlv}`` — the registry maps
  the path's basename back to an adapter id
  (``adapter_id_for_executable``) so the explicit executable wins over
  anything on ``$PATH`` or in ``config.json``.
"""

from __future__ import annotations

import os
import re
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
    adapter_id: str, adapter_paths: dict[str, str | None] | None
) -> str | None:
    """The executable tdb would run for ``adapter_id``: the config.json
    ``adapters`` override when set (a null entry counts as unset), else
    the first hit on PATH. Works for any executable name, not just the
    native debuggers."""
    override = (adapter_paths or {}).get(adapter_id)
    if override:
        return override
    return shutil.which(adapter_id)


def version_output(executable: str, args: tuple[str, ...] = ("--version",)) -> str | None:
    """Combined stdout + stderr of ``<executable> *args``, or None if it
    can't be run. Bounded by a timeout so a wedged tool never stalls
    ``tdb --info``."""
    try:
        proc = subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_VERSION_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (proc.stdout or "") + (proc.stderr or "")


def debugger_version(executable: str) -> str | None:
    """First non-blank line of ``<executable> --version`` (stdout, then
    stderr), or None if it can't be run."""
    output = version_output(executable)
    if output is None:
        return None
    for line in output.splitlines():
        if line.strip():
            return line.strip()
    return None


# "17.1", "5.40.1", or bash's "5.3.9(1)-release": the first dotted number
# in a --version line, plus bash's "(patch)-status" suffix when present.
_VERSION_RE = re.compile(r"(\d+(?:\.\d+)+)(\(\d+\)-[A-Za-z]+)?")


def version_number(line: str | None) -> str | None:
    """The version number inside a ``--version`` line, or None.

    ``"GNU gdb (Ubuntu 17.1-2ubuntu1) 17.1"`` -> ``"17.1"``;
    ``"This is perl 5, version 40, subversion 1 (v5.40.1) ..."`` ->
    ``"5.40.1"``; ``"GNU bash, version 5.3.9(1)-release (...)"`` ->
    ``"5.3.9(1)-release"``."""
    if not line:
        return None
    m = _VERSION_RE.search(line)
    return m.group(0) if m else None


def tool_version_number(
    executable: str, args: tuple[str, ...] = ("--version",)
) -> str | None:
    """The first version number in the output of ``<executable> *args``
    (``dlv version`` puts it on the second line), or None."""
    for line in (version_output(executable, args) or "").splitlines():
        found = version_number(line)
        if found:
            return found
    return None

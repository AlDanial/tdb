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

import glob
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

# gdb's DAP interpreter (`gdb -i dap`) arrived in GDB 14. Older builds
# answer `-i dap` with "Interpreter `dap' unrecognized" and exit 1 --
# RHEL 8's stock gdb is 8.2, RHEL 9's is 10.2. Keep in sync with
# tdb.info.MINIMUM_VERSIONS["gdb"].
GDB_DAP_MIN_VERSION: tuple[int, ...] = (14,)

# gdb's exact wording (gdb/main.c) when `-i NAME` names no registered
# interpreter. The `dap` interpreter exists only in GDB >= 14 and only
# when gdb was built with Python (gdb/python/py-dap.c registers it), so
# a new-enough gdb configured --without-python prints this too.
GDB_NO_DAP_INTERPRETER = "Interpreter `dap' unrecognized"

# `gdb -i dap` on a capable gdb prints its banner as DAP output events
# and exits at EOF on stdin, well under a second; allow for slow hosts.
_DAP_PROBE_TIMEOUT_S = 10.0

# Where Red Hat's Software Collections install newer toolchains on
# RHEL 7/8/9 and their rebuilds (Rocky, Alma, Oracle, CentOS Stream):
# `dnf install gcc-toolset-14-gdb` puts a DAP-capable gdb 14.2 here
# without touching /usr/bin/gdb, and it runs by absolute path (no
# `scl enable` needed). tdb consults these only when the gdb it would
# otherwise run is missing or provably too old.
TOOLSET_GDB_GLOBS: tuple[str, ...] = (
    "/opt/rh/gcc-toolset-*/root/usr/bin/gdb",
    "/opt/rh/devtoolset-*/root/usr/bin/gdb",
)


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


def version_output(
    executable: str, args: tuple[str, ...] = ("--version",)
) -> str | None:
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


def tool_version_tuple(
    executable: str, args: tuple[str, ...] = ("--version",)
) -> tuple[int, ...] | None:
    """``(8, 2)`` for a gdb whose ``--version`` says ``8.2-20.el8``; None
    when the tool can't be run or prints no version number."""
    number = tool_version_number(executable, args)
    if number is None:
        return None
    m = re.match(r"\d+(?:\.\d+)*", number)
    return tuple(int(n) for n in m.group(0).split(".")) if m else None


def gdb_supports_dap(executable: str) -> bool | None:
    """Whether ``executable -i dap`` starts gdb's DAP interpreter.

    False when gdb answers with GDB_NO_DAP_INTERPRETER (too old, or built
    without Python); True when it starts and exits cleanly at EOF; None
    when the probe is inconclusive (gdb can't be run, times out, or
    fails for some other reason). ``-nx`` keeps the user's gdbinit out
    of it and ``-batch`` plus a closed stdin guarantees gdb exits instead
    of waiting for DAP requests from the terminal."""
    try:
        proc = subprocess.run(
            [executable, "-nx", "-batch", "-i", "dap"],
            capture_output=True,
            text=True,
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=_DAP_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (proc.stdout or "") + (proc.stderr or "")
    if GDB_NO_DAP_INTERPRETER in output:
        return False
    return True if proc.returncode == 0 else None


def find_dap_capable_gdb() -> tuple[str, tuple[int, ...]] | None:
    """The newest gdb at or above GDB_DAP_MIN_VERSION among the Software
    Collections toolsets (TOOLSET_GDB_GLOBS), as ``(path, version)``, or
    None. Candidates whose version can't be read are skipped: this is a
    fallback and must never pick something it can't vouch for."""
    best: tuple[str, tuple[int, ...]] | None = None
    for pattern in TOOLSET_GDB_GLOBS:
        for path in sorted(glob.glob(pattern)):
            version = tool_version_tuple(path)
            if version is None or version < GDB_DAP_MIN_VERSION:
                continue
            if best is None or version > best[1]:
                best = (path, version)
    return best

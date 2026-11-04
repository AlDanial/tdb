"""Attach to a running Python process by pid (`tdb -a PID`).

The target needs no preparation. tdb's adapter waits for a connection, and
code injected into the target dials back to it, so quitting tdb leaves no
open port or helper process behind. This module holds everything specific
to reaching the target; it imports no controller or UI code.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass

from tdb.languages.base import LanguageNotSupportedError

MINIMUM_VERSION = (3, 11)

_PYTHON_NAME = re.compile(r"^(python|pypy)(\d+(\.\d+)?t?)?(\.exe)?$", re.IGNORECASE)

_PROBE_TIMEOUT = 10.0
_PROBE_CODE = (
    "import sys;"
    "print(sys.implementation.name, sys.version_info[0], sys.version_info[1],"
    " hasattr(sys, 'remote_exec'))"
)


class PidAttachError(Exception):
    """Injection failed; `str()` is the message shown to the user."""


@dataclass(frozen=True)
class TargetInfo:
    """What tdb knows about the process it is attaching to.

    `interpreter` is an executable of the target's own Python, or None
    when it can't be found or safely run; `sys.remote_exec` needs a
    caller of the same version, so None rules that method out.
    """

    pid: int
    interpreter: str | None
    has_remote_exec: bool


def is_python_executable(path: str) -> bool:
    """True if `path` is named like a Python interpreter."""
    return _PYTHON_NAME.match(os.path.basename(path)) is not None


def pid_exists(pid: int) -> bool:
    """True if `pid` is running, or on Windows, where this can't tell:
    `os.kill(pid, 0)` there terminates the process."""
    if sys.platform == "win32":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _exe_of_pid(pid: int) -> str | None:
    if sys.platform != "linux":
        return None
    return os.path.realpath(f"/proc/{pid}/exe")


def probe_target(pid: int, python: str | None = None) -> TargetInfo:
    """Find the target's interpreter and what it supports.

    `python` (the --python flag) overrides the lookup. A binary not named
    like Python is never run: it may embed an interpreter (uWSGI), and
    these arguments mean nothing to it. Such targets are still attachable
    through debugpy's injection, so they come back as unknown, not as an
    error.

    Raises LanguageNotSupportedError for a missing pid or a known
    unsupported interpreter.
    """
    if not pid_exists(pid):
        raise LanguageNotSupportedError(f"no process with pid {pid}")
    unknown = TargetInfo(pid, None, False)
    path = python or _exe_of_pid(pid)
    if path is None or not os.path.isfile(path) or not is_python_executable(path):
        return unknown
    try:
        result = subprocess.run(
            [path, "-I", "-c", _PROBE_CODE],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
        )
        name, major, minor, remote_exec = result.stdout.split()
        version = (int(major), int(minor))
    except (OSError, ValueError, subprocess.SubprocessError):
        return unknown
    if name != "cpython":
        raise LanguageNotSupportedError(
            f"pid {pid} runs {name}; attaching by pid needs CPython"
        )
    if version < MINIMUM_VERSION:
        raise LanguageNotSupportedError(
            f"pid {pid} runs Python {major}.{minor}; attaching by pid needs "
            f"Python {MINIMUM_VERSION[0]}.{MINIMUM_VERSION[1]} or newer"
        )
    return TargetInfo(pid, path, remote_exec == "True")

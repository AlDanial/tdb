"""Redirect the write locations of the tools tdb runs into the config dir.

When ``--config-dir``/``TDB_CONFIG_DIR`` is in effect, the adapters,
debuggers, build steps and debuggees tdb starts must not write under
``$HOME`` either (a read-only ``$HOME`` is the motivating case). Every
child process inherits ``os.environ``, so filling these variables in
there once, at startup, covers all of them.

Only *write* locations (caches, history, state) are redirected — never
a place a tool reads its installation or the user's settings from
(``HOME``, ``XDG_CONFIG_HOME``, ``XDG_DATA_HOME``, ``GOROOT``,
``GOPATH``, ``CARGO_HOME``, ``RUSTUP_HOME``, ``GEM_HOME``,
``OPAMROOT``), or the toolchain would no longer be found. A variable
the user already set (non-empty) always wins.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from pathlib import Path

# (variable, path parts under DIR/tools). Evidence for each entry:
#   XDG_CACHE_HOME   go os.UserCacheDir (GOCACHE default), pip, pwsh
#                    StartupProfileData cache, debuginfod client, gdb
#                    index-cache, lldb module/index caches.
#   XDG_STATE_HOME   rdbg history default ${XDG_STATE_HOME}/rdbg/history
#                    (debug gem lib/debug/console.rb).
#   GOCACHE          go build cache (dlv debug runs `go build`).
#   GOMODCACHE       module downloads; default is $GOPATH/pkg/mod, so it
#                    is only filled while GOPATH is unset (~/go).
#   DEBUGINFOD_CACHE_PATH  debuginfod-client(7) cache, used by gdb.
#   GDBHISTFILE      gdb history file when `set history save on`.
#   RUBY_DEBUG_HISTORY_FILE  rdbg history (debug gem config.rb).
#   DUNE_CACHE_ROOT  dune shared cache when a debuggee/build runs dune.
#   PIP_CACHE_DIR    pip cache; XDG does not apply on Windows/macOS.
_TABLE: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("XDG_CACHE_HOME", ("cache",)),
    ("XDG_STATE_HOME", ("state",)),
    ("GOCACHE", ("go", "build")),
    ("GOMODCACHE", ("go", "mod")),
    ("DEBUGINFOD_CACHE_PATH", ("gdb", "debuginfod")),
    ("GDBHISTFILE", ("gdb", "history")),
    ("RUBY_DEBUG_HISTORY_FILE", ("ruby", "rdbg_history")),
    ("DUNE_CACHE_ROOT", ("ocaml", "dune")),
    ("PIP_CACHE_DIR", ("pip",)),
)


def tool_redirects(config_dir: Path) -> dict[str, str]:
    """The full redirect table for ``config_dir``, all under
    ``config_dir/tools``. Independent of the current environment."""
    tools = Path(config_dir) / "tools"
    return {name: str(tools.joinpath(*parts)) for name, parts in _TABLE}


def apply_tool_redirects(
    config_dir: Path, environ: MutableMapping[str, str] = os.environ
) -> dict[str, str]:
    """Fill each redirect variable that is absent or empty in ``environ``.

    ``GOMODCACHE`` is skipped when ``GOPATH`` is set: the user's module
    cache lives there. Creates no directories (each tool creates its
    own). Returns the variables it set.
    """
    applied: dict[str, str] = {}
    for name, value in tool_redirects(config_dir).items():
        if environ.get(name):
            continue
        if name == "GOMODCACHE" and environ.get("GOPATH"):
            continue
        environ[name] = value
        applied[name] = value
    return applied

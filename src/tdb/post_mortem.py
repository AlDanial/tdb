"""Post-mortem support: snapshot a crashed program and launch tdb on it.

Usage from user code:
    import sys, tdb
    sys.excepthook = tdb.exception_hook

When an uncaught exception propagates out of the program, the hook snapshots
every frame in the traceback (locals, plus one-level-recursive children of
containers and objects with __dict__), writes the snapshot to a temp file,
then launches `python -m tdb --post-mortem <file>` so the TUI takes over the
terminal. The original interpreter state is frozen — no stepping, no eval,
but full stack navigation + variable inspection at crash time.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import traceback as _tb_mod
from types import TracebackType
from typing import Any

log = logging.getLogger(__name__)


# Limits applied while snapshotting to keep the file small and guard against
# pathological object graphs (deep recursion, huge containers, cycles).
_MAX_DEPTH = 5
_MAX_CHILDREN = 50
_MAX_REPR = 200


def exception_hook(
    exc_type: type[BaseException],
    exc_value: BaseException,
    tb: TracebackType | None,
) -> None:
    """sys.excepthook replacement: snapshots the traceback and opens tdb."""
    # Always print the traceback first — if tdb fails to launch, the user
    # still has the record, and the scrollback shows what happened before
    # the TUI takes over.
    sys.__excepthook__(exc_type, exc_value, tb)

    if tb is None:
        return

    # Only take over if we have a real tty on both ends. Running tdb under
    # `python -c '...' | cat` or from cron would otherwise hang.
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return
    except (AttributeError, ValueError):
        return

    try:
        snapshot = _make_snapshot(exc_type, exc_value, tb)
    except Exception:
        log.exception("tdb.exception_hook: snapshot failed")
        return

    # If the program also used tdb.breakpoint() and the user just quit that
    # session, the live tdb subprocess may still be tearing down its TUI
    # (alt-screen exit, cursor restore, etc.) when this hook fires. Spawning
    # the post-mortem TUI now would have two full-screen tdb processes
    # rendering to the same tty — they race on output and on the user's
    # keystrokes, leaving the terminal corrupted. Wait for the live tdb to
    # finish exiting first. The wait is bounded so a still-attached tdb
    # (user pressed `c` past the exception instead of quitting) doesn't
    # block the post-mortem indefinitely.
    try:
        from tdb.breakpoint_hook import _subprocess as _bp_proc
        if _bp_proc is not None and _bp_proc.poll() is None:
            try:
                _bp_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                log.warning(
                    "tdb.exception_hook: live tdb.breakpoint() subprocess "
                    "still running after 10s — proceeding with post-mortem "
                    "anyway; the terminal display may be corrupted."
                )
    except Exception:
        log.exception("tdb.exception_hook: error checking breakpoint subprocess")

    fd, path = tempfile.mkstemp(prefix="tdb-postmortem-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(snapshot, f)
        subprocess.run(
            [sys.executable, "-m", "tdb", "--post-mortem", path],
            stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr,
        )
    except Exception:
        log.exception("tdb.exception_hook: failed to launch tdb")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _make_snapshot(
    exc_type: type[BaseException],
    exc_value: BaseException,
    tb: TracebackType,
) -> dict[str, Any]:
    """Build a JSON-serializable snapshot of the crash."""
    # Collect traceback entries from outermost to innermost (tb.tb_next chain).
    tb_entries: list[TracebackType] = []
    cur: TracebackType | None = tb
    while cur is not None:
        tb_entries.append(cur)
        cur = cur.tb_next

    # DAP stack frames go innermost-first (index 0 = deepest = crash site).
    tb_entries.reverse()

    variables: dict[str, list[dict[str, Any]]] = {}
    # Cache ref assignment by object identity so cycles / shared objects
    # don't explode the snapshot.
    seen: dict[int, int] = {}
    ref_counter = [1000]

    def next_ref() -> int:
        ref_counter[0] += 1
        return ref_counter[0]

    def snapshot_value(obj: Any, depth: int) -> tuple[str, str, int]:
        """Return (value_repr, type_name, variables_reference).

        If variables_reference > 0, children are registered in `variables`
        under str(ref) and the caller can render the value as expandable.
        """
        try:
            type_name = type(obj).__name__
        except Exception:
            type_name = "?"
        try:
            value_repr = repr(obj)
        except Exception:
            value_repr = f"<unrepr-able {type_name}>"
        if len(value_repr) > _MAX_REPR:
            value_repr = value_repr[: _MAX_REPR - 3] + "..."

        if depth >= _MAX_DEPTH:
            return value_repr, type_name, 0

        oid = id(obj)
        if oid in seen:
            return value_repr, type_name, seen[oid]

        children = _collect_children(obj)
        if not children:
            return value_repr, type_name, 0

        ref = next_ref()
        seen[oid] = ref
        # Pre-register an empty list so self-referential children don't
        # cause unbounded recursion before we populate it.
        variables[str(ref)] = []
        entries: list[dict[str, Any]] = []
        for child_name, child_val in children[:_MAX_CHILDREN]:
            cv, ct, cref = snapshot_value(child_val, depth + 1)
            entries.append({
                "name": child_name,
                "value": cv,
                "type": ct,
                "variablesReference": cref,
            })
        if len(children) > _MAX_CHILDREN:
            entries.append({
                "name": f"... {len(children) - _MAX_CHILDREN} more",
                "value": "",
                "type": "",
                "variablesReference": 0,
            })
        variables[str(ref)] = entries
        return value_repr, type_name, ref

    frames_out: list[dict[str, Any]] = []
    for i, tb_entry in enumerate(tb_entries):
        pyframe = tb_entry.tb_frame
        lineno = tb_entry.tb_lineno
        funcname = pyframe.f_code.co_name
        filename = pyframe.f_code.co_filename

        locals_ref = next_ref()
        variables[str(locals_ref)] = []
        entries: list[dict[str, Any]] = []
        # Preserve insertion order from the frame's locals dict.
        for name, val in pyframe.f_locals.items():
            cv, ct, cref = snapshot_value(val, depth=1)
            entries.append({
                "name": name,
                "value": cv,
                "type": ct,
                "variablesReference": cref,
            })
        variables[str(locals_ref)] = entries

        frames_out.append({
            "id": 1 + i,
            "filename": filename,
            "lineno": lineno,
            "funcname": funcname,
            "scopes": [
                {"name": "Locals", "variablesReference": locals_ref},
            ],
        })

    tb_text = "".join(_tb_mod.format_exception(exc_type, exc_value, tb))

    return {
        "version": 1,
        "exception": {
            "type": getattr(exc_type, "__name__", "Exception"),
            "message": str(exc_value),
            "traceback_text": tb_text,
        },
        "frames": frames_out,
        "variables": variables,
    }


def _collect_children(obj: Any) -> list[tuple[str, Any]]:
    """Return [(name, value)] pairs describing obj's expandable children.

    Empty list means obj is a leaf (no useful expansion).
    """
    # Ordered by common-case first. dict/list/tuple/set come before __dict__
    # since those types also have __dict__ on subclasses but we want indexed
    # access shown.
    if isinstance(obj, dict):
        out: list[tuple[str, Any]] = []
        for k, v in obj.items():
            try:
                key_str = repr(k)
            except Exception:
                key_str = "<unrepr-able key>"
            if len(key_str) > 60:
                key_str = key_str[:57] + "..."
            out.append((key_str, v))
        return out
    if isinstance(obj, (list, tuple)):
        return [(f"[{i}]", v) for i, v in enumerate(obj)]
    if isinstance(obj, (set, frozenset)):
        return [(f"<{i}>", v) for i, v in enumerate(obj)]
    # Plain objects with attributes: prefer __dict__ when present. Skip
    # builtins, modules, and types themselves (vars(type) is huge and
    # rarely useful in a post-mortem).
    if isinstance(obj, (type, type(sys))):  # type(sys) == ModuleType
        return []
    try:
        attrs = vars(obj)
    except TypeError:
        return []
    return [(str(k), v) for k, v in attrs.items()]

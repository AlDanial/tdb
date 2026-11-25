"""Start-up hook for ``tdb --trace``.

tdb puts this directory first on the traced program's ``PYTHONPATH``, so
every Python process of the program imports this file at start-up.  When
``TDB_TRACE_CONFIG`` is set it starts the tracer (``_tdb_trace_agent``)
or, on an interpreter that cannot be traced, leaves a marker file in the
``TDB_TRACE_PARTS`` directory.  It then takes this directory off
``sys.path`` and imports the next ``sitecustomize`` on it, so a distro or
venv ``sitecustomize`` still runs.

Rules: stdlib only, never imports tdb, never raises, and must parse on
Python 3.7+ (no walrus, no ``match``, no f-string ``=``, no
``from __future__ import annotations``): old interpreters load it too, and
must reach the version check.
"""

import os
import sys

_AGENT = "_tdb_trace_agent"


def skip_reason(version, gil_enabled):
    """Why this interpreter cannot be traced, or None if it can."""
    if tuple(version[:2]) < (3, 12):
        return f"python {version[0]}.{version[1]} is older than 3.12"
    if gil_enabled is False:
        return "free-threaded build without the GIL"
    return None


def _write_marker(kind, fields):
    """Write ``PARTS/<kind>-<pid>.json``; never raises."""
    try:
        parts = os.environ.get("TDB_TRACE_PARTS")
        if not parts:
            return
        import json

        record = {"marker": kind, "pid": os.getpid(), "argv": list(sys.argv)}
        record.update(fields)
        data = json.dumps(record, separators=(",", ":")).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        name = f"{kind}-{os.getpid()}"
        for n in range(1, 100):
            path = os.path.join(parts, name + (f"-{n}" if n > 1 else "") + ".json")
            try:
                fd = os.open(path, flags, 0o666)
            except FileExistsError:
                continue
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            return
    except BaseException:
        pass


def _start_tracing():
    gil_check = getattr(sys, "_is_gil_enabled", None)
    reason = skip_reason(sys.version_info, gil_check() if gil_check else None)
    if reason is not None:
        _write_marker(
            "skipped",
            {
                "python": "%d.%d.%d" % tuple(sys.version_info[:3]),
                "reason": reason,
            },
        )
        return
    if _AGENT in sys.modules:
        # Another copy of this hook (a second tdb's agent directory later
        # on PYTHONPATH) already started a tracer in this process.
        return
    try:
        import json

        with open(os.environ["TDB_TRACE_CONFIG"], "rb") as fh:
            config = json.loads(fh.read().decode("utf-8"))
        import _tdb_trace_agent

        _tdb_trace_agent.start(config)
    except BaseException as exc:
        _write_marker("agent-error", {"error": f"{type(exc).__name__}: {exc}"})


def _is_agent_dir(entry, agent_real):
    try:
        return os.path.normcase(os.path.realpath(entry or os.curdir)) == agent_real
    except BaseException:
        return False


def _drop_agent_dir(agent_real):
    sys.path[:] = [p for p in sys.path if not _is_agent_dir(p, agent_real)]


def _chain(agent_real):
    """Import the next ``sitecustomize`` on sys.path, the way site.py would."""
    me = sys.modules.pop("sitecustomize", None)
    try:
        try:
            import sitecustomize  # noqa: F401  (the next one, not this file)
        except ImportError as exc:
            if exc.name != "sitecustomize":
                raise
    except Exception as err:
        # Report exactly as site.execsitecustomize does without a tracer.
        if sys.flags.verbose:
            sys.excepthook(*sys.exc_info())
        else:
            sys.stderr.write(
                "Error in sitecustomize; set PYTHONVERBOSE for traceback:\n"
                "%s: %s\n" % (err.__class__.__name__, err)
            )
    finally:
        # The importer running this file pops "sitecustomize" from
        # sys.modules when it finishes; if no other one took its place,
        # this module must be there, or site.py reports a KeyError.
        if me is not None and "sitecustomize" not in sys.modules:
            sys.modules["sitecustomize"] = me
        _drop_agent_dir(agent_real)


def _main():
    try:
        agent_real = os.path.normcase(
            os.path.realpath(os.path.dirname(os.path.abspath(__file__)))
        )
    except BaseException:
        return
    try:
        if os.environ.get("TDB_TRACE_CONFIG"):
            _start_tracing()
    except BaseException:
        pass
    try:
        _drop_agent_dir(agent_real)
        _chain(agent_real)
    except BaseException:
        pass


if __name__ == "sitecustomize":
    _main()

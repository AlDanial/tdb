"""tdb as pytest's debugger: ``pytest --pdb --pdbcls=tdb.pytest_debugger:Debugger``.

pytest instantiates the ``--pdbcls`` class (wrapped in its own subclass) and
drives it the way it drives ``pdb.Pdb``:

``--pdb`` (test failure / error)
    ``reset()`` then ``interaction(None, tb_or_exc)``. The failing frames
    have already finished executing, so this uses the same frozen-snapshot
    post-mortem TUI as ``tdb.exception_hook``: full stack navigation and
    variable inspection at the point of failure, no eval or stepping. Quit
    tdb to let pytest carry on with the next test.

``breakpoint()`` / ``pytest.set_trace()`` inside a test
    ``set_trace(frame)``. Opens a live tdb attached to the pytest process
    (the ``tdb.breakpoint()`` mechanism) paused on the next line of the
    test. Step, inspect, and continue as usual; quitting tdb resumes the
    test run.

``--trace``
    ``runcall(test_function)``. Same live tdb, paused on the first line of
    each test.

When stdin/stdout are not a terminal (CI, xdist workers) tdb cannot draw
its TUI and every entry point falls back to plain ``pdb`` behavior.
"""

from __future__ import annotations

import logging
import os
import pdb
import sys
from types import FrameType, TracebackType
from typing import Any, Callable

from tdb import breakpoint_hook, post_mortem

log = logging.getLogger(__name__)

__all__ = ["Debugger", "Pdb"]


class Debugger(pdb.Pdb):
    """``pdb.Pdb`` subclass that shows failures and breakpoints in tdb."""

    def interaction(
        self,
        frame: FrameType | None,
        tb_or_exc: TracebackType | BaseException | None,
    ) -> None:
        if tb_or_exc is None or not breakpoint_hook._stdio_is_tty():
            # A live stop reached through bdb (e.g. after a tty-less
            # fallback in runcall) — nothing tdb can draw; behave as pdb.
            super().interaction(frame, tb_or_exc)  # type: ignore[arg-type]
            return

        exc, tb = _split_tb_or_exc(tb_or_exc)
        if tb is None:
            super().interaction(frame, tb_or_exc)  # type: ignore[arg-type]
            return
        if exc is None:
            exc = _find_exception_for(tb)

        try:
            snapshot = post_mortem._make_snapshot(
                type(exc) if exc is not None else None,
                exc,
                tb,
                hide_frame=_is_pytest_internal_frame,
            )
        except Exception:
            log.exception("tdb pytest debugger: snapshot failed; using pdb")
            super().interaction(frame, tb_or_exc)  # type: ignore[arg-type]
            return
        # A tdb attached earlier by breakpoint() in this run still owns the
        # terminal; wait for the user to quit it rather than drawing a
        # second TUI on top of it.
        post_mortem.launch_snapshot(snapshot, live_tdb_wait=None)

    def set_trace(self, frame: FrameType | None = None) -> None:
        if frame is None:
            frame = sys._getframe().f_back
        if not breakpoint_hook.attach_and_pause(stop_at_frame=frame):
            super().set_trace(frame)

    def runcall(self, func: Callable[..., Any], /, *args: Any, **kwds: Any) -> Any:
        return _trace_trampoline(self, func, *args, **kwds)


def _trace_trampoline(
    dbg: pdb.Pdb, func: Callable[..., Any], /, *args: Any, **kwds: Any
) -> Any:
    """Pause on the line below, which tdb recognizes and steps into.

    pydevd can only be told to stop at the next line of an *existing*
    frame, and the test's frame doesn't exist yet. So stop on the call
    line here; tdb's stopped handler sees this function on top of the
    stack (python profile `breakpoint_hook_step_in_frame`) and issues a
    step-in, landing on the first line of the test.
    """
    if not breakpoint_hook.attach_and_pause(stop_at_frame=sys._getframe()):
        return pdb.Pdb.runcall(dbg, func, *args, **kwds)
    return func(*args, **kwds)


# Same spelling as IPython.terminal.debugger:Pdb, for muscle memory.
Pdb = Debugger


def _split_tb_or_exc(
    tb_or_exc: TracebackType | BaseException,
) -> tuple[BaseException | None, TracebackType | None]:
    """pytest passes the exception on Python >= 3.13, the traceback before."""
    if isinstance(tb_or_exc, BaseException):
        return tb_or_exc, tb_or_exc.__traceback__
    return None, tb_or_exc


def _find_exception_for(tb: TracebackType) -> BaseException | None:
    """Recover the exception whose traceback is ``tb`` (Python < 3.13).

    pytest's ``post_mortem(tb)`` only hands over the traceback; its caller
    ``_enter_pdb`` still holds the ``ExceptionInfo``. Look up the stack for
    an exception object owning ``tb`` so the TUI can show its type and
    message. Returns None if it can't be found.
    """
    f = sys._getframe(1)
    while f is not None:
        for value in list(f.f_locals.values()):
            candidates = [value]
            info = getattr(value, "_excinfo", None)  # pytest ExceptionInfo
            if isinstance(info, tuple) and len(info) == 3:
                candidates.append(info[1])
            for c in candidates:
                if isinstance(c, BaseException) and c.__traceback__ is tb:
                    return c
        f = f.f_back
    return None


def _package_dir(modname: str) -> str | None:
    try:
        mod = __import__(modname)
    except ImportError:
        return None
    path = getattr(mod, "__file__", None)
    if not path:
        return None
    return os.path.normcase(os.path.dirname(os.path.abspath(path))) + os.sep


_INTERNAL_DIRS: tuple[str, ...] | None = None


def _is_pytest_internal_frame(frame: FrameType) -> bool:
    """Frames pytest itself leaves out of failure tracebacks."""
    global _INTERNAL_DIRS
    if _INTERNAL_DIRS is None:
        _INTERNAL_DIRS = tuple(
            d for d in (_package_dir("_pytest"), _package_dir("pluggy")) if d
        )
    try:
        if frame.f_locals.get("__tracebackhide__", False):
            return True
    except Exception:
        pass
    # normcase: Windows paths compare case-insensitively.
    filename = os.path.normcase(os.path.abspath(frame.f_code.co_filename))
    return filename.startswith(_INTERNAL_DIRS)

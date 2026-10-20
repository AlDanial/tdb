"""Unit tests for tdb.pytest_debugger (pytest ``--pdbcls`` support).

No TUI is started: the tty check, the post-mortem launcher, and the live
attach are replaced so the tests can observe what the class would do.
"""

from __future__ import annotations

import os
import pdb
import sys

import pytest

import tdb.pytest_debugger as pd
from tdb import breakpoint_hook, post_mortem

pytest_plugins = ["pytester"]


def _raise_and_catch() -> BaseException:
    def inner():
        local_value = [1, 2, 3]  # noqa: F841 - inspected via snapshot
        raise ValueError("boom")

    try:
        inner()
    except ValueError as e:
        return e
    raise AssertionError("unreachable")


@pytest.fixture
def launched(monkeypatch):
    calls: list[tuple[dict, dict]] = []
    monkeypatch.setattr(
        post_mortem, "launch_snapshot", lambda snap, **kw: calls.append((snap, kw))
    )
    return calls


@pytest.fixture
def tty(monkeypatch):
    monkeypatch.setattr(breakpoint_hook, "_stdio_is_tty", lambda: True)


@pytest.fixture
def no_tty(monkeypatch):
    monkeypatch.setattr(breakpoint_hook, "_stdio_is_tty", lambda: False)


# --- helpers ----------------------------------------------------------------


def test_split_accepts_exception_and_traceback():
    exc = _raise_and_catch()
    assert pd._split_tb_or_exc(exc) == (exc, exc.__traceback__)
    assert pd._split_tb_or_exc(exc.__traceback__) == (None, exc.__traceback__)


def test_find_exception_for_pytest_excinfo_local():
    exc = _raise_and_catch()

    class FakeExcInfo:  # mimics pytest's ExceptionInfo._excinfo
        _excinfo = (type(exc), exc, exc.__traceback__)

    excinfo = FakeExcInfo()  # noqa: F841 - found by walking frames
    assert pd._find_exception_for(exc.__traceback__) is exc


def test_find_exception_for_returns_none_when_absent():
    exc = _raise_and_catch()
    tb = exc.__traceback__
    del exc
    assert pd._find_exception_for(tb) is None


def test_internal_frame_detection():
    import _pytest.python

    assert pd._is_pytest_internal_frame(sys._getframe()) is False

    __tracebackhide__ = True  # noqa: F841
    assert pd._is_pytest_internal_frame(sys._getframe()) is True

    # Any frame executing inside the _pytest package counts.
    code = compile("import sys; f = sys._getframe()", _pytest.python.__file__, "exec")
    ns: dict = {}
    exec(code, ns)
    assert pd._is_pytest_internal_frame(ns["f"]) is True


# --- interaction (post-mortem) ------------------------------------------------


def test_interaction_with_exception_launches_snapshot(tty, launched):
    exc = _raise_and_catch()
    pd.Debugger().interaction(None, exc)
    assert len(launched) == 1
    snap, kw = launched[0]
    assert kw == {"live_tdb_wait": None}
    assert snap["exception"]["type"] == "ValueError"
    assert snap["exception"]["message"] == "boom"
    assert snap["frames"][0]["funcname"] == "inner"


def test_interaction_with_traceback_recovers_exception(tty, launched):
    exc = _raise_and_catch()

    class FakeExcInfo:
        _excinfo = (type(exc), exc, exc.__traceback__)

    excinfo = FakeExcInfo()  # noqa: F841
    pd.Debugger().interaction(None, exc.__traceback__)
    snap, _ = launched[0]
    assert snap["exception"]["type"] == "ValueError"
    assert snap["exception"]["message"] == "boom"


def test_interaction_without_tty_falls_back_to_pdb(no_tty, launched, monkeypatch):
    seen = []
    monkeypatch.setattr(pdb.Pdb, "interaction", lambda self, f, t: seen.append((f, t)))
    exc = _raise_and_catch()
    pd.Debugger().interaction(None, exc)
    assert seen == [(None, exc)]
    assert launched == []


def test_interaction_live_frame_falls_back_to_pdb(tty, launched, monkeypatch):
    seen = []
    monkeypatch.setattr(pdb.Pdb, "interaction", lambda self, f, t: seen.append((f, t)))
    frame = sys._getframe()
    pd.Debugger().interaction(frame, None)
    assert seen == [(frame, None)]
    assert launched == []


# --- set_trace / runcall (live attach) -----------------------------------------


def test_set_trace_attaches_at_given_frame(monkeypatch):
    got = []
    monkeypatch.setattr(
        breakpoint_hook, "attach_and_pause", lambda **kw: got.append(kw) or True
    )
    frame = sys._getframe()
    pd.Debugger().set_trace(frame)
    assert got == [{"stop_at_frame": frame}]


def test_set_trace_defaults_to_caller_frame(monkeypatch):
    got = []
    monkeypatch.setattr(
        breakpoint_hook, "attach_and_pause", lambda **kw: got.append(kw) or True
    )
    pd.Debugger().set_trace()
    assert got[0]["stop_at_frame"] is sys._getframe()


def test_set_trace_falls_back_to_pdb_when_attach_fails(monkeypatch):
    monkeypatch.setattr(breakpoint_hook, "attach_and_pause", lambda **kw: False)
    seen = []
    monkeypatch.setattr(
        pdb.Pdb, "set_trace", lambda self, frame=None: seen.append(frame)
    )
    frame = sys._getframe()
    pd.Debugger().set_trace(frame)
    assert seen == [frame]


def test_runcall_pauses_in_trampoline_then_calls(monkeypatch):
    got = []
    monkeypatch.setattr(
        breakpoint_hook, "attach_and_pause", lambda **kw: got.append(kw) or True
    )
    result = pd.Debugger().runcall(lambda a, b=0: a + b, 2, b=3)
    assert result == 5
    (kw,) = got
    assert kw["stop_at_frame"].f_code is pd._trace_trampoline.__code__


def test_runcall_falls_back_to_pdb_when_attach_fails(monkeypatch):
    monkeypatch.setattr(breakpoint_hook, "attach_and_pause", lambda **kw: False)
    seen = []

    def fake_runcall(self, func, *args, **kwds):
        seen.append(func)
        return "pdb"

    monkeypatch.setattr(pdb.Pdb, "runcall", fake_runcall)
    f = lambda: "direct"  # noqa: E731
    assert pd.Debugger().runcall(f) == "pdb"
    assert seen == [f]


def test_pdb_alias():
    assert pd.Pdb is pd.Debugger


# --- through a real pytest run --------------------------------------------------


def test_pytest_pdbcls_post_mortem_end_to_end(pytester, tty, launched):
    """``pytest --pdb --pdbcls=tdb.pytest_debugger:Debugger`` routes each
    failure through pytest's wrapper class into a snapshot of the failing
    test's frames, and the run carries on to the next test."""
    pytester.makepyfile(
        """
        def test_one():
            value = {"k": [1, 2]}
            assert value["k"] == [3]

        def test_two():
            name = "x"
            raise KeyError(name)

        def test_ok():
            pass
        """
    )
    result = pytester.runpytest_inprocess(
        "-p", "no:cacheprovider", "--pdb", "--pdbcls=tdb.pytest_debugger:Debugger"
    )
    result.assert_outcomes(failed=2, passed=1)
    assert [s["frames"][0]["funcname"] for s, _ in launched] == [
        "test_one",
        "test_two",
    ]
    first, second = (s for s, _ in launched)
    assert first["exception"]["type"] == "AssertionError"
    assert second["exception"]["type"] == "KeyError"
    # pytest/pluggy runner frames are trimmed from the stack.
    for snap in (first, second):
        files = {os.path.basename(f["filename"]) for f in snap["frames"]}
        assert files == {"test_pytest_pdbcls_post_mortem_end_to_end.py"}
    names = {
        v["name"]
        for v in first["variables"][
            str(first["frames"][0]["scopes"][0]["variablesReference"])
        ]
    }
    assert "value" in names

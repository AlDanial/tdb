"""Unit tests for tdb.post_mortem (exception_hook + _make_snapshot).

`_make_snapshot` walks a real traceback, so tests raise real exceptions
through small helper functions and inspect the resulting JSON-ready
dict. `exception_hook` is tested with its side effects stubbed
(subprocess.run recorded, tty-ness faked) — no TUI is ever launched.
"""

from __future__ import annotations

import json
import sys

import pytest

import tdb.post_mortem as pm
from tdb.post_mortem import _collect_children, _make_snapshot, exception_hook
from tdb.session.post_mortem_loader import load_post_mortem_into
from tdb.session.state import DebugState


def _capture(fn) -> tuple:
    """Run fn, catch its exception, return (type, value, traceback)."""
    try:
        fn()
    except Exception as e:
        return type(e), e, e.__traceback__
    raise AssertionError("fn did not raise")


def _crash_two_frames():
    def boom():
        local_flag = True  # noqa: F841 — snapshot fodder
        raise ValueError("kaput")

    def outer():
        answer = 42  # noqa: F841
        boom()

    outer()


def _locals_of(snap: dict, frame_idx: int) -> list[dict]:
    ref = snap["frames"][frame_idx]["scopes"][0]["variablesReference"]
    return snap["variables"][str(ref)]


def _entry(entries: list[dict], name: str) -> dict:
    return next(e for e in entries if e["name"] == name)


# --- _make_snapshot: shape --------------------------------------------


def test_snapshot_header_and_frame_order():
    snap = _make_snapshot(*_capture(_crash_two_frames))
    assert snap["version"] == 1
    assert snap["exception"]["type"] == "ValueError"
    assert snap["exception"]["message"] == "kaput"
    assert "ValueError: kaput" in snap["exception"]["traceback_text"]
    # Innermost-first (DAP convention): crash site is frame 0.
    names = [f["funcname"] for f in snap["frames"]]
    assert names[0] == "boom"
    assert names[1] == "outer"
    assert [f["id"] for f in snap["frames"]] == list(range(1, len(names) + 1))
    assert snap["frames"][0]["filename"] == __file__


def test_snapshot_captures_locals_per_frame():
    snap = _make_snapshot(*_capture(_crash_two_frames))
    assert _entry(_locals_of(snap, 0), "local_flag")["value"] == "True"
    answer = _entry(_locals_of(snap, 1), "answer")
    assert (answer["value"], answer["type"]) == ("42", "int")
    assert answer["variablesReference"] == 0  # leaf


def test_snapshot_expands_containers_recursively():
    def crash():
        data = {"point": [3, 4]}  # noqa: F841
        raise RuntimeError("x")

    snap = _make_snapshot(*_capture(crash))
    data = _entry(_locals_of(snap, 0), "data")
    assert data["type"] == "dict"
    assert data["variablesReference"] > 0
    (point,) = snap["variables"][str(data["variablesReference"])]
    assert point["name"] == "'point'"  # dict keys shown as repr
    children = snap["variables"][str(point["variablesReference"])]
    assert [(c["name"], c["value"]) for c in children] == [("[0]", "3"), ("[1]", "4")]


def test_snapshot_object_attributes_via_dict():
    class Config:
        def __init__(self):
            self.host = "localhost"
            self.port = 80

    def crash():
        cfg = Config()  # noqa: F841
        raise RuntimeError("x")

    snap = _make_snapshot(*_capture(crash))
    cfg = _entry(_locals_of(snap, 0), "cfg")
    attrs = snap["variables"][str(cfg["variablesReference"])]
    assert _entry(attrs, "host")["value"] == "'localhost'"
    assert _entry(attrs, "port")["value"] == "80"


# --- _make_snapshot: guards against pathological graphs -----------------


def test_cycles_reuse_the_same_reference():
    def crash():
        selfref = []
        selfref.append(selfref)
        raise RuntimeError("x")

    snap = _make_snapshot(*_capture(crash))
    selfref = _entry(_locals_of(snap, 0), "selfref")
    ref = selfref["variablesReference"]
    (child,) = snap["variables"][str(ref)]
    assert child["variablesReference"] == ref  # cycle folded, not exploded


def test_shared_objects_snapshotted_once():
    def crash():
        shared = {"k": 1}
        a = [shared]  # noqa: F841
        b = [shared]  # noqa: F841
        raise RuntimeError("x")

    snap = _make_snapshot(*_capture(crash))
    locs = _locals_of(snap, 0)
    ref_a = snap["variables"][str(_entry(locs, "a")["variablesReference"])]
    ref_b = snap["variables"][str(_entry(locs, "b")["variablesReference"])]
    assert ref_a[0]["variablesReference"] == ref_b[0]["variablesReference"]


def test_depth_cap_stops_expansion():
    def crash():
        deep = [[[[["bottom"]]]]]  # noqa: F841
        raise RuntimeError("x")

    snap = _make_snapshot(*_capture(crash))
    entry = _entry(_locals_of(snap, 0), "deep")
    hops = 0
    while entry["variablesReference"]:
        (entry,) = snap["variables"][str(entry["variablesReference"])]
        hops += 1
    assert hops < 6  # _MAX_DEPTH=5 cut the tail off
    assert "bottom" in entry["value"]  # value still visible as repr


def test_children_cap_adds_more_marker():
    def crash():
        big = list(range(60))  # noqa: F841
        raise RuntimeError("x")

    snap = _make_snapshot(*_capture(crash))
    big = _entry(_locals_of(snap, 0), "big")
    children = snap["variables"][str(big["variablesReference"])]
    assert len(children) == 51  # 50 kept + "... N more" marker
    assert children[-1]["name"] == "... 10 more"
    assert children[-1]["variablesReference"] == 0


def test_long_reprs_truncated():
    def crash():
        wall = "x" * 500  # noqa: F841
        raise RuntimeError("x")

    snap = _make_snapshot(*_capture(crash))
    value = _entry(_locals_of(snap, 0), "wall")["value"]
    assert len(value) == 200
    assert value.endswith("...")


def test_unreprable_value_survives():
    class Cursed:
        def __repr__(self):
            raise RuntimeError("no repr for you")

    def crash():
        c = Cursed()  # noqa: F841
        raise RuntimeError("x")

    snap = _make_snapshot(*_capture(crash))
    entry = _entry(_locals_of(snap, 0), "c")
    assert entry["value"] == "<unrepr-able Cursed>"


def test_snapshot_is_json_serializable():
    snap = _make_snapshot(*_capture(_crash_two_frames))
    json.dumps(snap)  # must not raise


# --- round trip into the loader -----------------------------------------


def test_snapshot_round_trips_through_loader():
    """End-to-end within one process: crash → snapshot → JSON → loader.
    This is the exact producer/consumer pair used by --post-mortem."""
    snap = json.loads(json.dumps(_make_snapshot(*_capture(_crash_two_frames))))
    state = DebugState()
    load_post_mortem_into(state, snap)
    assert state.stack_frames[0].name == "boom"
    assert state.stack_frames[0].source.path == __file__
    locals_ref = state.scopes[0].variables_reference
    names = [v.name for v in state.variables[locals_ref]]
    assert "local_flag" in names


# --- _collect_children edge cases ----------------------------------------


def test_collect_children_dict_key_reprs():
    long_key = "k" * 80
    children = _collect_children({long_key: 1})
    assert children[0][0].endswith("...")
    assert len(children[0][0]) == 60


def test_collect_children_unreprable_key():
    class BadKey:
        def __repr__(self):
            raise RuntimeError("nope")

        def __hash__(self):
            return 1

    ((name, _),) = _collect_children({BadKey(): 1})
    assert name == "<unrepr-able key>"


def test_collect_children_sequences_and_sets():
    assert _collect_children([10, 20]) == [("[0]", 10), ("[1]", 20)]
    assert _collect_children((10,)) == [("[0]", 10)]
    names = [n for n, _ in _collect_children({"a", "b"})]
    assert names == ["<0>", "<1>"]


def test_collect_children_skips_types_modules_and_leaves():
    assert _collect_children(int) == []  # a type
    assert _collect_children(sys) == []  # a module
    assert _collect_children(42) == []  # vars() raises TypeError

    class Slotted:
        __slots__ = ("x",)

    assert _collect_children(Slotted()) == []


# --- exception_hook gating ------------------------------------------------


class _Tty:
    def __init__(self, is_tty: bool = True):
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


@pytest.fixture
def hook_env(monkeypatch):
    """Fake a tty on stdin/stdout and record subprocess.run calls.
    Returns the list of recorded (argv, snapshot_dict) tuples.

    Patches `pm.sys` (the module's own reference) rather than the global
    `sys.stdout` — pytest's capture manager re-installs its own stdout
    around the test body, which would silently undo a global patch and
    make the hook see isatty() == False. Also silences the hook's
    initial `__excepthook__` traceback print as a side benefit.
    """
    import types

    launches: list[tuple[list, dict]] = []

    def fake_run(argv, **kwargs):
        # Read the snapshot while the temp file still exists.
        with open(argv[-1]) as f:
            launches.append((argv, json.load(f)))

    fake_sys = types.SimpleNamespace(
        __excepthook__=lambda *a: None,
        stdin=_Tty(),
        stdout=_Tty(),
        stderr=sys.stderr,
        executable=sys.executable,
    )
    monkeypatch.setattr(pm, "sys", fake_sys)
    monkeypatch.setattr(pm.subprocess, "run", fake_run)
    return launches


def test_hook_launches_tdb_on_snapshot(hook_env):
    exception_hook(*_capture(_crash_two_frames))
    ((argv, snap),) = hook_env
    assert argv[:4] == [sys.executable, "-m", "tdb", "--post-mortem"]
    assert snap["exception"]["message"] == "kaput"
    # Temp snapshot file is cleaned up after the TUI exits.
    import os

    assert not os.path.exists(argv[-1])


def test_hook_noop_without_traceback(hook_env):
    exception_hook(ValueError, ValueError("no tb"), None)
    assert hook_env == []


def test_hook_noop_when_not_a_tty(hook_env):
    pm.sys.stdout = _Tty(is_tty=False)  # pm.sys is the fixture's stub
    exception_hook(*_capture(_crash_two_frames))
    assert hook_env == []


def test_hook_noop_when_isatty_raises(hook_env):
    class Broken:
        def isatty(self):
            raise ValueError("closed stream")

    pm.sys.stdin = Broken()
    exception_hook(*_capture(_crash_two_frames))
    assert hook_env == []


def test_hook_survives_snapshot_failure(hook_env, monkeypatch):
    monkeypatch.setattr(
        pm, "_make_snapshot", lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    exception_hook(*_capture(_crash_two_frames))  # must not raise
    assert hook_env == []


def test_hook_waits_for_live_breakpoint_subprocess(hook_env, monkeypatch):
    """If a tdb.breakpoint() TUI is still tearing down, the hook must
    wait for it before spawning the post-mortem TUI (tty handover)."""
    import tdb.breakpoint_hook as bph

    waited: list[float] = []

    class FakeProc:
        def poll(self):
            return None  # still running

        def wait(self, timeout):
            waited.append(timeout)

    monkeypatch.setattr(bph, "_subprocess", FakeProc())
    exception_hook(*_capture(_crash_two_frames))
    assert waited == [10]
    assert len(hook_env) == 1  # post-mortem still launched afterwards


def test_hook_proceeds_when_breakpoint_subprocess_hangs(hook_env, monkeypatch):
    import subprocess as sp

    import tdb.breakpoint_hook as bph

    class HungProc:
        def poll(self):
            return None

        def wait(self, timeout):
            raise sp.TimeoutExpired(cmd="tdb", timeout=timeout)

    monkeypatch.setattr(bph, "_subprocess", HungProc())
    exception_hook(*_capture(_crash_two_frames))  # must not raise
    assert len(hook_env) == 1  # launched anyway, with a warning logged

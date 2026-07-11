"""Unit tests for tdb.session.post_mortem_loader.

`load_post_mortem_into` rehydrates a DebugState from the JSON snapshot
written by tdb.exception_hook — no DAP, no async. These tests feed it
hand-built snapshot dicts and inspect the resulting state.
"""

from __future__ import annotations

from tdb.session.post_mortem_loader import load_post_mortem_into
from tdb.session.state import DebugState, SessionPhase


def _snapshot() -> dict:
    """Two-frame snapshot: crash site in `boom` called from `main`."""
    return {
        "version": 1,
        "exception": {
            "type": "ValueError",
            "message": "bad",
            "traceback_text": "Traceback ...",
        },
        "frames": [
            {
                "id": 1,
                "filename": "/app/lib.py",
                "lineno": 14,
                "funcname": "boom",
                "scopes": [{"name": "Locals", "variablesReference": 1001}],
            },
            {
                "id": 2,
                "filename": "/app/main.py",
                "lineno": 3,
                "funcname": "main",
                "scopes": [{"name": "Locals", "variablesReference": 1002}],
            },
        ],
        "variables": {
            "1001": [
                {"name": "x", "value": "7", "type": "int", "variablesReference": 0},
                {
                    "name": "items",
                    "value": "[1, 2]",
                    "type": "list",
                    "variablesReference": 1003,
                },
            ],
            "1002": [
                {"name": "arg", "value": "'go'", "type": "str"},
            ],
            "1003": [
                {"name": "[0]", "value": "1", "type": "int", "variablesReference": 0},
                {"name": "[1]", "value": "2", "type": "int", "variablesReference": 0},
            ],
        },
    }


def test_loads_phase_and_stop_reason():
    state = DebugState()
    load_post_mortem_into(state, _snapshot())
    assert state.phase is SessionPhase.POST_MORTEM
    assert state.is_post_mortem is True
    assert state.stop_reason == "exception"


def test_rebuilds_stack_frames_with_sources():
    state = DebugState()
    load_post_mortem_into(state, _snapshot())
    assert [f.name for f in state.stack_frames] == ["boom", "main"]
    top = state.stack_frames[0]
    assert top.id == 1
    assert top.line == 14
    assert top.source.path == "/app/lib.py"
    assert top.source.name == "lib.py"  # basename derived


def test_selects_crash_frame_and_its_scopes():
    state = DebugState()
    load_post_mortem_into(state, _snapshot())
    assert state.current_frame_id == 1
    assert [s.name for s in state.scopes] == ["Locals"]
    assert state.scopes[0].variables_reference == 1001
    # Every frame's scopes are retrievable for later frame selection.
    assert state.frame_scopes[2][0].variables_reference == 1002


def test_variables_rekeyed_by_int_reference():
    state = DebugState()
    load_post_mortem_into(state, _snapshot())
    assert set(state.variables) == {1001, 1002, 1003}
    x, items = state.variables[1001]
    assert (x.name, x.value, x.type) == ("x", "7", "int")
    assert items.variables_reference == 1003  # expandable child link intact


def test_missing_optional_fields_get_defaults():
    state = DebugState()
    load_post_mortem_into(
        state,
        {
            "frames": [{"id": 5}],  # no filename/lineno/funcname/scopes
            "variables": {"9": [{}]},  # entry with no keys at all
        },
    )
    (frame,) = state.stack_frames
    assert frame.name == "<frame>"
    assert frame.line == 0
    assert frame.source.path == ""
    assert frame.source.name is None  # empty path → no basename
    assert state.frame_scopes[5] == []
    (var,) = state.variables[9]
    assert (var.name, var.value, var.variables_reference) == ("", "", 0)


def test_empty_snapshot_is_harmless():
    state = DebugState()
    load_post_mortem_into(state, {})
    assert state.phase is SessionPhase.POST_MORTEM
    assert state.stack_frames == []
    assert state.variables == {}
    assert state.current_frame_id is None  # untouched


def test_reload_replaces_previous_content():
    state = DebugState()
    load_post_mortem_into(state, _snapshot())
    load_post_mortem_into(
        state,
        {
            "frames": [
                {
                    "id": 7,
                    "filename": "/other.py",
                    "lineno": 1,
                    "funcname": "f",
                    "scopes": [],
                }
            ],
            "variables": {},
        },
    )
    assert [f.id for f in state.stack_frames] == [7]
    assert state.variables == {}
    assert state.current_frame_id == 7

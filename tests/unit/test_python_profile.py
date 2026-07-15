"""DebugpyAdapter must reproduce the exact launch/attach bodies the
client hardcoded before the LanguageProfile extraction."""

import sys

import pytest

from tdb.dap.types import Capabilities
from tdb.languages.base import LanguageNotSupportedError
from tdb.languages.python import PYTHON_PROFILE, DebugpyAdapter, build_python_profile


def test_command_runs_own_interpreter_with_frozen_modules_off():
    assert DebugpyAdapter().command() == [
        sys.executable,
        "-Xfrozen_modules=off",
        "-m",
        "debugpy.adapter",
    ]


def test_launch_body_matches_legacy_client_body():
    body = DebugpyAdapter().launch_body(
        program="/tmp/prog.py",
        args=["a", "b"],
        cwd="/tmp",
        env={"K": "V"},
        stop_on_entry=True,
        console="internalConsole",
        opts={
            "just_my_code": False,
            "python": "/usr/bin/python3",
            "sub_process": False,
        },
    )
    assert body == {
        "type": "debugpy",
        "request": "launch",
        "program": "/tmp/prog.py",
        "args": ["a", "b"],
        "cwd": "/tmp",
        "console": "internalConsole",
        "redirectOutput": True,
        "justMyCode": False,
        "stopOnEntry": True,
        "subProcess": False,
        "pythonArgs": ["-Xfrozen_modules=off"],
        "env": {"K": "V"},
        "python": "/usr/bin/python3",
    }


def test_launch_body_defaults():
    body = DebugpyAdapter().launch_body(
        program="p.py",
        args=[],
        cwd=".",
        env=None,
        stop_on_entry=False,
        console="externalTerminal",
        opts={},
    )
    assert body["justMyCode"] is True
    assert body["subProcess"] is True
    assert body["redirectOutput"] is False  # externalTerminal
    assert "env" not in body
    assert "python" not in body


def test_attach_body_matches_legacy_client_body():
    body = DebugpyAdapter().attach_body(
        host="10.0.0.1",
        port=5678,
        opts={
            "sub_process_id": 42,
            "just_my_code": False,
            "path_mappings": [("/local", "/remote")],
        },
    )
    assert body == {
        "type": "debugpy",
        "request": "attach",
        "connect": {"host": "10.0.0.1", "port": 5678},
        "justMyCode": False,
        "subProcess": True,
        "subProcessId": 42,
        "pathMappings": [{"localRoot": "/local", "remoteRoot": "/remote"}],
    }


def test_attach_body_minimal():
    body = DebugpyAdapter().attach_body(host="127.0.0.1", port=1, opts={})
    assert "subProcessId" not in body
    assert "pathMappings" not in body
    assert body["justMyCode"] is True


def test_exception_filters_are_user_unhandled_regardless_of_caps():
    assert DebugpyAdapter().pick_exception_filters(Capabilities()) == ["userUnhandled"]


def test_quirks_pre_arm_pause_on_attach():
    assert DebugpyAdapter().quirks.pre_arm_pause_on_attach is True


def test_profile_shape():
    p = PYTHON_PROFILE
    assert p.id == "python"
    assert p.display_name == "Python"
    assert p.presentation.lexer == "python"
    assert p.capabilities.task_inspection is True
    assert p.capabilities.child_process_strategy == "debugpy"
    from tdb.source_analysis import compute_step_units

    assert p.capabilities.compute_step_units is compute_step_units


def test_build_rejects_unknown_adapter():
    with pytest.raises(LanguageNotSupportedError):
        build_python_profile(adapter="gdb")

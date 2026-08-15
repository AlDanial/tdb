import shutil

import pytest

from tdb.dap.types import Capabilities
from tdb.languages.base import AdapterNotFoundError, LanguageNotSupportedError
from tdb.languages.cpp import GdbDapAdapter, LldbDapAdapter, build_cpp_profile
from tdb.languages import registry


def test_profile_shape():
    p = build_cpp_profile()
    assert p.id == "cpp"
    assert p.adapter.id == "gdb"
    assert p.presentation.lexer == "cpp"
    assert p.capabilities.compute_step_units is None
    assert p.capabilities.child_process_strategy is None
    assert p.capabilities.task_inspection is False
    assert p.adapter.quirks.pre_arm_pause_on_attach is False


def test_registered_in_registry():
    assert "cpp" in registry.known_languages()
    assert registry.resolve("cpp").id == "cpp"


def test_command_uses_explicit_executable():
    assert LldbDapAdapter(executable="/opt/lldb-dap").command() == ["/opt/lldb-dap"]


def test_adapter_paths_override_reaches_default_adapter():
    p = build_cpp_profile(adapter_paths={"gdb": "/opt/gdb"})
    assert p.adapter.command() == ["/opt/gdb", "-i", "dap"]


def test_command_missing_executable_hints_install(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(AdapterNotFoundError) as exc:
        LldbDapAdapter().command()
    assert "lldb-dap" in exc.value.hint
    assert "LLVM" in exc.value.hint


def test_launch_body_shape():
    body = LldbDapAdapter().launch_body(
        program="/x/prog",
        args=["-n", "3"],
        cwd="/x",
        env={"A": "1"},
        stop_on_entry=True,
        console="internalConsole",
        opts={},
    )
    assert body == {
        "type": "lldb-dap",
        "request": "launch",
        "program": "/x/prog",
        "args": ["-n", "3"],
        "cwd": "/x",
        "stopOnEntry": True,
        "env": ["A=1"],  # lldb-dap takes KEY=VALUE strings
    }


def test_attach_not_supported():
    with pytest.raises(LanguageNotSupportedError):
        LldbDapAdapter().attach_body(host="h", port=1, opts={})


def test_exception_filters_use_adapter_defaults():
    caps = Capabilities.from_dict(
        {
            "exceptionBreakpointFilters": [
                {"filter": "cpp_throw", "label": "C++ Throw", "default": False},
                {"filter": "cpp_catch", "label": "C++ Catch", "default": False},
            ]
        }
    )
    # Neither is marked default -> no exception breakpoints (crashes
    # still stop the debuggee via signal handling).
    assert LldbDapAdapter().pick_exception_filters(caps) == []


def test_unknown_cpp_adapter_rejected():
    with pytest.raises(LanguageNotSupportedError, match="codelldb"):
        build_cpp_profile(adapter="codelldb")


def test_gdb_adapter_selectable():
    p = build_cpp_profile(adapter="gdb")
    assert p.adapter.id == "gdb"
    assert p.id == "cpp"  # same language side


def test_lldb_adapter_selectable():
    p = build_cpp_profile(adapter="lldb-dap")
    assert p.adapter.id == "lldb-dap"
    assert p.id == "cpp"  # same language side


def test_gdb_command():
    assert GdbDapAdapter(executable="/usr/bin/gdb").command() == [
        "/usr/bin/gdb",
        "-i",
        "dap",
    ]


def test_gdb_command_missing_hints_gdb14(monkeypatch):
    import shutil as _sh

    monkeypatch.setattr(_sh, "which", lambda name: None)
    with pytest.raises(AdapterNotFoundError, match="GDB >= 14"):
        GdbDapAdapter().command()


def test_gdb_launch_body():
    body = GdbDapAdapter().launch_body(
        program="/x/prog",
        args=["a"],
        cwd="/x",
        env=None,
        stop_on_entry=True,
        console="internalConsole",
        opts={},
    )
    assert body == {
        "type": "gdb",
        "request": "launch",
        "program": "/x/prog",
        "args": ["a"],
        "cwd": "/x",
        "stopAtBeginningOfMainSubprogram": True,
    }


def test_lldb_launch_body_external_terminal_sets_run_in_terminal() -> None:
    body = LldbDapAdapter().launch_body(
        program="/bin/x",
        args=[],
        cwd="/",
        env=None,
        stop_on_entry=False,
        console="externalTerminal",
        opts={},
    )
    assert body["runInTerminal"] is True


def test_lldb_launch_body_internal_console_omits_run_in_terminal() -> None:
    body = LldbDapAdapter().launch_body(
        program="/bin/x",
        args=[],
        cwd="/",
        env=None,
        stop_on_entry=False,
        console="internalConsole",
        opts={},
    )
    assert "runInTerminal" not in body


def test_gdb_launch_body_rejects_external_terminal() -> None:
    with pytest.raises(LanguageNotSupportedError, match="lldb-dap"):
        GdbDapAdapter().launch_body(
            program="/bin/x",
            args=[],
            cwd="/",
            env=None,
            stop_on_entry=False,
            console="externalTerminal",
            opts={},
        )

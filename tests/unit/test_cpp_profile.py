import shutil

import pytest

from tdb.dap.types import Capabilities, StackFrame
from tdb.languages.base import AdapterNotFoundError, LanguageNotSupportedError
from tdb.languages.cpp import (
    HOOK_STOP_FUNCTION,
    GdbDapAdapter,
    LldbDapAdapter,
    build_cpp_profile,
    hook_frame_base,
    is_breakpoint_hook_frame,
)
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


def test_attach_without_local_program_rejected():
    # Native remote attach needs the local symbol-bearing executable.
    with pytest.raises(LanguageNotSupportedError):
        LldbDapAdapter().attach_body(host="h", port=1, opts={})
    with pytest.raises(LanguageNotSupportedError):
        GdbDapAdapter().attach_body(host="h", port=1, opts={})


def test_gdb_attach_body_targets_remote_stub():
    body = GdbDapAdapter().attach_body(
        host="devbox", port=2345, opts={"program": "/local/app"}
    )
    assert body == {"program": "/local/app", "target": "devbox:2345"}


def test_lldb_attach_body_uses_gdb_remote_with_source_map():
    body = LldbDapAdapter().attach_body(
        host="devbox",
        port=2345,
        opts={"program": "/local/app", "path_mappings": [("/src", "/remote/src")]},
    )
    assert body["gdb-remote-host"] == "devbox"
    assert body["gdb-remote-port"] == 2345
    assert body["program"] == "/local/app"
    assert body["sourceMap"] == [["/remote/src", "/src"]]


def test_gdb_pre_configuration_commands_translate_path_mappings():
    assert GdbDapAdapter().pre_configuration_commands([("/local", "/remote")]) == (
        'set substitute-path "/remote" "/local"',
    )


def test_native_adapters_attach_via_adapter():
    assert GdbDapAdapter().quirks.attach_via_adapter is True
    assert LldbDapAdapter().quirks.attach_via_adapter is True


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


def test_gdb_pid_attach_body_and_quirks():
    gdb = GdbDapAdapter(attach_pid=4242)
    assert gdb.attach_body(host="127.0.0.1", port=0, opts={"program": "/bin/prog"}) == {
        "program": "/bin/prog",
        "pid": 4242,
    }
    assert gdb.quirks.attach_via_adapter is True
    assert gdb.quirks.attach_requires_local_program is True
    # The controller decides pause vs resume for pid attach; the
    # remote-stub "always resume" quirk must be off.
    assert gdb.quirks.attach_stop_is_pausable is True
    assert gdb.quirks.resume_after_remote_attach is False
    assert gdb.hook_function_breakpoints() == (HOOK_STOP_FUNCTION,)


def test_gdb_remote_attach_unchanged_without_pid():
    gdb = GdbDapAdapter()
    assert gdb.attach_body(host="h", port=9, opts={"program": "/bin/prog"}) == {
        "program": "/bin/prog",
        "target": "h:9",
    }
    assert gdb.quirks.resume_after_remote_attach is True
    assert gdb.quirks.attach_stop_is_pausable is False
    assert gdb.hook_function_breakpoints() == ()


def test_lldb_pid_attach_body_honors_pause_option():
    lldb = LldbDapAdapter(attach_pid=4242)
    assert lldb.attach_body(
        host="127.0.0.1", port=0, opts={"program": "/bin/prog"}
    ) == {
        "program": "/bin/prog",
        "pid": 4242,
        "stopOnEntry": True,
    }
    assert (
        lldb.attach_body(
            host="127.0.0.1",
            port=0,
            opts={"program": "/bin/prog", "pause_on_attach": False},
        )["stopOnEntry"]
        is False
    )
    assert lldb.quirks.attach_stop_is_pausable is False
    assert lldb.hook_function_breakpoints() == (HOOK_STOP_FUNCTION,)
    assert LldbDapAdapter().hook_function_breakpoints() == ()


def test_pid_attach_requires_program():
    with pytest.raises(LanguageNotSupportedError):
        GdbDapAdapter(attach_pid=1).attach_body(host="127.0.0.1", port=0, opts={})


def test_build_cpp_profile_forwards_attach_pid():
    p = build_cpp_profile(attach_pid=77)
    assert p.adapter.hook_function_breakpoints() == (HOOK_STOP_FUNCTION,)
    p = build_cpp_profile(adapter="lldb-dap", attach_pid=77)
    assert p.adapter.attach_body(host="", port=0, opts={"program": "x"})["pid"] == 77
    assert registry.resolve(
        "cpp", attach_pid=77
    ).adapter.hook_function_breakpoints() == (HOOK_STOP_FUNCTION,)


def _frame(name: str) -> StackFrame:
    return StackFrame(id=1, name=name, line=1, column=0)


@pytest.mark.parametrize(
    "name",
    [
        "tdb_breakpoint_stop",
        "tdb_breakpoint",
        "tdb_breakpoint_lang",
        # lldb-dap on g++ builds
        "::tdb_breakpoint_stop()",
        "::tdb_breakpoint_lang(const char *)",
        "::tdb_breakpoint()",
    ],
)
def test_cpp_hook_frames(name):
    assert is_breakpoint_hook_frame(_frame(name)) is True


@pytest.mark.parametrize(
    "name",
    [
        "main",
        "compute",
        "nanosleep",
        "",
        "tdb::breakpoint",
        "main()",
        "::main",
        "foo::bar",
        "<signal handler called>",
    ],
)
def test_cpp_non_hook_frames(name):
    assert is_breakpoint_hook_frame(_frame(name)) is False


@pytest.mark.parametrize(
    ("raw", "base"),
    [
        ("::tdb_breakpoint_stop()", "tdb_breakpoint_stop"),
        ("::tdb_breakpoint_lang(const char *)", "tdb_breakpoint_lang"),
        ("tdb::tdb_breakpoint_stop", "tdb::tdb_breakpoint_stop"),
        ("main", "main"),
        ("", ""),
    ],
)
def test_hook_frame_base(raw, base):
    assert hook_frame_base(raw) == base


def test_cpp_profile_declares_hook_predicate():
    assert (
        build_cpp_profile().capabilities.breakpoint_hook_frame
        is is_breakpoint_hook_frame
    )

"""tests/unit/test_go_profile.py"""

import shutil

import pytest

from tdb.languages import registry
from tdb.languages.base import AdapterNotFoundError, LanguageNotSupportedError
from tdb.languages.go import DelveAdapter, build_go_profile


def test_profile_shape():
    p = build_go_profile()
    assert p.id == "go"
    assert p.display_name == "Go"
    assert p.adapter.id == "dlv"
    assert p.adapter.connect_mode == "spawn_tcp"
    assert p.adapter.listen_regex is not None
    assert p.presentation.lexer == "go"
    assert p.capabilities.task_inspection is False
    assert p.capabilities.child_process_strategy is None
    assert p.capabilities.pause_while_running is True
    assert p.capabilities.concurrency_inspection == "go"
    assert p.capabilities.compute_step_units is None


def test_registered_in_registry():
    assert "go" in registry.known_languages()
    assert registry.resolve("go").id == "go"


def test_command_and_adapter_paths_override():
    assert DelveAdapter(executable="/opt/dlv").command() == [
        "/opt/dlv",
        "dap",
        "--listen=127.0.0.1:0",
    ]
    p = build_go_profile(adapter_paths={"dlv": "/opt/dlv"})
    assert p.adapter.command()[0] == "/opt/dlv"


def test_command_missing_dlv_hints_install(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(AdapterNotFoundError) as exc:
        DelveAdapter().command()
    assert "go install github.com/go-delve/delve/cmd/dlv@latest" in exc.value.hint


def test_listen_regex_matches_dlv_output():
    m = DelveAdapter.listen_regex.search("DAP server listening at: 127.0.0.1:38697\n")
    assert m is not None
    assert (m.group(1), m.group(2)) == ("127.0.0.1", "38697")


def _body(adapter, program, console="internalConsole"):
    return adapter.launch_body(
        program=program,
        args=["-n"],
        cwd="/w",
        env={"A": "1"},
        stop_on_entry=True,
        console=console,
        opts={},
    )


def test_launch_mode_debug_for_source(tmp_path):
    src = tmp_path / "main.go"
    src.write_text("package main\n")
    body = _body(DelveAdapter(), str(src))
    assert body["mode"] == "debug"
    assert body == {
        "type": "go",
        "request": "launch",
        "mode": "debug",
        "program": str(src),
        "args": ["-n"],
        "cwd": "/w",
        "stopOnEntry": True,
        "env": {"A": "1"},
    }


def test_launch_mode_exec_for_go_binary(tmp_path):
    binary = tmp_path / "prog"
    binary.write_bytes(b"\x7fELF" + b"\x00" * 60 + b"\xff Go buildinf:xxxx")
    assert _body(DelveAdapter(), str(binary))["mode"] == "exec"


def test_launch_mode_test_via_builder(tmp_path):
    p = build_go_profile(program=str(tmp_path), test=True)
    assert _body(p.adapter, str(tmp_path))["mode"] == "test"


def test_terminal_rejected():
    with pytest.raises(LanguageNotSupportedError):
        _body(DelveAdapter(), "x.go", console="externalTerminal")


def test_attach_bodies():
    local = build_go_profile(attach_pid=1234).adapter
    assert local.attach_body(host="127.0.0.1", port=0, opts={}) == {
        "mode": "local",
        "processId": 1234,
        "stopOnEntry": True,
    }
    assert local.quirks.attach_via_adapter is True
    remote = build_go_profile().adapter
    assert remote.attach_body(host="h", port=9, opts={}) == {"mode": "remote"}
    assert remote.quirks.attach_via_adapter is False


def test_unknown_adapter_rejected():
    with pytest.raises(LanguageNotSupportedError):
        build_go_profile(adapter="gdb")


from tdb.dap.types import Source, StackFrame, Thread
from tdb.languages.go import classify_go_threads


def _frame(name):
    return StackFrame(id=1, name=name, source=Source(path="/w/main.go"), line=1)


def test_classify_hides_pure_runtime_goroutines():
    threads = [
        Thread(id=1, name="* [Go 1] main.main"),
        Thread(id=2, name="[Go 17] runtime.gcBgMarkWorker"),
        Thread(id=3, name="[Go 5] main.worker"),
    ]
    stacks = {
        1: [_frame("main.main")],
        2: [_frame("runtime.gopark"), _frame("runtime.gcBgMarkWorker")],
        3: [
            _frame("runtime.gopark"),
            _frame("runtime.chanrecv"),
            _frame("main.worker"),
        ],
    }
    d = classify_go_threads(threads, stacks)
    assert [x.hidden for x in d] == [False, True, False]
    assert all(x.label is None for x in d)  # dlv's names are already good


def test_classify_without_stack_stays_visible():
    threads = [Thread(id=9, name="[Go 9] main.helper")]
    d = classify_go_threads(threads, {})
    assert d[0].hidden is False


def test_profile_wires_error_parser_and_classifier():
    p = build_go_profile()
    assert p.presentation.parse_error is not None
    assert p.capabilities.classify_threads is not None

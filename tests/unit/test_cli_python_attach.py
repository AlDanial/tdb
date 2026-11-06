"""`tdb -a PID` for Python: CLI wiring and the attach request."""

from __future__ import annotations

import pytest

from tdb.cli import parse_args
from tdb.languages.base import LanguageNotSupportedError
from tdb.languages.python import DebugpyAdapter, PidAttachDebugpyAdapter
from tdb.languages.python_attach import TargetInfo

TARGET = TargetInfo(4242, "/usr/bin/python3.14", True)


@pytest.fixture
def probe_calls(monkeypatch):
    calls: list[tuple] = []

    def probe(pid, python=None):
        calls.append((pid, python))
        return TARGET

    monkeypatch.setattr("tdb.languages.python.probe_target", probe)
    return calls


def test_attach_python_by_lang(probe_calls):
    args = parse_args(["--lang", "python", "-a", "4242"])
    assert args.profile.id == "python"
    assert isinstance(args.profile.adapter, PidAttachDebugpyAdapter)
    assert args.profile.adapter.target == TARGET
    assert args.program is None
    assert (args.attach_host, args.attach_port) == ("127.0.0.1", 0)


def test_attach_python_detected_from_proc(probe_calls, tmp_path, monkeypatch):
    exe = tmp_path / "python3.14"
    exe.write_bytes(b"\x7fELF" + b"\0" * 60)
    monkeypatch.setattr("tdb.cli.sys.platform", "linux")
    monkeypatch.setattr("tdb.cli._pid_exe", lambda pid: str(exe))
    args = parse_args(["-a", "4242"])
    assert args.profile.id == "python"


def test_attach_python_rejects_program(probe_calls, tmp_path, capsys):
    script = tmp_path / "app.py"
    script.write_text("pass\n")
    with pytest.raises(SystemExit):
        parse_args(["--lang", "python", "-a", "4242", str(script)])
    assert "cannot be combined" in capsys.readouterr().err


def test_attach_python_forwards_python_flag(probe_calls):
    parse_args(["--lang", "python", "-a", "4242", "--python", "/opt/py/bin/python3"])
    assert probe_calls == [(4242, "/opt/py/bin/python3")]


def test_probe_error_becomes_parser_error(monkeypatch, capsys):
    def probe(pid, python=None):
        raise LanguageNotSupportedError("no process with pid 4242")

    monkeypatch.setattr("tdb.languages.python.probe_target", probe)
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--lang", "python", "-a", "4242"])
    assert exc_info.value.code == 2
    assert "no process with pid 4242" in capsys.readouterr().err


def test_remote_attach_keeps_plain_adapter():
    args = parse_args(["-r", "5678"])
    assert type(args.profile.adapter) is DebugpyAdapter


def test_attach_body_shape():
    body = PidAttachDebugpyAdapter(TARGET, 6000).attach_body(
        host="127.0.0.1", port=0, opts={}
    )
    assert body["listen"] == {"host": "127.0.0.1", "port": 6000}
    assert body["redirectOutput"] is True
    assert body["subProcess"] is False
    assert "connect" not in body


def test_quirks():
    quirks = PidAttachDebugpyAdapter(TARGET, 6000).quirks
    assert quirks.attach_via_adapter
    # A pre-armed pause lands inside the injected call, with gdb attached.
    assert not quirks.pre_arm_pause_on_attach
    assert quirks.stdin_route is None


async def test_adapter_hook_runs_inject(monkeypatch):
    calls = []

    async def fake_inject(target, port, connected, quitting, *, token):
        calls.append((target, port, connected, quitting, token))
        return "sys.remote_exec"

    monkeypatch.setattr("tdb.languages.python.inject", fake_inject)
    event, quitting = object(), object()
    spec = PidAttachDebugpyAdapter(TARGET, 6000)
    method = await spec.after_attach_request(event, quitting)
    assert method == "sys.remote_exec"
    assert calls == [(TARGET, 6000, event, quitting, spec.token)]

"""load_recording validates everything before any process is launched."""

import pytest

from tdb.replay import Recording, RecordingError, load_recording

HEADER = (
    '{"tdb_recording": 1, "created": "2026-07-31T00:00:00", "mode": "launch",'
    ' "language": "python", "program": "/abs/p.py", "args": [], "cwd": "/abs",'
    ' "python": null, "adapter": null, "step_mode": "statement",'
    ' "no_just_my_code": false}'
)


def write(tmp_path, *lines):
    p = tmp_path / "rec.jsonl"
    p.write_text("\n".join(lines) + "\n")
    return str(p)


def test_loads_valid_recording(tmp_path):
    path = write(
        tmp_path,
        HEADER,
        '{"t": 0.1, "action": "set_breakpoint", "params": ["/abs/p.py:3"]}',
        '{"t": 0.5, "action": "continue", "params": []}',
    )
    rec = load_recording(path)
    assert isinstance(rec, Recording)
    assert rec.header["mode"] == "launch"
    assert [r["action"] for r in rec.records] == ["set_breakpoint", "continue"]


def test_blank_lines_are_skipped(tmp_path):
    path = write(tmp_path, HEADER, "", '{"t": 1, "action": "quit", "params": []}')
    assert len(load_recording(path).records) == 1


def test_empty_file_rejected(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    with pytest.raises(RecordingError):
        load_recording(str(p))


def test_wrong_version_rejected_line1(tmp_path):
    path = write(tmp_path, '{"tdb_recording": 99, "mode": "launch"}')
    with pytest.raises(RecordingError, match=r":1:"):
        load_recording(path)


def test_unknown_mode_rejected(tmp_path):
    path = write(tmp_path, HEADER.replace('"launch"', '"teleport"'))
    with pytest.raises(RecordingError, match="mode"):
        load_recording(path)


def test_launch_header_missing_program_rejected(tmp_path):
    bad = HEADER.replace('"program": "/abs/p.py", ', "")
    path = write(tmp_path, bad)
    with pytest.raises(RecordingError, match="program"):
        load_recording(path)


def test_malformed_json_names_line(tmp_path):
    path = write(tmp_path, HEADER, "{not json")
    with pytest.raises(RecordingError, match=r":2:"):
        load_recording(path)


def test_unknown_action_names_line(tmp_path):
    path = write(tmp_path, HEADER, '{"t": 1, "action": "teleport", "params": []}')
    with pytest.raises(RecordingError, match=r":2:.*teleport"):
        load_recording(path)


def test_missing_t_rejected(tmp_path):
    path = write(tmp_path, HEADER, '{"action": "continue", "params": []}')
    with pytest.raises(RecordingError, match=r":2:"):
        load_recording(path)


def test_non_list_params_rejected(tmp_path):
    path = write(tmp_path, HEADER, '{"t": 1, "action": "continue", "params": "x"}')
    with pytest.raises(RecordingError, match=r":2:"):
        load_recording(path)


def test_attach_header_requires_host_port(tmp_path):
    attach = (
        '{"tdb_recording": 1, "mode": "remote-attach", "language": "python",'
        ' "host": "127.0.0.1", "step_mode": "statement"}'
    )
    with pytest.raises(RecordingError, match="port"):
        load_recording(write(tmp_path, attach))


def test_loads_recording_with_detach(tmp_path):
    path = write(tmp_path, HEADER, '{"t": 1, "action": "detach", "params": []}')
    assert [r["action"] for r in load_recording(path).records] == ["detach"]


def test_detach_is_not_an_rpc_action():
    from tdb.server.handlers import RpcHandlers

    assert "detach" not in RpcHandlers.ACTIONS


async def test_replay_detach_ends_session_like_quit(tmp_path, monkeypatch):
    import tdb.replay as replay
    import tdb.server.runner as runner

    stopped = []

    class FakeController:
        async def stop(self):
            stopped.append(True)

    class FakeHandler:
        def drain_output(self):
            return ""

    async def fake_setup(**kw):
        return FakeController(), FakeHandler()

    monkeypatch.setattr(runner, "setup_headless_session", fake_setup)
    monkeypatch.setattr(replay, "profile_from_header", lambda h: None)
    path = write(
        tmp_path,
        HEADER,
        '{"t": 1, "action": "detach", "params": []}',
        '{"t": 2, "action": "evaluate", "params": ["never"]}',
    )
    lines = []
    errors = await replay.run_replay(load_recording(path), echo=lines.append)
    assert errors == 0
    assert stopped == [True]  # ended once (via the quit handler), no relay
    assert not any("never" in ln for ln in lines)
    assert any("ok:" in ln for ln in lines)

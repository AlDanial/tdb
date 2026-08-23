"""SessionRecorder writes spec-shaped JSONL; NullRecorder is inert."""

import json
from types import SimpleNamespace

from tdb.session.recorder import NullRecorder, SessionRecorder, build_header


def read_lines(path):
    return [json.loads(x) for x in path.read_text().splitlines()]


def make_header(**over):
    base = {"tdb_recording": 1, "created": "2026-07-31T00:00:00", "mode": "launch"}
    base.update(over)
    return base


def test_header_is_first_line_and_flushed_immediately(tmp_path):
    p = tmp_path / "rec.jsonl"
    rec = SessionRecorder(str(p), make_header(program="/x.py"))
    # No close, no record: header must already be on disk (flush-per-line).
    lines = read_lines(p)
    assert lines[0]["tdb_recording"] == 1
    assert lines[0]["program"] == "/x.py"
    rec.close()


def test_record_appends_t_action_params_and_flushes(tmp_path):
    p = tmp_path / "rec.jsonl"
    rec = SessionRecorder(str(p), make_header())
    rec.record("set_breakpoint", ["/x.py:3"])
    rec.record("continue", [])
    lines = read_lines(p)  # file readable before close
    assert lines[1]["action"] == "set_breakpoint"
    assert lines[1]["params"] == ["/x.py:3"]
    assert isinstance(lines[1]["t"], float)
    assert lines[2]["action"] == "continue"
    assert lines[2]["t"] >= lines[1]["t"]
    rec.close()


def test_active_flag_and_close(tmp_path):
    rec = SessionRecorder(str(tmp_path / "r.jsonl"), make_header())
    assert rec.active is True
    rec.close()
    assert rec.active is False
    rec.record("continue", [])  # after close: silently ignored
    rec.close()  # double close: no error


def test_write_failure_degrades_and_reports_once(tmp_path):
    p = tmp_path / "r.jsonl"
    rec = SessionRecorder(str(p), make_header())
    errors = []
    rec.on_error = errors.append
    rec._file.close()  # simulate the OS yanking the file mid-session
    rec.record("continue", [])
    rec.record("next", [])
    assert rec.active is False
    assert len(errors) == 1  # reported once, not per record


def test_null_recorder_is_inert(tmp_path):
    rec = NullRecorder()
    assert rec.active is False
    rec.on_error = lambda m: None
    rec.record("continue", [])
    rec.close()


def _ns(**kw):
    base = dict(
        attach_host=None,
        attach_port=None,
        path_mappings=[],
        profile=SimpleNamespace(id="python"),
        adapter=None,
        program="/abs/prog.py",
        args=["a1"],
        cwd="/abs/dir",
        python=None,
        no_just_my_code=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_build_header_launch():
    h = build_header(_ns(), SimpleNamespace(step_mode="statement"))
    assert h["tdb_recording"] == 1
    assert h["mode"] == "launch"
    assert h["language"] == "python"
    assert h["program"] == "/abs/prog.py"
    assert h["args"] == ["a1"]
    assert h["cwd"] == "/abs/dir"
    assert h["python"] is None
    assert h["adapter"] is None
    assert h["step_mode"] == "statement"
    assert h["no_just_my_code"] is False
    assert "host" not in h
    assert isinstance(h["created"], str)


def test_build_header_launch_defaults_cwd_to_getcwd():
    import os

    h = build_header(_ns(cwd=None), SimpleNamespace(step_mode="line"))
    assert h["cwd"] == os.getcwd()


def test_build_header_remote_attach():
    h = build_header(
        _ns(
            attach_host="10.0.0.5",
            attach_port=5678,
            path_mappings=[("/local", "/remote")],
            program=None,
        ),
        SimpleNamespace(step_mode="statement"),
    )
    assert h["mode"] == "remote-attach"
    assert h["host"] == "10.0.0.5"
    assert h["port"] == 5678
    assert h["path_mappings"] == [["/local", "/remote"]]
    assert h["program"] is None


def test_build_header_remote_attach_keeps_rust_local_program():
    # Rust native remote attach replays need the local symbol-bearing
    # executable, so attach headers persist it.
    h = build_header(
        _ns(
            attach_host="10.0.0.5",
            attach_port=2345,
            path_mappings=[],
            profile=SimpleNamespace(id="rust"),
            program="/abs/target/debug/app",
        ),
        SimpleNamespace(step_mode="line"),
    )
    assert h["mode"] == "remote-attach"
    assert h["program"] == "/abs/target/debug/app"


def test_build_header_tolerates_missing_profile():
    h = build_header(_ns(profile=None), SimpleNamespace(step_mode="statement"))
    assert h["language"] == "python"

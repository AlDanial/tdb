"""--record flag parsing/validation and TdbApp recorder wiring."""

import pytest

from tdb.app import TdbApp
from tdb.cli import parse_args
from tdb.persist import TdbConfig
from tdb.session.recorder import NullRecorder


def test_record_flag_parses(tmp_path):
    prog = tmp_path / "p.py"
    prog.write_text("x = 1\n")
    args = parse_args(["--record", str(tmp_path / "s.jsonl"), str(prog)])
    assert args.record == str(tmp_path / "s.jsonl")


def test_record_default_is_none(tmp_path):
    prog = tmp_path / "p.py"
    prog.write_text("x = 1\n")
    assert parse_args([str(prog)]).record is None


@pytest.mark.parametrize("conflict", ["--headless", "--server"])
def test_record_rejected_with_server_modes(tmp_path, conflict, capsys):
    prog = tmp_path / "p.py"
    prog.write_text("x = 1\n")
    with pytest.raises(SystemExit):
        parse_args(["--record", "out.jsonl", conflict, str(prog)])
    assert "--record" in capsys.readouterr().err


def test_record_rejected_with_post_mortem(capsys):
    with pytest.raises(SystemExit):
        parse_args(["--record", "out.jsonl", "--post-mortem", "snap.json"])
    assert "--record" in capsys.readouterr().err


async def test_app_defaults_to_null_recorder():
    app = TdbApp(program="", config=TdbConfig())
    assert isinstance(app.recorder, NullRecorder)


async def test_app_accepts_recorder_and_wires_on_error():
    class Cap:
        def __init__(self):
            self.active = True
            self.on_error = None

        def record(self, action, params):
            pass

        def close(self):
            pass

    cap = Cap()
    app = TdbApp(program="", config=TdbConfig(), recorder=cap)
    assert app.recorder is cap
    assert cap.on_error is not None  # app installed its notify callback

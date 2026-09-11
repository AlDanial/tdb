"""--replay-tui argument parsing and conflicts."""

import pytest

from tdb.cli import parse_args


def test_replay_tui_parses_file():
    args = parse_args(["--replay-tui", "session.jsonl"])
    assert args.replay_tui == "session.jsonl"
    assert args.replay is None


def test_replay_tui_accepts_timing_and_timeout():
    args = parse_args(["--replay-tui", "s.jsonl", "--timing", "--replay-timeout", "5"])
    assert args.timing is True
    assert args.replay_timeout == 5.0


def test_replay_tui_rejects_program_argument(capsys):
    with pytest.raises(SystemExit):
        parse_args(["--replay-tui", "s.jsonl", "prog.py"])
    assert "takes no program argument" in capsys.readouterr().err


@pytest.mark.parametrize(
    "extra",
    [
        ["--record", "out.jsonl"],
        ["--server"],
        ["--headless"],
        ["--mcp"],
        ["--replay", "s.jsonl"],
    ],
)
def test_replay_tui_conflicts(extra, capsys):
    with pytest.raises(SystemExit):
        parse_args(["--replay-tui", "s.jsonl"] + extra)
    assert "--replay-tui cannot be combined with" in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["--run", "--eval"])
def test_run_and_eval_conflict_with_replay_tui(mode, capsys):
    argv = (
        [mode] + (["3", "x"] if mode == "--eval" else []) + ["--replay-tui", "s.jsonl"]
    )
    with pytest.raises(SystemExit):
        parse_args(argv)
    assert "cannot be combined with" in capsys.readouterr().err


def test_replay_quiet_parses_with_replay_tui():
    args = parse_args(["--replay-tui", "s.jsonl", "--replay-quiet"])
    assert args.replay_quiet is True
    assert parse_args(["--replay-tui", "s.jsonl"]).replay_quiet is False


def test_replay_quiet_requires_replay_tui(capsys, tmp_path):
    prog = tmp_path / "p.py"
    prog.write_text("x = 1\n")
    with pytest.raises(SystemExit):
        parse_args(["--replay-quiet", str(prog)])
    assert (
        "--replay-quiet has no effect without --replay-tui" in capsys.readouterr().err
    )


def test_build_replay_tui_app_passes_quiet_to_driver(tmp_path):
    from tdb.cli import build_replay_tui_app
    from tdb.persist import TdbConfig
    from tdb.replay import Recording

    rec = Recording(
        header={
            "mode": "launch",
            "language": "python",
            "program": str(tmp_path / "p.py"),
            "args": [],
            "cwd": str(tmp_path),
        },
        records=[],
    )
    _, driver = build_replay_tui_app(rec, label="s", config=TdbConfig(), announce=False)
    assert driver.announce is False


@pytest.mark.parametrize("mode", ["--replay", "--replay-tui"])
def test_replay_interval_parses_with_either_replay_mode(mode):
    args = parse_args([mode, "s.jsonl", "--replay-interval", "1.5"])
    assert args.replay_interval == 1.5
    assert parse_args([mode, "s.jsonl"]).replay_interval is None


def test_replay_interval_requires_a_replay_mode(capsys, tmp_path):
    prog = tmp_path / "p.py"
    prog.write_text("x = 1\n")
    with pytest.raises(SystemExit):
        parse_args(["--replay-interval", "1", str(prog)])
    assert "--replay-interval has no effect without" in capsys.readouterr().err


def test_replay_interval_conflicts_with_timing(capsys):
    with pytest.raises(SystemExit):
        parse_args(["--replay", "s.jsonl", "--timing", "--replay-interval", "1"])
    assert (
        "--replay-interval cannot be combined with --timing" in capsys.readouterr().err
    )


def test_replay_interval_rejects_negative(capsys):
    with pytest.raises(SystemExit):
        parse_args(["--replay-tui", "s.jsonl", "--replay-interval", "-1"])
    assert "--replay-interval must be >= 0" in capsys.readouterr().err


def test_build_replay_tui_app_passes_interval_to_driver(tmp_path):
    from tdb.cli import build_replay_tui_app
    from tdb.persist import TdbConfig
    from tdb.replay import Recording

    rec = Recording(
        header={
            "mode": "launch",
            "language": "python",
            "program": str(tmp_path / "p.py"),
            "args": [],
            "cwd": str(tmp_path),
        },
        records=[],
    )
    _, driver = build_replay_tui_app(rec, label="s", config=TdbConfig(), interval=2.0)
    assert driver.interval == 2.0

"""--examine-log parsing: repeatable, '-' means stdout, only with --run."""

import pytest

from tdb.cli import build_parser


def test_examine_log_default_empty():
    args = build_parser().parse_args(["--run", "prog.py"])
    assert args.examine_log == []


def test_examine_log_repeatable_in_order():
    args = build_parser().parse_args(
        ["--run", "--examine-log", "-", "--examine-log", "hang.jsonl", "prog.py"]
    )
    assert args.examine_log == ["-", "hang.jsonl"]


def test_examine_log_requires_run(capsys):
    from tdb.cli import parse_args

    with pytest.raises(SystemExit) as ei:
        parse_args(["--examine-log", "x.jsonl", "prog.py"])
    assert ei.value.code == 2
    assert "--examine-log requires --run" in capsys.readouterr().err


def test_run_help_mentions_examine():
    text = build_parser().format_help()
    assert "--examine-log" in text
    assert "snapshot" in text

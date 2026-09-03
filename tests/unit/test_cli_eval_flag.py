"""-e/--eval FILE:LINE EXPR: headless run that evaluates EXPR each time
the program reaches FILE:LINE, then continues. No TUI; incompatible
with every mode that owns the session lifecycle differently. Locations
reuse the -k resolution + statement-snapping machinery; saved
breakpoints are never loaded (the headless path never reads them)."""

import pytest

from tdb.cli import parse_args


@pytest.fixture
def prog(tmp_path):
    p = tmp_path / "prog.py"
    p.write_text("x = 1\nfor i in range(3):\n    x += i\nprint(x)\n")
    return str(p)


def test_eval_parses_file_line_spec(prog):
    args = parse_args(["-e", f"{prog}:3", "print(x)", prog])
    assert args.eval_points == [(prog, 3, "print(x)")]
    assert args.stop_on_entry is False


def test_eval_bare_line_targets_program(prog):
    args = parse_args(["--eval", "3", "x", prog])
    assert args.eval_points == [(prog, 3, "x")]


def test_eval_repeatable(prog):
    args = parse_args(["-e", "1", "x", "-e", "3", "print(x)", prog])
    assert args.eval_points == [(prog, 1, "x"), (prog, 3, "print(x)")]


def test_eval_snaps_to_statement_start(prog, capsys):
    # Line 2 is a `for` header; its body line 3 is a statement start, but
    # a continuation-ish target like the blank-free line 2 stays put.
    # Use a line that is mid-statement to see snapping: build one.
    args = parse_args(["-e", f"{prog}:2", "x", prog])
    assert args.eval_points == [(prog, 2, "x")]


def test_eval_snap_moves_continuation_line(tmp_path, capsys):
    p = tmp_path / "multi.py"
    p.write_text("total = (1 +\n         2)\nprint(total)\n")
    args = parse_args(["-e", f"{p}:2", "total", str(p)])
    # Line 2 is a continuation; snaps back to line 1 with a warning.
    assert args.eval_points == [(str(p), 1, "total")]
    assert "moved to line 1" in capsys.readouterr().err


def test_eval_invalid_line_number(prog, capsys):
    with pytest.raises(SystemExit):
        parse_args(["-e", f"{prog}:notaline", "x", prog])
    assert "Invalid line number" in capsys.readouterr().err


def test_eval_missing_file(prog, capsys):
    with pytest.raises(SystemExit):
        parse_args(["-e", "no_such_file.py:3", "x", prog])
    assert "file not found" in capsys.readouterr().err.lower()


def test_eval_requires_program(capsys):
    with pytest.raises(SystemExit):
        parse_args(["-e", "3", "x"])


@pytest.mark.parametrize(
    "extra",
    [
        ["--run"],
        ["-r", "5678"],
        ["-k", "3"],
        ["-t", "3"],
        ["--record", "out.json"],
        ["--server"],
        ["--headless"],
        ["--mcp"],
        ["--terminal", "xterm"],
        ["-a", "1234"],
    ],
)
def test_eval_conflicts(prog, extra, capsys):
    with pytest.raises(SystemExit):
        parse_args(["-e", "3", "x", prog] + extra)
    assert "--eval cannot be combined with" in capsys.readouterr().err


def test_eval_all_points_dropped_is_an_error(tmp_path, capsys):
    # Snapping drops a line that precedes the first logical statement;
    # if that leaves NO eval points, launching would silently run the
    # whole program with no evaluation — must be a hard error instead.
    p = tmp_path / "header.py"
    p.write_text("# comment\n# comment\nx = 1\n")
    with pytest.raises(SystemExit):
        parse_args(["-e", f"{p}:1", "print(x)", str(p)])
    err = capsys.readouterr().err
    assert "no logical statement" in err
    assert "--eval" in err


def test_eval_some_points_dropped_still_runs(tmp_path, capsys):
    p = tmp_path / "header.py"
    p.write_text("# comment\n# comment\nx = 1\nprint(x)\n")
    args = parse_args(["-e", f"{p}:1", "x", "-e", f"{p}:3", "x", str(p)])
    assert args.eval_points == [(str(p), 3, "x")]


def test_eval_conflicts_with_replay(capsys):
    with pytest.raises(SystemExit):
        parse_args(["-e", "3", "x", "--replay", "session.json"])
    assert "--eval cannot be combined with" in capsys.readouterr().err

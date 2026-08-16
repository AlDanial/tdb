"""--run: headless execution until a signal opens the TUI. Long flag
only (-r is --remote-attach); incompatible with every mode that owns
the terminal or the session lifecycle differently; requires an adapter
that can pause a running debuggee."""

import pytest

from tdb.cli import parse_args


@pytest.fixture
def prog(tmp_path):
    p = tmp_path / "prog.py"
    p.write_text("print('hi')\n")
    return str(p)


def test_run_flag_parses_and_implies_no_stop_on_entry(prog):
    args = parse_args(["--run", prog])
    assert args.run is True
    assert args.stop_on_entry is False


def test_run_rejects_short_r_as_remote_attach(prog):
    # -r must still mean --remote-attach, never --run.
    args = parse_args(["-r", "5678"])
    assert args.remote_attach == "5678"
    assert args.run is False


@pytest.mark.parametrize(
    "extra",
    [
        ["-r", "5678"],
        ["-k", "3"],
        ["-t", "3"],
        ["--record", "out.json"],
        ["--server"],
        ["--headless"],
        ["--mcp"],
    ],
)
def test_run_conflicts(prog, extra, capsys):
    with pytest.raises(SystemExit):
        parse_args(["--run", prog] + extra)
    assert "--run cannot be combined with" in capsys.readouterr().err


def test_run_conflicts_with_replay(capsys):
    with pytest.raises(SystemExit):
        parse_args(["--run", "--replay", "session.json"])
    assert "--run cannot be combined with" in capsys.readouterr().err


def test_run_requires_pause_capable_language(tmp_path, capsys):
    # cpp is the profile without pause_while_running until Task 9.
    prog = tmp_path / "prog.py"
    prog.write_text("print('hi')\n")
    with pytest.raises(SystemExit):
        parse_args(["--run", "--lang", "cpp", str(prog)])
    err = capsys.readouterr().err
    assert "cannot pause a running program" in err

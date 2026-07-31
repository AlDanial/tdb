"""End-to-end: a recording file drives a real debugpy session in-process."""

import json

import pytest

from tdb.replay import load_recording, run_replay

TOY = """\
x = 1
y = 2
z = x + y
print("z =", z)
"""


def make_recording(tmp_path, records, *, stop_on_entry_continue=False):
    prog = tmp_path / "toy.py"
    prog.write_text(TOY)
    header = {
        "tdb_recording": 1,
        "created": "2026-07-31T00:00:00",
        "mode": "launch",
        "language": "python",
        "program": str(prog),
        "args": [],
        "cwd": str(tmp_path),
        "python": None,
        "adapter": None,
        "step_mode": "line",
        "no_just_my_code": False,
    }
    lines = [json.dumps(header)]
    t = 0.0
    for action, params in records:
        t += 0.1
        lines.append(json.dumps({"t": round(t, 3), "action": action, "params": params}))
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return str(path), str(prog)


async def test_replay_breakpoint_evaluate_quit(tmp_path):
    path, prog = make_recording(
        tmp_path,
        [
            ("set_breakpoint", [f"{tmp_path}/toy.py:3"]),
            ("continue", []),
            ("evaluate", ["x + y"]),
            ("quit", []),
        ],
    )
    out: list[str] = []
    errors = await run_replay(load_recording(path), echo=out.append)
    text = "\n".join(out)
    assert errors == 0
    assert "4 commands, 0 errors" in text
    assert "ok: 3" in text  # evaluate result, verbatim
    assert "set_breakpoint" in text  # command header lines present


async def test_replay_reports_failed_command_and_continues(tmp_path):
    path, _ = make_recording(
        tmp_path,
        [
            ("set_breakpoint", ["not-a-file-line-spec"]),  # ERROR
            ("continue", []),  # still runs
            ("quit", []),
        ],
    )
    out: list[str] = []
    errors = await run_replay(load_recording(path), echo=out.append)
    text = "\n".join(out)
    assert errors == 1
    assert "ERROR:" in text
    assert "3 commands, 1 errors" in text


async def test_replay_interleaves_program_output(tmp_path):
    path, _ = make_recording(
        tmp_path,
        [("continue", []), ("quit", [])],
    )
    out: list[str] = []
    await run_replay(load_recording(path), echo=out.append)
    text = "\n".join(out)
    assert "z = 3" in text  # debuggee stdout surfaced in the transcript


async def test_replay_timing_sleeps_recorded_deltas(tmp_path):
    import time

    path, _ = make_recording(tmp_path, [("quit", [])])
    # rewrite the single record with t=0.5 to force a measurable delay
    lines = open(path).read().splitlines()
    rec = json.loads(lines[1])
    rec["t"] = 0.5
    open(path, "w").write(lines[0] + "\n" + json.dumps(rec) + "\n")
    out: list[str] = []
    t0 = time.monotonic()
    await run_replay(load_recording(path), timing=True, echo=out.append)
    assert time.monotonic() - t0 >= 0.5


async def test_condition_reset_updates_in_place(tmp_path):
    """Spec § limitations: re-recording a breakpoint with a condition
    (the condition-modal gesture) must yield ONE breakpoint on replay —
    controller.add_breakpoint updates in place (controller.py:588)."""
    path, prog = make_recording(
        tmp_path,
        [
            ("set_breakpoint", [f"{tmp_path}/toy.py:3"]),
            ("set_breakpoint", [f"{tmp_path}/toy.py:3", "x == 1", ""]),
            ("list_breakpoints", []),
            ("quit", []),
        ],
    )
    out: list[str] = []
    errors = await run_replay(load_recording(path), echo=out.append)
    text = "\n".join(out)
    assert errors == 0
    assert text.count("toy.py:3") >= 1
    # exactly one breakpoint listed for line 3 (update, not duplicate):
    # the list_breakpoints block contains a single 'toy.py:3' entry line.
    # Match "condition=" (the literal text list_breakpoints emits), not a
    # bare "condition" substring: tmp_path is derived from this test's own
    # function name ("test_condition_reset_updates_..."), so every
    # transcript line that echoes the recorded file path (e.g. the
    # set_breakpoint command headers) spuriously contains "condition" too.
    listing = [l for l in out if "toy.py:3" in l and "condition=" in l]
    assert len(listing) == 1


async def test_recorder_file_round_trips(tmp_path):
    """Round-trip property (spec § Testing): a file written by the REAL
    SessionRecorder replays to the recorded stop-line sequence."""
    from tdb.session.recorder import SessionRecorder

    prog = tmp_path / "toy.py"
    prog.write_text(TOY)
    header = {
        "tdb_recording": 1,
        "created": "2026-07-31T00:00:00",
        "mode": "launch",
        "language": "python",
        "program": str(prog),
        "args": [],
        "cwd": str(tmp_path),
        "python": None,
        "adapter": None,
        "step_mode": "line",
        "no_just_my_code": False,
    }
    rec_path = tmp_path / "rt.jsonl"
    rec = SessionRecorder(str(rec_path), header)
    rec.record("set_breakpoint", [f"{prog}:3"])
    rec.record("continue", [])  # -> stops at toy.py:3
    rec.record("next", [])  # -> stops at toy.py:4
    rec.record("quit", [])
    rec.close()

    out: list[str] = []
    errors = await run_replay(load_recording(str(rec_path)), echo=out.append)
    text = "\n".join(out)
    assert errors == 0
    # Stop-location responses appear in recorded order: line 3, then 4.
    assert text.index("toy.py:3") < text.index("toy.py:4")


def test_replay_cli_flags_parse(tmp_path):
    from tdb.cli import parse_args

    args = parse_args(["--replay", "session.jsonl", "--timing"])
    assert args.replay == "session.jsonl"
    assert args.timing is True
    assert args.replay_timeout == 30.0


def test_replay_rejects_program_argument(capsys):
    from tdb.cli import parse_args

    with pytest.raises(SystemExit):
        parse_args(["--replay", "s.jsonl", "prog.py"])
    assert "--replay" in capsys.readouterr().err

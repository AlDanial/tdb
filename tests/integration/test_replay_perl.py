"""Replay is language-agnostic: a perl recording replays through the
perl DAP adapter."""

import json
import shutil
import subprocess

import pytest

from tdb.replay import load_recording, run_replay

pytestmark = pytest.mark.skipif(
    shutil.which("perl") is None
    or subprocess.run(["perl", "-e", "require v5.18"]).returncode != 0,
    reason="perl >= 5.18 required",
)

TOY = """\
my $x = 1;
my $y = 2;
my $z = $x + $y;
print "z=$z\\n";
"""


async def test_perl_recording_replays(tmp_path):
    prog = tmp_path / "toy.pl"
    prog.write_text(TOY)
    header = {
        "tdb_recording": 1,
        "created": "2026-07-31T00:00:00",
        "mode": "launch",
        "language": "perl",
        "program": str(prog),
        "args": [],
        "cwd": str(tmp_path),
        "python": None,
        "adapter": None,
        "step_mode": "line",
        "no_just_my_code": False,
    }
    records = [
        {"t": 0.1, "action": "set_breakpoint", "params": [f"{prog}:3"]},
        {"t": 0.2, "action": "continue", "params": []},
        {"t": 0.3, "action": "evaluate", "params": ["$x + $y"]},
        {"t": 0.4, "action": "quit", "params": []},
    ]
    path = tmp_path / "perl.jsonl"
    path.write_text(
        "\n".join([json.dumps(header)] + [json.dumps(r) for r in records]) + "\n"
    )
    out: list[str] = []
    errors = await run_replay(load_recording(str(path)), echo=out.append)
    text = "\n".join(out)
    assert errors == 0
    assert "ok: 3" in text  # $x + $y evaluated through perl5db

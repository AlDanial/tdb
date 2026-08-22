"""Replay is language-agnostic: a ruby recording replays through the
ruby proxy adapter."""

import json

import pytest

from tdb.replay import load_recording, run_replay
from tests.integration.ruby_adapter_harness import rdbg_ok

pytestmark = pytest.mark.skipif(not rdbg_ok(), reason="needs rdbg (debug gem >= 1.9)")

TOY = """\
x = 1
y = 2
z = x + y
puts "z=#{z}"
"""


async def test_ruby_recording_replays(tmp_path):
    prog = tmp_path / "toy.rb"
    prog.write_text(TOY)
    header = {
        "tdb_recording": 1,
        "created": "2026-08-21T00:00:00",
        "mode": "launch",
        "language": "ruby",
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
        {"t": 0.3, "action": "evaluate", "params": ["x + y"]},
        {"t": 0.4, "action": "quit", "params": []},
    ]
    path = tmp_path / "ruby.jsonl"
    path.write_text(
        "\n".join([json.dumps(header)] + [json.dumps(r) for r in records]) + "\n"
    )
    out: list[str] = []
    errors = await run_replay(load_recording(str(path)), echo=out.append)
    text = "\n".join(out)
    assert errors == 0
    assert "ok: 3" in text  # x + y evaluated through rdbg

"""Replay is language-agnostic: a PowerShell recording replays through
the PSES proxy adapter."""

import json

import pytest

from tdb.replay import load_recording, run_replay
from tests.integration.powershell_adapter_harness import pwsh_ok

pytestmark = pytest.mark.skipif(not pwsh_ok(), reason="needs pwsh + PSES")

TOY = """\
$x = 1
$y = 2
$z = $x + $y
Write-Host "z=$z"
"""


async def test_powershell_recording_replays(tmp_path):
    prog = tmp_path / "toy.ps1"
    prog.write_text(TOY)
    header = {
        "tdb_recording": 1,
        "created": "2026-09-03T00:00:00",
        "mode": "launch",
        "language": "powershell",
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
    path = tmp_path / "ps.jsonl"
    path.write_text(
        "\n".join([json.dumps(header)] + [json.dumps(r) for r in records]) + "\n"
    )
    out: list[str] = []
    errors = await run_replay(load_recording(str(path)), echo=out.append)
    text = "\n".join(out)
    assert errors == 0
    assert "ok: 3" in text  # $x + $y evaluated through PSES

"""`--no-pause-on-attach`: remote-attach opt-out of the pre-armed pause.

Used by `tdb.breakpoint()` (the debuggee stops itself via
`debugpy.breakpoint()`, so a pre-armed pause only races it) and available
to anyone whose program calls `debugpy.breakpoint()` right after
`debugpy.wait_for_client()`.
"""

from __future__ import annotations

import pytest

from tdb.cli import parse_args


def test_default_is_pause_on_attach():
    args = parse_args(["-r", "5678"])
    assert args.no_pause_on_attach is False


def test_flag_parses_with_remote_attach():
    args = parse_args(["-r", "localhost:5678", "--no-pause-on-attach"])
    assert args.no_pause_on_attach is True


def test_flag_requires_remote_attach(tmp_path):
    py = tmp_path / "x.py"
    py.write_text("print(1)\n")
    with pytest.raises(SystemExit):
        parse_args(["--no-pause-on-attach", str(py)])

"""Live regression for native OCaml's source-level GDB entry stop."""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess

import pytest

from tdb.languages.ocaml import build_ocaml_profile
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController


def _gdb_supports_dap() -> bool:
    gdb = shutil.which("gdb")
    if gdb is None:
        return False
    out = subprocess.run([gdb, "--version"], capture_output=True, text=True).stdout
    match = re.search(r"(\d+)\.\d+", out)
    return bool(match) and int(match.group(1)) >= 14


pytestmark = pytest.mark.skipif(
    shutil.which("ocamlopt") is None or not _gdb_supports_dap(),
    reason="ocamlopt and GDB >= 14 are required",
)

WAIT = 20.0
SOURCE = """\
let answer = 40 + 2
let () = Printf.printf "answer=%d\\n" answer
"""


@pytest.mark.asyncio
async def test_stop_on_entry_pauses_in_project_ocaml_source(tmp_path):
    source = tmp_path / "entry.ml"
    source.write_text(SOURCE)
    program = tmp_path / "entry.exe"
    subprocess.run(
        ["ocamlopt", "-g", "-o", str(program), str(source)],
        cwd=tmp_path,
        check=True,
    )

    handler = ServerEventHandler()
    controller = DebugController(
        handler,
        profile=build_ocaml_profile(adapter="gdb", program=str(program)),
    )
    try:
        await controller.start(
            program=str(program), cwd=str(tmp_path), stop_on_entry=True
        )
        await asyncio.wait_for(handler.initialized_event.wait(), WAIT)
        await asyncio.wait_for(controller.do_configure(), WAIT)
        assert await handler.wait_for_stop(timeout=WAIT)
        assert not handler.terminated_event.is_set()
        await controller.fetch_stop_info()

        frame = controller.state.stack_frames[0]
        assert frame.source is not None
        assert frame.source.path == str(source)
        assert frame.line in (1, 2)
    finally:
        try:
            await asyncio.wait_for(controller.stop(), WAIT)
        except Exception:
            pass

"""End-to-end: Console-view stdin reaches a debugpy-launched program.

Runs the controller against a real debugpy session with the TUI's
event handler replaced by a recording sink. The program is the shape
of examples/prompt_for_input.py: it blocks in input() and echoes what
it read, so the test proves the prompt (no trailing newline) reaches
the output sink before the answer is written, and that the answer
reaches the program.
"""

from __future__ import annotations

import asyncio

import pytest

from tdb.run_mode import ConsoleRunHandler
from tdb.session.controller import DebugController

PROMPT_SCRIPT = 'x = input("enter a value: ")\nprint(f"You entered: {x}")\n'
EOF_SCRIPT = "try:\n    input()\nexcept EOFError:\n    print('got eof')\n"


class _Sink(ConsoleRunHandler):
    def __init__(self) -> None:
        super().__init__()
        self.output: list[tuple[str, str]] = []

    def on_output(self, text: str, category: str) -> None:
        self.output.append((category, text))

    def text(self, category: str = "stdout") -> str:
        return "".join(t for c, t in self.output if c == category)


async def _wait_until(pred, timeout=20.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        assert loop.time() < deadline, "condition not met in time"
        await asyncio.sleep(0.05)


@pytest.fixture
async def session(tmp_path):
    sink = _Sink()
    ctrl = DebugController(sink)

    async def start(script: str):
        p = tmp_path / "prog.py"
        p.write_text(script)
        await ctrl.start(program=str(p), cwd=str(tmp_path))
        await asyncio.wait_for(sink.initialized.wait(), 20)
        await ctrl.do_configure()
        return sink, ctrl

    yield start
    # Always stop: releases the adapter subprocess and the pipes even
    # when the program has already exited.
    await ctrl.stop()


async def test_prompt_then_answer_round_trips(session):
    sink, ctrl = await session(PROMPT_SCRIPT)
    assert ctrl.supports_stdin
    await _wait_until(lambda: sink.text() == "enter a value: ")
    await ctrl.write_stdin("42\n")
    await _wait_until(lambda: "You entered: 42" in sink.text())
    await asyncio.wait_for(sink.exited.wait(), 20)
    assert sink.exit_code == 0


async def test_close_stdin_delivers_eof(session):
    sink, ctrl = await session(EOF_SCRIPT)
    await ctrl.close_stdin()
    await _wait_until(lambda: "got eof" in sink.text())
    await asyncio.wait_for(sink.exited.wait(), 20)
    assert sink.exit_code == 0

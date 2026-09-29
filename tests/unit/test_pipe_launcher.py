"""Unit tests for PipeLauncher: the runInTerminal handler tdb installs
when no --terminal is given for an adapter whose debuggee stdin can
only be owned by spawning the debuggee ourselves (debugpy).

A real shell stands in for debugpy's launcher command: it reads one
line from stdin and echoes it back, so the test proves both directions
of the pipe -- stdin written through `write_stdin` reaches the child,
and the child's stdout/stderr come back through the output callback.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from tdb.dap.messages import Request
from tdb.session.terminal import PipeLauncher

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="uses /bin/sh as the fake debuggee"
)


async def _wait_for(pred, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        assert loop.time() < deadline, "condition not met in time"
        await asyncio.sleep(0.02)


def _request(script: str, cwd: str) -> Request:
    return Request(
        seq=1,
        command="runInTerminal",
        arguments={"args": ["/bin/sh", "-c", script], "cwd": cwd},
    )


async def test_stdin_written_through_launcher_reaches_child(tmp_path):
    outputs: list[tuple[str, str]] = []
    launcher = PipeLauncher(on_output=lambda text, cat: outputs.append((cat, text)))
    body = await launcher.handle_run_in_terminal(
        _request("read x; echo got:$x; echo oops >&2", str(tmp_path))
    )
    assert body == {}
    launcher.write_stdin("hello\n")
    await _wait_for(lambda: ("stdout", "got:hello\n") in outputs)
    await _wait_for(lambda: ("stderr", "oops\n") in outputs)
    await launcher.close()


async def test_prompt_without_newline_is_delivered(tmp_path):
    """input('prompt: ') writes no newline; the pump must not wait for one."""
    outputs: list[str] = []
    launcher = PipeLauncher(on_output=lambda text, cat: outputs.append(text))
    await launcher.handle_run_in_terminal(
        _request("printf 'enter: '; read x", str(tmp_path))
    )
    await _wait_for(lambda: "".join(outputs) == "enter: ")
    launcher.write_stdin("1\n")
    await launcher.close()


async def test_close_stdin_sends_eof(tmp_path):
    outputs: list[str] = []
    launcher = PipeLauncher(on_output=lambda text, cat: outputs.append(text))
    await launcher.handle_run_in_terminal(
        _request("if read x; then echo line; else echo eof; fi", str(tmp_path))
    )
    launcher.close_stdin()
    await _wait_for(lambda: "eof\n" in outputs)
    await launcher.close()


async def test_write_after_child_exit_raises(tmp_path):
    launcher = PipeLauncher(on_output=lambda text, cat: None)
    await launcher.handle_run_in_terminal(_request("exit 0", str(tmp_path)))
    await _wait_for(lambda: launcher.exited)
    with pytest.raises(RuntimeError):
        launcher.write_stdin("x\n")
    await launcher.close()


async def test_env_from_request_is_merged_and_unbuffered(tmp_path):
    outputs: list[str] = []
    launcher = PipeLauncher(on_output=lambda text, cat: outputs.append(text))
    req = _request('echo "$FOO/$PYTHONUNBUFFERED"', str(tmp_path))
    req.arguments["env"] = {"FOO": "bar"}
    await launcher.handle_run_in_terminal(req)
    await _wait_for(lambda: "bar/1\n" in outputs)
    await launcher.close()

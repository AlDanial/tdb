"""Terminal-mode (runInTerminal) launches of the tcsh adapter."""

import asyncio

import pytest

from tdb.adapters.tcsh.guardian import _process_is_gone
from tests.integration.tcsh_dap_client import DAPClient
from tests.integration.test_tcsh_adapter import configure, stack_frames


@pytest.mark.asyncio
async def test_terminal_launch_steps_and_reports_exit_code(
    dap_client: DAPClient, tcsh_path, tcsh_fixtures_dir, tmp_path
) -> None:
    program = tmp_path / "t.csh"
    program.write_text("set x = 1\nset y = 2\nexit 4\n")
    spawned: list[asyncio.subprocess.Process] = []

    async def spawn(message):
        args = message["arguments"]
        assert args["kind"] == "external"
        # start_new_session=True mirrors what a real terminal emulator does
        # when it opens a new window/pty for the spawned command (a fresh
        # session/process-group leader) -- guardian.py's own path-mode
        # unit tests spawn it the same way (see
        # test_guardian_path_mode_handshake_pid_and_exit_status). Without
        # this the guardian inherits this TEST's own session, and its
        # post-exit "wait for the session to drain" step
        # (_wait_for_drain_or_termination) would never see that session
        # empty out -- the whole pytest/shell process tree is still in it
        # -- and hang forever waiting to report the debuggee's exit code.
        proc = await asyncio.create_subprocess_exec(
            *args["args"],
            cwd=args.get("cwd"),
            env=args.get("env") or None,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        spawned.append(proc)
        return {}

    dap_client.on_reverse_request = spawn
    await dap_client.request(
        "initialize",
        {"adapterID": "tcsh", "supportsRunInTerminalRequest": True},
    )
    await dap_client.wait_for_event("initialized")
    await dap_client.launch(
        program, tcshPath=str(tcsh_path), console="externalTerminal"
    )
    await configure(dap_client)
    stopped = await dap_client.wait_for_event("stopped")
    assert stopped["body"]["reason"] == "entry"
    frames = await stack_frames(dap_client)
    assert frames[0]["line"] == 1
    assert (await dap_client.request("next", {"threadId": 1}))["success"]
    await dap_client.wait_for_event("stopped")
    assert (await dap_client.request("continue", {"threadId": 1}))["success"]
    exited = await dap_client.wait_for_event("exited", timeout=15)
    assert exited["body"]["exitCode"] == 4
    await dap_client.wait_for_event("terminated")
    assert spawned and await spawned[0].wait() == 4


@pytest.mark.asyncio
async def test_terminal_launch_without_capability_fails(
    dap_client: DAPClient, tcsh_path, tmp_path
) -> None:
    program = tmp_path / "t.csh"
    program.write_text("set x = 1\n")
    await dap_client.initialize()
    response = await dap_client.request(
        "launch",
        {
            "program": str(program),
            "tcshPath": str(tcsh_path),
            "console": "externalTerminal",
        },
    )
    assert response["success"] is False
    assert "runInTerminal" in response["message"]


@pytest.mark.asyncio
async def test_terminal_launch_terminate_kills_debuggee(
    dap_client: DAPClient, tcsh_path, tmp_path
) -> None:
    """A `terminate` sent after the entry stop must force-kill the real,
    client-spawned OS process (the guardian, which owns tcsh's whole
    session generation) -- the whole point of the path-mode guardian
    handshake in terminal mode, since the adapter never owns this process
    as a reapable child of its own."""

    program = tmp_path / "long.csh"
    program.write_text("set x = 1\nsleep 30\nset y = 2\n")
    spawned: list[asyncio.subprocess.Process] = []

    async def spawn(message):
        args = message["arguments"]
        assert args["kind"] == "external"
        # start_new_session=True mirrors what a real terminal emulator does
        # when it opens a new window/pty for the spawned command (a fresh
        # session/process-group leader) -- guardian.py's own path-mode
        # unit tests spawn it the same way (see
        # test_guardian_path_mode_handshake_pid_and_exit_status). Without
        # this the guardian inherits this TEST's own session, and its
        # post-exit "wait for the session to drain" step
        # (_wait_for_drain_or_termination) would never see that session
        # empty out -- the whole pytest/shell process tree is still in it
        # -- and hang forever waiting to report the debuggee's exit code.
        proc = await asyncio.create_subprocess_exec(
            *args["args"],
            cwd=args.get("cwd"),
            env=args.get("env") or None,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        spawned.append(proc)
        return {}

    dap_client.on_reverse_request = spawn
    await dap_client.request(
        "initialize",
        {"adapterID": "tcsh", "supportsRunInTerminalRequest": True},
    )
    await dap_client.wait_for_event("initialized")
    await dap_client.launch(
        program, tcshPath=str(tcsh_path), console="externalTerminal"
    )
    await configure(dap_client)
    await dap_client.wait_for_event("stopped")
    assert spawned
    guardian_pid = spawned[0].pid

    response = await dap_client.request("terminate", {})
    assert response["success"] is True

    # The test owns spawned[0] (it spawned it directly via the reverse
    # request), so waiting on it reaps reliably regardless of an init-less
    # environment. Bounded so a debuggee that somehow survives can't hang
    # the test forever.
    await asyncio.wait_for(spawned[0].wait(), 10)
    await dap_client.wait_for_event("terminated")

    # Belt-and-suspenders OS-level confirmation. NOT a bare
    # os.kill(pid, 0)/ProcessLookupError probe: in an init-less CI
    # container an unreaped zombie keeps its pid live to that probe
    # forever. _process_is_gone treats an unreaped zombie as dead too.
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        if _process_is_gone(guardian_pid):
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail(
            f"guardian pid {guardian_pid} was not confirmed dead after terminate"
        )

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from pathlib import Path

import pytest

from tests.integration.tcsh_dap_client import DAPClient


async def configure(client: DAPClient) -> None:
    response = await client.request("configurationDone", {})
    assert response["success"] is True


async def stack_frames(client: DAPClient) -> list[dict[str, object]]:
    response = await client.request("stackTrace", {"threadId": 1})
    assert response["success"] is True
    return response["body"]["stackFrames"]


@pytest.mark.asyncio
async def test_stop_on_entry_precedes_first_command(
    dap_client: DAPClient,
    tcsh_path: Path,
    tcsh_fixtures_dir: Path,
) -> None:
    await dap_client.initialize()
    await dap_client.launch(
        tcsh_fixtures_dir / "stepping.csh",
        tcshPath=str(tcsh_path),
    )
    await configure(dap_client)

    stopped = await dap_client.wait_for_event("stopped")
    stack = await dap_client.request("stackTrace", {"threadId": 1})

    assert stopped["body"]["reason"] == "entry"
    assert stack["body"]["stackFrames"][0]["source"]["path"].endswith("stepping.csh")
    assert not dap_client.output_contains("first command ran")


@pytest.mark.asyncio
async def test_breakpoint_relocation_and_next_suppress_sourced_probes(
    dap_client: DAPClient,
    tcsh_path: Path,
    tcsh_fixtures_dir: Path,
) -> None:
    program = (tcsh_fixtures_dir / "stepping.csh").resolve()
    await dap_client.initialize()
    await dap_client.launch(program, tcshPath=str(tcsh_path), stopOnEntry=False)
    breakpoints = await dap_client.request(
        "setBreakpoints",
        {"source": {"path": str(program)}, "breakpoints": [{"line": 1}]},
    )
    assert breakpoints["success"] is True
    assert breakpoints["body"]["breakpoints"][0]["line"] == 2
    await configure(dap_client)

    stopped = await dap_client.wait_for_event("stopped")
    assert stopped["body"]["reason"] == "breakpoint"
    frames = await stack_frames(dap_client)
    assert (frames[0]["source"]["path"], frames[0]["line"]) == (str(program), 2)
    assert not dap_client.output_contains("first command ran")
    threads = await dap_client.request("threads", {})
    assert threads["body"]["threads"] == [{"id": 1, "name": "tcsh"}]

    assert (await dap_client.request("next", {"threadId": 1}))["success"] is True
    frames = await stack_frames_after_stop(dap_client)
    assert (frames[0]["source"]["path"], frames[0]["line"]) == (str(program), 3)
    await dap_client.wait_for_output("first command ran", category="stdout")

    assert (await dap_client.request("next", {"threadId": 1}))["success"] is True
    frames = await stack_frames_after_stop(dap_client)
    assert (frames[0]["source"]["path"], frames[0]["line"]) == (str(program), 4)
    await dap_client.wait_for_output("library first", category="stdout")
    await dap_client.wait_for_output("library second", category="stdout")

    assert (await dap_client.request("continue", {"threadId": 1}))["success"] is True
    exited = await dap_client.wait_for_event("exited")
    await dap_client.wait_for_event("terminated")
    assert exited["body"]["exitCode"] == 0


async def stack_frames_after_stop(client: DAPClient) -> list[dict[str, object]]:
    await client.wait_for_event("stopped")
    return await stack_frames(client)


async def stopped_frame_id(client: DAPClient) -> int:
    await client.wait_for_event("stopped")
    frames = await stack_frames(client)
    return frames[0]["id"]


def variable_pairs(response: dict[str, object]) -> set[tuple[object, object]]:
    return {
        (variable["name"], variable["value"])
        for variable in response["body"]["variables"]
    }


def dap_workspaces() -> set[Path]:
    return set(Path(tempfile.gettempdir()).glob("tcsh-dap-*"))


async def wait_until(predicate, *, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true before timeout")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_step_in_and_out_report_current_first_original_source_stack(
    dap_client: DAPClient,
    tcsh_path: Path,
    tcsh_fixtures_dir: Path,
) -> None:
    program = (tcsh_fixtures_dir / "stepping.csh").resolve()
    library = (tcsh_fixtures_dir / "library.csh").resolve()
    await dap_client.initialize()
    await dap_client.launch(program, tcshPath=str(tcsh_path))
    await configure(dap_client)
    await dap_client.wait_for_event("stopped")

    assert (await dap_client.request("next", {"threadId": 1}))["success"] is True
    frames = await stack_frames_after_stop(dap_client)
    assert (frames[0]["source"]["path"], frames[0]["line"]) == (str(program), 3)

    assert (await dap_client.request("stepIn", {"threadId": 1}))["success"] is True
    frames = await stack_frames_after_stop(dap_client)
    assert [frame["source"]["path"] for frame in frames] == [
        str(library),
        str(program),
    ]
    assert [frame["line"] for frame in frames] == [1, 3]

    assert (await dap_client.request("stepOut", {"threadId": 1}))["success"] is True
    frames = await stack_frames_after_stop(dap_client)
    assert [(frame["source"]["path"], frame["line"]) for frame in frames] == [
        (str(program), 4)
    ]

    assert (await dap_client.request("continue", {"threadId": 1}))["success"] is True
    await dap_client.wait_for_event("terminated")


@pytest.mark.asyncio
async def test_scopes_and_evaluate_observe_live_tcsh_state(
    dap_client: DAPClient,
    tcsh_path: Path,
    tcsh_fixtures_dir: Path,
) -> None:
    program = (tcsh_fixtures_dir / "scopes.csh").resolve()
    await dap_client.initialize()
    await dap_client.launch(
        program,
        tcshPath=str(tcsh_path),
        args=["first", "two words"],
        stopOnEntry=False,
    )
    breakpoints = await dap_client.request(
        "setBreakpoints",
        {"source": {"path": str(program)}, "breakpoints": [{"line": 6}]},
    )
    assert breakpoints["body"]["breakpoints"][0]["line"] == 6
    await configure(dap_client)
    frame_id = await stopped_frame_id(dap_client)

    scopes_response = await dap_client.request("scopes", {"frameId": frame_id})
    scopes = scopes_response["body"]["scopes"]
    assert [scope["name"] for scope in scopes] == [
        "Shell Variables",
        "Environment",
        "Aliases",
        "Arguments",
    ]
    values = {
        scope["name"]: await dap_client.request(
            "variables", {"variablesReference": scope["variablesReference"]}
        )
        for scope in scopes
    }
    assert ("name", "original") in variable_pairs(values["Shell Variables"])
    assert ("items", "(one two words)") in variable_pairs(values["Shell Variables"])
    assert ("TCSH_DAP_SCOPE_VALUE", "environment value") in variable_pairs(
        values["Environment"]
    )
    assert ("greeting", "echo hello world") in variable_pairs(values["Aliases"])
    assert variable_pairs(values["Arguments"]) == {("argv", "(first two words)")}

    observed = await dap_client.request(
        "evaluate", {"expression": 'echo "$name"', "frameId": frame_id}
    )
    assert observed["body"]["result"] == "original\n"
    changed = await dap_client.request(
        "evaluate", {"expression": "set name = changed", "frameId": frame_id}
    )
    assert changed["success"] is True
    observed = await dap_client.request(
        "evaluate", {"expression": 'echo "$name"', "frameId": frame_id}
    )
    assert observed["body"]["result"] == "changed\n"


@pytest.mark.asyncio
async def test_evaluate_syntax_error_fails_or_terminates_without_hanging(
    dap_client: DAPClient,
    tcsh_path: Path,
    tcsh_fixtures_dir: Path,
) -> None:
    program = (tcsh_fixtures_dir / "scopes.csh").resolve()
    await dap_client.initialize()
    await dap_client.launch(program, tcshPath=str(tcsh_path), stopOnEntry=False)
    breakpoints = await dap_client.request(
        "setBreakpoints",
        {"source": {"path": str(program)}, "breakpoints": [{"line": 6}]},
    )
    assert breakpoints["body"]["breakpoints"][0]["line"] == 6
    await configure(dap_client)
    frame_id = await stopped_frame_id(dap_client)

    response = await dap_client.request(
        "evaluate",
        {"expression": "if (", "frameId": frame_id},
        timeout=8,
    )
    if response["success"] is False:
        stack = await dap_client.request("stackTrace", {"threadId": 1})
        if stack["success"] is False:
            await dap_client.wait_for_event("terminated", timeout=5)
    else:
        await dap_client.wait_for_event("terminated", timeout=5)


@pytest.mark.asyncio
async def test_output_categories_and_nonzero_exit_status(
    dap_client: DAPClient,
    tcsh_path: Path,
    tmp_path: Path,
) -> None:
    program = tmp_path / "output-and-exit.csh"
    program.write_text(
        '#!/usr/bin/tcsh -f\necho "standard output"\n'
        "/bin/sh -c 'echo standard error >&2'\nexit 7\n"
    )
    await dap_client.initialize()
    await dap_client.launch(program, tcshPath=str(tcsh_path), stopOnEntry=False)
    await configure(dap_client)

    await dap_client.wait_for_output("standard output", category="stdout")
    await dap_client.wait_for_output("standard error", category="stderr")
    exited = await dap_client.wait_for_event("exited")
    await dap_client.wait_for_event("terminated")
    assert exited["body"]["exitCode"] == 7


@pytest.mark.asyncio
async def test_program_path_with_spaces_and_verbatim_arguments(
    dap_client: DAPClient,
    tcsh_path: Path,
    tmp_path: Path,
) -> None:
    directory = tmp_path / "directory with spaces"
    directory.mkdir()
    program = directory / "argument script.csh"
    program.write_text(
        '#!/usr/bin/tcsh -f\necho "argc=$#argv"\n'
        "/usr/bin/printf 'arg1=<%s>\\n' \"$argv[1]\"\n"
        "/usr/bin/printf 'arg2=<%s>\\n' \"$argv[2]\"\n"
        "/usr/bin/printf 'arg3=<%s>\\n' \"$argv[3]\"\n"
        "/usr/bin/printf 'arg4=<%s>\\n' \"$argv[4]\"\n"
        "/usr/bin/printf 'arg5=<%s>\\n' \"$argv[5]\"\n"
        "exit 0\n"
    )
    arguments = ["", "*", "semi;colon", "two words", "$dollar"]
    await dap_client.initialize()
    await dap_client.launch(
        program,
        tcshPath=str(tcsh_path),
        stopOnEntry=False,
        args=arguments,
    )
    await configure(dap_client)

    await dap_client.wait_for_event("terminated")
    output = "".join(
        str(message["body"]["output"])
        for message in dap_client.messages
        if message.get("type") == "event"
        and message.get("event") == "output"
        and message["body"]["category"] == "stdout"
    )
    assert "argc=5\n" in output
    assert [
        f"arg{index}=<{argument}>" for index, argument in enumerate(arguments, 1)
    ] == [
        line
        for line in output.splitlines()
        if line.startswith("arg") and not line.startswith("argc")
    ]


@pytest.mark.asyncio
async def test_disconnect_terminates_debuggee_and_removes_workspace(
    dap_client: DAPClient,
    tcsh_path: Path,
    tcsh_fixtures_dir: Path,
) -> None:
    before = dap_workspaces()
    await dap_client.initialize()
    await dap_client.launch(
        tcsh_fixtures_dir / "stepping.csh",
        tcshPath=str(tcsh_path),
    )
    await configure(dap_client)
    await dap_client.wait_for_event("stopped")
    created = dap_workspaces() - before
    assert len(created) == 1

    assert (await dap_client.request("disconnect", {}))["success"] is True
    await dap_client.wait_for_event("terminated")
    await wait_until(lambda: not any(path.exists() for path in created))


@pytest.mark.asyncio
async def test_terminate_kills_process_group_and_removes_workspace(
    dap_client: DAPClient,
    tcsh_path: Path,
    tcsh_fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    child_pid_file = tmp_path / "child.pid"
    before = dap_workspaces()
    await dap_client.initialize()
    await dap_client.launch(
        tcsh_fixtures_dir / "long_running.csh",
        tcshPath=str(tcsh_path),
        stopOnEntry=False,
        args=[str(child_pid_file)],
    )
    await configure(dap_client)
    await dap_client.wait_for_output("long running ready", category="stdout")
    await wait_until(child_pid_file.exists)
    child_pid = int(child_pid_file.read_text().strip())
    os.kill(child_pid, 0)
    created = dap_workspaces() - before
    assert len(created) == 1

    assert (await dap_client.request("terminate", {}, timeout=8))["success"] is True
    await dap_client.wait_for_event("terminated")

    def child_is_gone() -> bool:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return True
        return False

    await wait_until(child_is_gone)
    await wait_until(lambda: not any(path.exists() for path in created))


@pytest.mark.asyncio
async def test_one_thousand_probes_finish_without_deadlock(
    dap_client: DAPClient,
    tcsh_path: Path,
    tmp_path: Path,
) -> None:
    program = tmp_path / "thousand-probes.csh"
    program.write_text(
        "#!/usr/bin/tcsh -f\nset count = 0\n" + "@ count++\n" * 1000 + "exit 0\n"
    )
    before = dap_workspaces()
    await dap_client.initialize()
    await dap_client.launch(program, tcshPath=str(tcsh_path), stopOnEntry=False)
    await configure(dap_client)
    created = dap_workspaces() - before
    assert len(created) == 1

    exited = await dap_client.wait_for_event("exited", timeout=30)
    await dap_client.wait_for_event("terminated", timeout=30)
    assert exited["body"]["exitCode"] == 0
    await wait_until(lambda: not any(path.exists() for path in created))

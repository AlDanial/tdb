"""Console input reaching a debugged tcsh script's stdin via `tdbStdin`."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.tcsh_dap_client import DAPClient


async def launch_prompting_script(
    client: DAPClient, tcsh_path: Path, fixtures: Path
) -> None:
    await client.initialize()
    await client.launch(
        fixtures / "stdin_prompt.csh",
        tcshPath=str(tcsh_path),
        stopOnEntry=False,
    )
    response = await client.request("configurationDone", {})
    assert response["success"] is True


@pytest.mark.asyncio
async def test_console_text_reaches_script_stdin(
    dap_client: DAPClient,
    tcsh_path: Path,
    tcsh_fixtures_dir: Path,
) -> None:
    await launch_prompting_script(dap_client, tcsh_path, tcsh_fixtures_dir)

    # The prompt has no trailing newline: the adapter must deliver a
    # partial (unterminated) chunk while `$<` is still blocked on stdin.
    prompt = await dap_client.wait_for_output("enter: ", category="stdout")
    assert str(prompt["body"]["output"]).endswith("enter: ")
    assert not dap_client.output_contains("got:")

    response = await dap_client.request("tdbStdin", {"text": "42\n"})
    assert response["success"] is True
    assert response["body"] == {}

    await dap_client.wait_for_output("got:42", category="stdout")
    await dap_client.wait_for_event("terminated")


@pytest.mark.asyncio
async def test_console_eof_gives_script_empty_read(
    dap_client: DAPClient,
    tcsh_path: Path,
    tcsh_fixtures_dir: Path,
) -> None:
    await launch_prompting_script(dap_client, tcsh_path, tcsh_fixtures_dir)
    await dap_client.wait_for_output("enter: ", category="stdout")

    response = await dap_client.request("tdbStdin", {"eof": True})
    assert response["success"] is True
    assert response["body"] == {}
    # A repeated EOF is a harmless no-op, not an error.
    response = await dap_client.request("tdbStdin", {"eof": True})
    assert response["success"] is True

    await dap_client.wait_for_output("eof", category="stdout")
    await dap_client.wait_for_event("terminated")

    # Once stdin is closed (and the program gone) text has nowhere to go.
    response = await dap_client.request("tdbStdin", {"text": "late\n"})
    assert response["success"] is False
    assert response["message"]


@pytest.mark.asyncio
async def test_stdin_before_launch_is_an_error_response(
    dap_client: DAPClient,
) -> None:
    await dap_client.initialize()

    response = await dap_client.request("tdbStdin", {"text": "42\n"})
    assert response["success"] is False
    assert "launch" in response["message"]

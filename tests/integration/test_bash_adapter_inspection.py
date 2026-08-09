"""DAP-level: stackTrace/scopes/variables/evaluate."""

import pytest

from tests.integration.bash_adapter_harness import (
    FIXTURES,
    bash_ok,
    launch_stopped,
    start_bash_adapter,
)

pytestmark = pytest.mark.skipif(not bash_ok(), reason="needs bash >= 4.4")


@pytest.mark.asyncio
async def test_scopes_and_variables_with_array_children():
    client = await start_bash_adapter()
    try:
        program = str(FIXTURES / "bash_arrays.sh")
        # DEVIATION from the brief: the brief breaks at line 4. That was
        # true when the brief was written, but 92bb3dc (anchoring
        # _INTERNAL_VARS) inserted four HISTORY/EPOCH_START/SHELLCHECK_OPTS/
        # FUNCNEST lines above the echo, pushing it from line 4 to line 8 --
        # the same line test_bash_session.py's
        # test_globals_include_arrays_and_filter_internals already uses.
        # Plain miscounted line number, not a behavior change.
        await launch_stopped(
            client, program, breakpoints=[{"line": 8}], stop_on_entry=False
        )
        await client.wait_event("stopped")
        frames = (await client.request("stackTrace", {"threadId": 1}))["body"][
            "stackFrames"
        ]
        assert frames[0]["name"] == "main"
        scopes = (await client.request("scopes", {"frameId": 0}))["body"]["scopes"]
        names = [s["name"] for s in scopes]
        assert names == ["Locals", "Globals", "Environment"]
        globals_ref = scopes[1]["variablesReference"]
        gvars = (
            await client.request("variables", {"variablesReference": globals_ref})
        )["body"]["variables"]
        by_name = {v["name"]: v for v in gvars}
        assert by_name["fruits"]["value"] == "array[3]"
        assert by_name["fruits"]["variablesReference"] > 0
        children = (
            await client.request(
                "variables",
                {"variablesReference": by_name["fruits"]["variablesReference"]},
            )
        )["body"]["variables"]
        assert {"name": "1", "value": '"banana"', "variablesReference": 0} in children
        await client.request("continue", {"threadId": 1})
        await client.wait_event("exited")
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_scopes_per_frame():
    client = await start_bash_adapter()
    try:
        program = str(FIXTURES / "bash_functions.sh")
        await launch_stopped(
            client, program, breakpoints=[{"line": 3}], stop_on_entry=False
        )
        await client.wait_event("stopped")
        scopes0 = (await client.request("scopes", {"frameId": 0}))["body"]["scopes"]
        assert [s["name"] for s in scopes0] == ["Locals", "Globals", "Environment"]
        scopes1 = (await client.request("scopes", {"frameId": 1}))["body"]["scopes"]
        assert [s["name"] for s in scopes1] == ["Globals", "Environment"]
        await client.request("continue", {"threadId": 1})
        await client.wait_event("exited")
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_scopes_rejects_out_of_range_frame_id():
    """Finding 4: a frameId beyond the last known stack frame (e.g. a
    stale client-cached id) must be rejected with an error response
    instead of silently returning a Globals-only scope list."""
    client = await start_bash_adapter()
    try:
        program = str(FIXTURES / "bash_functions.sh")
        await launch_stopped(
            client, program, breakpoints=[{"line": 3}], stop_on_entry=False
        )
        await client.wait_event("stopped")
        # populate self._stack_cache
        await client.request("stackTrace", {"threadId": 1})
        resp = await client.request("scopes", {"frameId": 99})
        assert resp["success"] is False
        await client.request("continue", {"threadId": 1})
        await client.wait_event("exited")
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_evaluate_mutates_debuggee():
    client = await start_bash_adapter()
    try:
        program = str(FIXTURES / "bash_hello.sh")
        await launch_stopped(
            client, program, breakpoints=[{"line": 3}], stop_on_entry=False
        )
        await client.wait_event("stopped")
        await client.request("evaluate", {"expression": "x=42", "context": "repl"})
        result = (
            await client.request(
                "evaluate", {"expression": 'echo "x=$x"', "context": "repl"}
            )
        )["body"]["result"]
        assert "x=42" in result
        await client.request("continue", {"threadId": 1})
        await client.wait_event("exited")
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_environment_scope_listed_and_populated():
    client = await start_bash_adapter()
    try:
        program = str(FIXTURES / "bash_env_scopes.sh")
        await launch_stopped(
            client, program, breakpoints=[{"line": 3}], stop_on_entry=False
        )
        await client.wait_event("stopped")
        await client.request("stackTrace", {"threadId": 1})
        scopes = (await client.request("scopes", {"frameId": 0}))["body"]["scopes"]
        assert [s["name"] for s in scopes] == ["Locals", "Globals", "Environment"]
        env_ref = scopes[2]["variablesReference"]
        env = {
            v["name"]: v
            for v in (
                await client.request("variables", {"variablesReference": env_ref})
            )["body"]["variables"]
        }
        assert "exported_var" in env
        assert "PATH" in env
        assert "plain_var" not in env
        globals_ref = scopes[1]["variablesReference"]
        gvars = {
            v["name"]
            for v in (
                await client.request("variables", {"variablesReference": globals_ref})
            )["body"]["variables"]
        }
        assert "plain_var" in gvars
        assert "exported_var" not in gvars
        await client.request("continue", {"threadId": 1})
        await client.wait_event("exited")
    finally:
        await client.stop()

"""Scripted DAP client for the PowerShell proxy + shared launch helper."""

import shutil
from pathlib import Path

from tdb.adapters.powershell.locate import find_pses
from tests.integration.perl_adapter_harness import AdapterClient

FIXTURES = Path(__file__).parent / "fixtures" / "powershell"


def pwsh_ok() -> bool:
    """pwsh on PATH and a resolvable PSES module (config/env/VS Code)."""
    if shutil.which("pwsh") is None:
        return False
    try:
        find_pses(None)
    except FileNotFoundError:
        return False
    return True


async def start_powershell_adapter() -> AdapterClient:
    client = AdapterClient()
    await client.start(module="tdb.adapters.powershell")
    await client.request(
        "initialize",
        {"adapterID": "pses", "linesStartAt1": True, "columnsStartAt1": True},
    )
    return client


async def launch_stopped(
    client: AdapterClient, program: str, breakpoints=None, stop_on_entry=True, args=None
):
    """launch -> initialized -> [setBreakpoints] -> configurationDone."""
    launch_fut = client.send(
        "launch",
        {
            "type": "powershell",
            "request": "launch",
            "program": program,
            "args": list(args or []),
            "cwd": str(Path(program).parent),
            "stopOnEntry": stop_on_entry,
            "console": "internalConsole",
        },
    )
    await client.wait_event("initialized")
    if breakpoints:
        await client.request(
            "setBreakpoints",
            {"source": {"path": program}, "breakpoints": breakpoints},
        )
    await client.request("configurationDone")
    await launch_fut


def output_text(client: AdapterClient, category: str | None = None) -> str:
    return "".join(
        e["body"].get("output", "")
        for e in list(client.events)
        if e["event"] == "output"
        and (category is None or e["body"].get("category") == category)
    )

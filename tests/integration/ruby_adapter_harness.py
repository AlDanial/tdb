"""Scripted DAP client for the ruby proxy + shared launch helper."""

import shutil
import subprocess
from pathlib import Path

from tests.integration.perl_adapter_harness import AdapterClient

FIXTURES = Path(__file__).parent / "fixtures"


def rdbg_ok() -> bool:
    """rdbg present and debug gem >= 1.9."""
    rdbg = shutil.which("rdbg")
    if not rdbg:
        return False
    try:
        cp = subprocess.run(
            [rdbg, "--version"], capture_output=True, text=True, check=True
        )
        # "rdbg 1.11.1"
        parts = cp.stdout.split()[-1].split(".")
        major, minor = int(parts[0]), int(parts[1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return False
    return (major, minor) >= (1, 9)


async def start_ruby_adapter() -> AdapterClient:
    client = AdapterClient()
    await client.start(module="tdb.adapters.ruby")
    await client.request(
        "initialize",
        {"adapterID": "rdbg", "linesStartAt1": True, "columnsStartAt1": True},
    )
    return client


async def launch_stopped(
    client: AdapterClient, program: str, breakpoints=None, stop_on_entry=True
):
    """Standard DAP dance: launch -> initialized -> [setBreakpoints] ->
    configurationDone; returns after both responses arrive."""
    launch_fut = client.send(
        "launch",
        {
            "type": "ruby",
            "request": "launch",
            "program": program,
            "args": [],
            "cwd": str(Path(program).parent),
            "stopOnEntry": stop_on_entry,
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

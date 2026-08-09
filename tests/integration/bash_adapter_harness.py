"""Scripted DAP client for the bash adapter + shared launch helper."""

import shutil
import subprocess
from pathlib import Path

from tests.integration.perl_adapter_harness import AdapterClient

FIXTURES = Path(__file__).parent / "fixtures"


def bash_ok() -> bool:
    bash = shutil.which("bash")
    if not bash:
        return False
    out = subprocess.run(
        [bash, "-c", 'echo "${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"'],
        capture_output=True,
        text=True,
    ).stdout.strip()
    major, minor = (int(p) for p in out.split("."))
    return (major, minor) >= (4, 4)


async def start_bash_adapter() -> AdapterClient:
    client = AdapterClient()
    await client.start(module="tdb.adapters.bash")
    await client.request("initialize", {"adapterID": "bash-tdb"})
    return client


async def launch_stopped(
    client: AdapterClient, program: str, breakpoints=None, stop_on_entry=True
):
    """Standard DAP dance: launch -> initialized -> [setBreakpoints] ->
    configurationDone; returns after both responses arrive."""
    launch_fut = client.send(
        "launch",
        {
            "type": "bash",
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
            {
                "source": {"path": program},
                "breakpoints": breakpoints,
            },
        )
    await client.request("configurationDone")
    await launch_fut

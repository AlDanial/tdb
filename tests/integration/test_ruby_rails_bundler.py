"""E2E coverage for launching a bundled Rails app through the rdbg bridge.

The bridge's `useBundler` launch option runs the program via
`bundle exec ruby` (rdbg command mode). This test boots a minimal committed
Rails app fixture that way and verifies the whole path: entry stop (proving
bundler resolution + Rails boot), stack inspection, continue to natural
termination, and the synthesized `exited` event with exit code 0.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from tdb.dap.client import DAPClient
from tdb.languages.ruby import build_ruby_profile

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ruby_rails"
PROGRAM = FIXTURE / "bin" / "e2e.rb"


def _bundler_ready() -> bool:
    if shutil.which("bundle") is None:
        return False
    result = subprocess.run(
        ["bundle", "check"], cwd=str(FIXTURE), capture_output=True
    )
    return result.returncode == 0


pytestmark = pytest.mark.skipif(
    shutil.which("rdbg") is None or not _bundler_ready(),
    reason="rdbg / bundler / Rails gems required",
)


def test_ruby_bridge_launches_bundled_rails_app() -> None:
    async def run() -> None:
        client = DAPClient(build_ruby_profile().adapter)
        initialized = asyncio.Event()
        stopped = asyncio.Event()
        exited = asyncio.Event()
        output: list[str] = []
        codes: list[int] = []
        client.on_event("initialized", lambda event: initialized.set())
        client.on_event("stopped", lambda event: stopped.set())
        client.on_event(
            "exited",
            lambda event: (codes.append(event.body.get("exitCode")), exited.set()),
        )
        client.on_event(
            "output",
            lambda event: output.append(event.body.get("output", "")),
        )
        await client.start()
        try:
            await client.initialize()
            launch = await client.launch(
                program=str(PROGRAM),
                cwd=str(FIXTURE),
                stop_on_entry=True,
                use_bundler=True,
                env={"BUNDLE_FROZEN": "true"},
            )
            await asyncio.wait_for(initialized.wait(), timeout=30)
            await client.configuration_done()
            assert (await asyncio.wait_for(launch, timeout=30)).success
            # Entry stop after `bundle exec` proves the Gemfile resolved and
            # the Rails app loaded far enough to reach the first line.
            await asyncio.wait_for(stopped.wait(), timeout=30)
            assert await client.threads()

            stopped.clear()
            await client.continue_(1)
            await asyncio.wait_for(exited.wait(), timeout=30)
            assert codes == [0]
            assert any("rails booted" in line for line in output)
        finally:
            await client.stop()

    asyncio.run(run())
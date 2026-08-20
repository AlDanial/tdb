"""End-to-end coverage for the bundled Ruby debug.gem DAP bridge."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field

import pytest

from tdb.dap.client import DAPClient
from tdb.dap.types import SourceBreakpoint
from tdb.languages.ruby import build_ruby_profile
from tdb.session.controller import DebugController


@pytest.mark.skipif(
    shutil.which("rdbg") is None, reason="Ruby debug gem is not installed"
)
def test_ruby_bridge_reports_exit_code(tmp_path) -> None:
    """rdbg sends no DAP `exited` event; the bridge synthesizes one from the
    debuggee process's return code. Verify a natural run reports code 0."""
    script = tmp_path / "hello.rb"
    script.write_text("puts 'hello'\n")

    async def run() -> None:
        client = DAPClient(build_ruby_profile().adapter)
        initialized = asyncio.Event()
        exited = asyncio.Event()
        codes: list[int] = []
        client.on_event("initialized", lambda event: initialized.set())
        client.on_event(
            "exited",
            lambda event: (codes.append(event.body.get("exitCode")), exited.set()),
        )
        await client.start()
        try:
            await client.initialize()
            launch = await client.launch(
                program=str(script.resolve()),
                cwd=str(tmp_path),
                stop_on_entry=False,
            )
            await asyncio.wait_for(initialized.wait(), timeout=20)
            await client.configuration_done()
            await asyncio.wait_for(launch, timeout=20)
            await asyncio.wait_for(exited.wait(), timeout=20)
            assert codes == [0]
        finally:
            await client.stop()

    asyncio.run(run())


@pytest.mark.skipif(
    shutil.which("rdbg") is None, reason="Ruby debug gem is not installed"
)
def test_ruby_bridge_reports_nonzero_exit_code(tmp_path) -> None:
    """A debuggee that exits non-zero must surface that code in the
    synthesized `exited` event, not a hard-coded 0."""
    script = tmp_path / "die.rb"
    script.write_text("#!/usr/bin/env ruby\nexit 3\n")

    async def run() -> None:
        client = DAPClient(build_ruby_profile().adapter)
        initialized = asyncio.Event()
        exited = asyncio.Event()
        codes: list[int] = []
        client.on_event("initialized", lambda event: initialized.set())
        client.on_event(
            "exited",
            lambda event: (codes.append(event.body.get("exitCode")), exited.set()),
        )
        await client.start()
        try:
            await client.initialize()
            launch = await client.launch(
                program=str(script.resolve()),
                cwd=str(tmp_path),
                stop_on_entry=False,
            )
            await asyncio.wait_for(initialized.wait(), timeout=20)
            await client.configuration_done()
            await asyncio.wait_for(launch, timeout=20)
            await asyncio.wait_for(exited.wait(), timeout=20)
            assert codes == [3]
        finally:
            await client.stop()

    asyncio.run(run())


@pytest.mark.skipif(
    shutil.which("rdbg") is None, reason="Ruby debug gem is not installed"
)
def test_ruby_bridge_streams_stdout_live_without_flush(tmp_path) -> None:
    """Program `puts` output must stream to the client live, not arrive
    in one burst at process exit.

    Ruby's STDOUT is fully buffered when attached to a pipe, so without
    the bridge's RUBYOPT stdout-sync helper a script that prints and
    then sleeps would deliver every line only at exit.  The helper makes
    each `puts` visible immediately, which is what the TUI console shows.
    """
    script = tmp_path / "stream.rb"
    script.write_text("puts 'first'\n$stdout.sync = false\nsleep 2\nputs 'second'\n")

    async def run() -> None:
        client = DAPClient(build_ruby_profile().adapter)
        initialized = asyncio.Event()
        exited = asyncio.Event()
        arrivals: list[tuple[float, str]] = []
        t0 = asyncio.get_event_loop().time()
        client.on_event("initialized", lambda event: initialized.set())
        client.on_event("exited", lambda event: exited.set())
        client.on_event(
            "output",
            lambda event: arrivals.append(
                (asyncio.get_event_loop().time() - t0, event.body.get("output", ""))
            ),
        )
        await client.start()
        try:
            await client.initialize()
            launch = await client.launch(
                program=str(script.resolve()),
                cwd=str(tmp_path),
                stop_on_entry=False,
            )
            await asyncio.wait_for(initialized.wait(), timeout=20)
            await client.configuration_done()
            await asyncio.wait_for(launch, timeout=20)
            await asyncio.wait_for(exited.wait(), timeout=20)
            stdout_lines = [
                (t, text)
                for t, text in arrivals
                if text.startswith(("first", "second"))
            ]
            assert [text for _, text in stdout_lines] == ["first\n", "second\n"]
            # The second line arrives only after the sleep, well after the
            # first — proving the first was not held until exit.
            first_t = next(t for t, text in stdout_lines if text == "first\n")
            second_t = next(t for t, text in stdout_lines if text == "second\n")
            assert second_t - first_t >= 1.5
        finally:
            await client.stop()

    asyncio.run(run())


@pytest.mark.skipif(
    shutil.which("rdbg") is None, reason="Ruby debug gem is not installed"
)
def test_ruby_bridge_launches_and_stops_on_entry(tmp_path) -> None:
    script = tmp_path / "hello.rb"
    script.write_text(
        "#!/usr/bin/env ruby\n"
        '# Simple "Hello, World!" example for tdb Ruby debugging.\n'
        "\n"
        "def greet(name)\n"
        '  puts "Hello, #{name}!"\n'
        "end\n"
        "\n"
        "if __FILE__ == $0\n"
        '  greet("World")\n'
        '  greet("Ruby")\n'
        "end\n"
    )

    async def run() -> None:
        client = DAPClient(build_ruby_profile().adapter)
        initialized = asyncio.Event()
        stopped = asyncio.Event()
        client.on_event("initialized", lambda event: initialized.set())
        client.on_event("stopped", lambda event: stopped.set())
        await client.start()
        try:
            await client.initialize()
            launch = await client.launch(
                program=str(script.resolve()),
                cwd=str(tmp_path),
                stop_on_entry=True,
            )
            # A cold Ruby process may spend several seconds loading gems.
            await asyncio.wait_for(initialized.wait(), timeout=20)
            await client.configuration_done()
            assert (await asyncio.wait_for(launch, timeout=20)).success
            await asyncio.wait_for(stopped.wait(), timeout=20)
            assert await client.threads()

            stopped.clear()
            breakpoints = await client.set_breakpoints(
                str(script.resolve()),
                [SourceBreakpoint(line=5)],
            )
            assert breakpoints[0].verified is True
            await client.continue_(1)
            await asyncio.wait_for(stopped.wait(), timeout=20)
            assert len(breakpoints) == 1
        finally:
            await client.stop()

    asyncio.run(run())


@dataclass
class _TestEventHandler:
    """Minimal DebugEventHandler implementation for controller-driven tests."""

    initialized: asyncio.Event = field(default_factory=asyncio.Event)
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    continued: asyncio.Event = field(default_factory=asyncio.Event)
    terminated: asyncio.Event = field(default_factory=asyncio.Event)
    exited: asyncio.Event = field(default_factory=asyncio.Event)
    exit_code: int | None = None

    def on_initialized(self) -> None:
        self.initialized.set()

    def on_stopped(
        self,
        thread_id: int | None,
        reason: str,
        description: str | None = None,
        text: str | None = None,
    ) -> None:
        self.stopped.set()

    def on_continued(self) -> None:
        self.continued.set()

    def on_terminated(self) -> None:
        self.terminated.set()

    def on_exited(self, exit_code: int) -> None:
        self.exit_code = exit_code
        self.exited.set()

    def on_output(self, text: str, category: str) -> None:
        pass

    def on_external_terminal_started(self) -> None:
        pass


@pytest.mark.skipif(
    shutil.which("rdbg") is None,
    reason="Ruby debug gem is not installed",
)
def test_ruby_evaluate_exception_does_not_deadlock(tmp_path) -> None:
    """A raising evaluate expression must not deadlock the Ruby session.

    rdbg fires its catch-all exception breakpoint when an evaluated
    expression raises inside the debuggee thread and suspends it
    mid-evaluation, so the evaluate response never arrives. The
    controller's `suppress_exception_breakpoints_during_evaluate` quirk
    (RdbgAdapter) clears the catch breakpoint around the request and
    restores it afterwards. Verify the response returns, reports the
    error, and the session stays usable afterwards.
    """
    script = tmp_path / "evaluate_error.rb"
    script.write_text("value = 42\nsleep 0.5\n")

    async def run() -> None:
        handler = _TestEventHandler()
        controller = DebugController(handler, profile=build_ruby_profile())

        await controller.start(
            program=str(script.resolve()),
            cwd=str(tmp_path),
            stop_on_entry=True,
        )

        try:
            await asyncio.wait_for(handler.initialized.wait(), timeout=20)

            # Arms the `any` exception breakpoint and releases the launch.
            await asyncio.wait_for(controller.do_configure(), timeout=20)

            await asyncio.wait_for(handler.stopped.wait(), timeout=20)
            assert controller.state.current_thread_id is not None

            # Load the stack so evaluate has a real frameId to target
            # (mirrors the TUI's post-stop flow in dap_events.py).
            await controller.fetch_stop_info()
            assert controller.state.current_frame_id is not None

            result = await asyncio.wait_for(
                controller.evaluate("Object.const_get('DefinitelyMissingConstant')"),
                timeout=10,
            )
            assert "NameError" in result

            # The important part: the session must still be usable after the
            # raising evaluate. Without the Ruby quirk this would deadlock.
            await controller.continue_()
            await asyncio.wait_for(handler.terminated.wait(), timeout=20)
        finally:
            await controller.stop()

    asyncio.run(run())

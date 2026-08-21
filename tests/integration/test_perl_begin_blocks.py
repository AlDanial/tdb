"""BEGIN blocks are steppable: tdb stops during perl's compile phase."""

import asyncio
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, "tests/integration")
from perl_adapter_harness import AdapterClient

from tdb.adapters.perl.protocol import StreamParser
from tdb.adapters.perl.session import PerlProtocolError, PerlSession, helpers_path

pytestmark = pytest.mark.skipif(
    shutil.which("perl") is None
    or subprocess.run(["perl", "-e", "require v5.18"]).returncode != 0,
    reason="perl >= 5.18 required",
)

WITH_BEGIN = """use strict;
use warnings;

BEGIN {
    my $x = 10;
    my $y = $x * 2;
    print STDERR "begin $x $y\\n";
}

my $b = 20;
print STDERR "main $b\\n";

END {
    my $z = 99;
    print STDERR "end $z\\n";
}
"""

NO_PRAGMAS = 'my $a = 1;\nmy $b = 2;\nprint STDERR "x\\n";\n'


async def _launch(tmp_path, source, name="p.pl"):
    prog = tmp_path / name
    prog.write_text(source)
    c = AdapterClient()
    await c.start()
    await c.request("initialize", {})
    c.send("launch", {"program": str(prog), "cwd": str(tmp_path)})
    await c.wait_event("initialized")
    await c.request("configurationDone", {})
    await c.wait_event("stopped")
    return c, str(prog)


async def _where(c):
    st = await c.request("stackTrace", {"threadId": 1})
    f = st["body"]["stackFrames"][0]
    return f["source"]["path"], f["line"], f["name"]


async def test_first_stop_is_compile_phase_statement(tmp_path):
    c, prog = await _launch(tmp_path, WITH_BEGIN)
    try:
        path, line, _ = await _where(c)
        assert path == prog
        assert line == 1  # `use strict;` -- a compile-time statement
    finally:
        await c.stop()


async def test_step_reaches_inside_the_begin_block(tmp_path):
    c, prog = await _launch(tmp_path, WITH_BEGIN)
    try:
        seen = []
        for _ in range(12):
            path, line, name = await _where(c)
            seen.append((line, name))
            if line == 5:  # `my $x = 10;` inside BEGIN
                break
            await c.request("stepIn", {"threadId": 1})
            await c.wait_event("stopped", timeout=20)
        assert any(line == 5 for line, _ in seen), f"never entered BEGIN: {seen}"
        assert all(
            "TdbCompile" not in str(p) for p in [prog]
        )  # sanity; real check below
    finally:
        await c.stop()


async def test_no_stop_is_reported_inside_tdbcompile_pm(tmp_path):
    c, prog = await _launch(tmp_path, WITH_BEGIN)
    try:
        for _ in range(20):
            path, _, _ = await _where(c)
            assert "TdbCompile.pm" not in path, "leaked a stop inside tdb's own shim"
            await c.request("stepIn", {"threadId": 1})
            try:
                await c.wait_event("stopped", timeout=15)
            except AssertionError:
                break
    finally:
        await c.stop()


async def test_program_without_compile_time_statements_unchanged(tmp_path):
    c, prog = await _launch(tmp_path, NO_PRAGMAS)
    try:
        path, line, _ = await _where(c)
        assert (path, line) == (prog, 1)
    finally:
        await c.stop()


async def test_no_stop_on_entry_runs_compile_phase_without_stops(tmp_path):
    """stopOnEntry:false (`--run`, headless): a program with compile-time
    statements must run to completion with NO stopped event. A bare `c`
    from the compile-phase entry prompt only clears single-step for the
    current BEGIN/require frame — perl5db re-arms it when that frame
    returns, so the debuggee traps again on the next `use` line and the
    spurious "step" stop makes run mode open the TUI."""
    prog = tmp_path / "p.pl"
    prog.write_text(WITH_BEGIN)
    c = AdapterClient()
    await c.start()
    try:
        await c.request("initialize", {})
        fut = c.send(
            "launch",
            {"program": str(prog), "cwd": str(tmp_path), "stopOnEntry": False},
        )
        await c.wait_event("initialized")
        await c.request("configurationDone", {})
        await asyncio.wait_for(fut, 30)
        await c.wait_event("exited", timeout=30)
        stops = [e for e in c.events if e["event"] == "stopped"]
        assert stops == [], f"spurious stops with stopOnEntry:false: {stops}"
    finally:
        await c.stop()


async def test_continue_from_entry_without_breakpoints_runs_to_exit(tmp_path):
    """Same root cause, TUI path: `c` at the entry stop with no
    breakpoints set must run to completion, not re-stop on the next
    compile-time statement."""
    c, prog = await _launch(tmp_path, WITH_BEGIN)
    try:
        await c.request("continue", {"threadId": 1})
        await c.wait_event("exited", timeout=30)
        stops = [e for e in c.events if e["event"] == "stopped"]
        assert stops == [], f"spurious stops after continue-from-entry: {stops}"
    finally:
        await c.stop()


async def test_breakpoints_set_during_compile_phase_still_fire(tmp_path):
    """Regression for the partial-line-table hazard: a breakpoint requested
    at the first (compile-phase) stop must fire once the program runs."""
    c, prog = await _launch(tmp_path, WITH_BEGIN)
    try:
        await c.request(
            "setBreakpoints",
            {"source": {"path": prog}, "breakpoints": [{"line": 11}]},
        )
        await c.request("continue", {"threadId": 1})
        await c.wait_event("stopped", timeout=30)
        path, line, _ = await _where(c)
        assert (path, line) == (prog, 11)
    finally:
        await c.stop()


async def test_end_block_still_steppable(tmp_path):
    """Background 5: END blocks already worked; must not regress."""
    c, prog = await _launch(tmp_path, WITH_BEGIN)
    try:
        await c.request(
            "setBreakpoints",
            {"source": {"path": prog}, "breakpoints": [{"line": 11}]},
        )
        await c.request("continue", {"threadId": 1})
        await c.wait_event("stopped", timeout=30)
        seen = []
        for _ in range(8):
            await c.request("stepIn", {"threadId": 1})
            try:
                await c.wait_event("stopped", timeout=15)
            except AssertionError:
                break
            _, line, name = await _where(c)
            seen.append((line, name))
        assert any("END" in str(name) for _, name in seen), f"END not reached: {seen}"
    finally:
        await c.stop()


async def test_breakpoint_inside_begin_block_fires(tmp_path):
    """The bug this file exists to fix: a breakpoint on a statement
    inside a BEGIN block must actually stop there (not be silently
    skipped, and not merely apply once RUN phase arrives -- by then the
    BEGIN block has already finished executing)."""
    c, prog = await _launch(tmp_path, WITH_BEGIN)
    try:
        resp = await c.request(
            "setBreakpoints",
            {"source": {"path": prog}, "breakpoints": [{"line": 5}]},
        )
        (bp,) = resp["body"]["breakpoints"]
        assert bp["verified"] is False  # deferred: still compiling
        await c.request("continue", {"threadId": 1})
        stopped = await c.wait_event("stopped", timeout=30)
        assert stopped["body"]["reason"] == "breakpoint"
        path, line, name = await _where(c)
        assert (path, line) == (prog, 5)
        assert name == "main::BEGIN"
    finally:
        await c.stop()


async def test_conditional_breakpoint_inside_begin_fires_when_true(tmp_path):
    c, prog = await _launch(tmp_path, WITH_BEGIN)
    try:
        await c.request(
            "setBreakpoints",
            {
                "source": {"path": prog},
                "breakpoints": [{"line": 6, "condition": "$x == 10"}],
            },
        )
        await c.request("continue", {"threadId": 1})
        stopped = await c.wait_event("stopped", timeout=30)
        assert stopped["body"]["reason"] == "breakpoint"
        path, line, name = await _where(c)
        assert (path, line) == (prog, 6)
        assert name == "main::BEGIN"
    finally:
        await c.stop()


async def test_conditional_breakpoint_inside_begin_skipped_when_false(tmp_path):
    c, prog = await _launch(tmp_path, WITH_BEGIN)
    try:
        await c.request(
            "setBreakpoints",
            {
                "source": {"path": prog},
                "breakpoints": [{"line": 6, "condition": "$x == 99"}],
            },
        )
        await c.request("continue", {"threadId": 1})
        # Condition never true: no compile-phase stop, no runtime
        # breakpoint left behind either -- runs to completion.
        await c.wait_event("terminated", timeout=30)
    finally:
        await c.stop()


async def test_begin_breakpoint_does_not_refire_on_next_continue(tmp_path):
    """Regression: after the compile-phase breakpoint fires, a further
    `continue` must proceed past it (never re-fire on the same line)."""
    c, prog = await _launch(tmp_path, WITH_BEGIN)
    try:
        await c.request(
            "setBreakpoints",
            {"source": {"path": prog}, "breakpoints": [{"line": 5}, {"line": 10}]},
        )
        await c.request("continue", {"threadId": 1})
        stopped = await c.wait_event("stopped", timeout=30)
        assert stopped["body"]["reason"] == "breakpoint"
        path, line, _ = await _where(c)
        assert (path, line) == (prog, 5)

        await c.request("continue", {"threadId": 1})
        stopped2 = await c.wait_event("stopped", timeout=30)
        assert stopped2["body"]["reason"] == "breakpoint"
        path2, line2, _ = await _where(c)
        assert (path2, line2) == (prog, 10), "re-fired on the same BEGIN line"
    finally:
        await c.stop()


MANY_USES = """use strict;
use warnings;
use File::Basename;
use File::Spec;

BEGIN {
    my $x = 10;
    print STDERR "begin $x\\n";
}

my $b = 20;
print STDERR "main $b\\n";
"""


async def test_one_next_advances_one_displayed_line_during_compile_phase(tmp_path):
    """Regression: perl5db traps several compile-time internal operations
    per source statement (a `use` expands into load + import), all
    reporting the same line. Each such trap used to surface as its own
    DAP stop, so advancing past one `use` line took several `next`
    presses. One user `next` must land on a DIFFERENT displayed line."""
    c, prog = await _launch(tmp_path, MANY_USES)
    try:
        prev = await _where(c)
        visited = [prev[1]]
        for _ in range(10):
            await c.request("next", {"threadId": 1})
            try:
                await c.wait_event("stopped", timeout=20)
            except AssertionError:
                break  # ran to completion -- fine, no repeats seen
            cur = await _where(c)
            assert (cur[0], cur[1]) != (prev[0], prev[1]), (
                f"one `next` did not advance the displayed line: stuck at "
                f"{cur[0]}:{cur[1]}; lines visited so far: {visited}"
            )
            visited.append(cur[1])
            prev = cur
            # Step PAST the first runtime line (11, `my $b = 20;`): the
            # START->RUN transition used to leak one extra same-line stop
            # there (the shim's self-uninstall trap, reported under the
            # caller's file:line), costing a duplicate `next` press.
            if cur[1] >= 12:
                break
        assert 12 in visited, f"never crossed into the runtime lines: {visited}"
    finally:
        await c.stop()


RUNTIME_LINE_BEFORE_LATER_BEGIN = """use strict;
use warnings;

my $b = 20;
print STDERR "before begin $b\\n";

BEGIN {
    my $x = 1;
}

print STDERR "after begin\\n";
"""


async def test_runtime_breakpoint_not_falsely_fired_by_later_begin_block(tmp_path):
    """Regression: a breakpoint on a RUNTIME line must fire at that exact
    runtime line, not during the compile phase merely because a
    compile-time statement (the BEGIN block) appears later in the file.
    Snap-forward matching would get this wrong; exact line equality
    must not."""
    c, prog = await _launch(tmp_path, RUNTIME_LINE_BEFORE_LATER_BEGIN, name="q.pl")
    try:
        await c.request(
            "setBreakpoints",
            {"source": {"path": prog}, "breakpoints": [{"line": 4}]},
        )
        await c.request("continue", {"threadId": 1})
        stopped = await c.wait_event("stopped", timeout=30)
        assert stopped["body"]["reason"] == "breakpoint"
        path, line, name = await _where(c)
        assert (path, line) == (prog, 4)
        assert name != "main::BEGIN"
    finally:
        await c.stop()


async def test_compile_phase_location_never_reports_unknown(tmp_path):
    """Pins the task-4 kill hazard: _classify_and_emit_stop treats a
    location() of file "?" as proof the debuggee ran to completion, and
    (in launch mode) force-quits it with `q`. A compile-phase stop that
    ever reported "?" would trip that branch and kill a debuggee that's
    still very much alive. Drives Devel::TdbHelper::location() directly
    (the same helper _classify_and_emit_stop calls), independent of the
    DAP layer, through every stop from the first compile-time statement
    into the BEGIN block.
    """
    prog = tmp_path / "p.pl"
    prog.write_text(WITH_BEGIN)
    outputs: list[tuple[str, str]] = []
    session = PerlSession(
        on_output=lambda text, cat: outputs.append((text, cat)),
        on_stop=lambda: None,
    )
    await asyncio.wait_for(
        session.launch(program=str(prog), args=[], cwd=str(tmp_path), env=None),
        20.0,
    )
    try:
        seen_files = []
        for _ in range(9):
            loc = await session.helper("Devel::TdbHelper::location()")
            f = loc.get("file")
            seen_files.append(f)
            assert f not in (None, "", "?"), f"location() reported {f!r}: {seen_files}"
            assert f == str(prog)
            try:
                await session.command("s")
            except PerlProtocolError:
                break
    finally:
        await session.stop()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="resource.RLIMIT_AS / preexec_fn are POSIX-only (no Windows equivalent)",
)
async def test_compile_phase_shim_does_not_exhaust_memory(tmp_path):
    """Regression guard for the OOM bug fixed in TdbCompile.pm.

    An earlier version of the shim kept its DB::DB wrapper installed
    for the debuggee's whole life and made the RUN-phase branch a bare
    `goto &$orig` pass-through. Verified empirically, that combination
    reliably makes perl die with "Out of memory in
    perl:util:safesysmalloc" once RUN-phase execution proceeds -- it
    crashed the development machine's session twice. The fix
    self-uninstalls the wrapper (`*DB::DB = $orig`) the moment
    ${^GLOBAL_PHASE} leaves 'START', before the tail-call `goto`.

    This drives the real shim through the compile phase and a genuine
    `continue` to completion under an explicit, tight RLIMIT_AS
    (independent of whatever ulimit the test runner itself happens to
    be wrapped in) and asserts perl exits cleanly -- so a reintroduction
    of the bug fails fast here instead of silently exhausting memory
    again.
    """
    import resource

    prog = tmp_path / "loopy.pl"
    prog.write_text(
        "use strict;\n"
        "use warnings;\n"
        "BEGIN { my $x = 1; }\n"
        + "".join(f"my $v{i} = {i};\n" for i in range(200))
        + 'print STDERR "done\\n";\n'
    )

    mem_limit = 300 * 1024 * 1024  # comfortably above a healthy perl
    # process (a few MB); comfortably below anything that could risk
    # the host if the leak regresses.

    def _limit_memory() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (mem_limit, mem_limit))

    connected: asyncio.Future = asyncio.get_running_loop().create_future()

    async def _on_connect(reader, writer):
        if not connected.done():
            connected.set_result((reader, writer))

    server = await asyncio.start_server(_on_connect, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    env = dict(os.environ)
    env["PERLDB_OPTS"] = f"RemotePort=127.0.0.1:{port}"
    env["TDB_COMPILE_FILE"] = str(prog)
    adapters_dir = os.path.dirname(helpers_path())
    proc = await asyncio.create_subprocess_exec(
        "perl",
        "-d",
        f"-I{adapters_dir}",
        "-MDevel::TdbCompile",
        str(prog),
        cwd=str(tmp_path),
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=_limit_memory,
    )
    stderr_chunks: list[bytes] = []

    async def _pump_stderr() -> None:
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                return
            stderr_chunks.append(chunk)

    async def _drain_stdout() -> None:
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                return

    stderr_task = asyncio.ensure_future(_pump_stderr())
    stdout_task = asyncio.ensure_future(_drain_stdout())
    try:
        reader, writer = await asyncio.wait_for(connected, 10.0)
        server.close()

        async def _next_prompt_text() -> str:
            parser = StreamParser()
            parts: list[str] = []
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    return "".join(parts)
                for ev in parser.feed(chunk):
                    if ev[0] == "text":
                        parts.append(ev[1])
                    elif ev[0] == "prompt":
                        return "".join(parts)

        await asyncio.wait_for(_next_prompt_text(), 10.0)  # first compile-phase stop
        # `continue` runs the program to completion. The shim's own
        # self-uninstall leaks exactly one extra stop, inside
        # TdbCompile.pm itself, at the START->RUN transition (the same
        # leak server.py's _location_after_settling steps past for real
        # sessions) -- drive through it the same way, then perl5db's
        # inhibit_exit (on by default) parks the still-alive child at a
        # fresh prompt instead of exiting, so a final `q` is needed to
        # make it really exit.
        text = ""
        for _ in range(5):
            writer.write(b"c\n")
            await writer.drain()
            text = await asyncio.wait_for(_next_prompt_text(), 15.0)
            if "TdbCompile.pm" not in text:
                break
        assert "TdbCompile.pm" not in text, f"stop leaked into the shim: {text!r}"
        writer.write(b"q\n")
        await writer.drain()
        try:
            await asyncio.wait_for(proc.wait(), 15.0)
        except asyncio.TimeoutError:
            pass
        await asyncio.wait_for(asyncio.gather(stderr_task, stdout_task), 5.0)
        stderr = b"".join(stderr_chunks)
        assert proc.returncode == 0, (
            f"perl died (rc={proc.returncode}); "
            f"stderr={stderr.decode(errors='replace')!r}"
        )
        assert b"Out of memory" not in stderr
        assert b"done" in stderr
    finally:
        stderr_task.cancel()
        stdout_task.cancel()
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        server.close()

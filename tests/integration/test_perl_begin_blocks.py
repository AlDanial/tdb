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

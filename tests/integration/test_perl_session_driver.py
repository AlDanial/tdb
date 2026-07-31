"""PerlSession against real perl -d — no DAP involved."""

import asyncio
import re
import shutil
import subprocess

import pytest

from tdb.adapters.perl.session import PerlSession, helpers_path

pytestmark = pytest.mark.skipif(
    shutil.which("perl") is None
    or subprocess.run(["perl", "-e", "require v5.18"]).returncode != 0,
    reason="perl >= 5.18 required",
)

WAIT = 20.0

SCRIPT = """\
my $x = 1;
my $y = 2;
sub add { my ($a, $b) = @_; return $a + $b }
my $z = add($x, $y);
print "z=$z\\n";
"""


@pytest.fixture
def script(tmp_path):
    p = tmp_path / "toy.pl"
    p.write_text(SCRIPT)
    return str(p)


@pytest.fixture
async def session(script):
    outputs: list[tuple[str, str]] = []
    stops: list[bool] = []
    s = PerlSession(
        on_output=lambda text, cat: outputs.append((text, cat)),
        on_stop=lambda: stops.append(True),
    )
    await asyncio.wait_for(
        s.launch(program=script, args=[], cwd=str(tmp_path_of(script)), env=None),
        WAIT,
    )
    yield s, outputs, stops
    await s.stop()


def tmp_path_of(script):
    import pathlib

    return pathlib.Path(script).parent


async def test_launch_stops_at_entry_with_helpers_injected(session, script):
    s, _, _ = session
    assert s.stopped
    loc = await s.helper("Devel::TdbHelper::location()")
    assert loc["version"] == 1
    assert loc["file"] == script
    assert loc["line"] == 1


async def test_breakpoint_continue_and_unsolicited_stop(session, script):
    s, _, stops = session
    events = await s.command(f"b 4")
    assert not any(e[0] == "json" for e in events)
    s.resume("c")
    for _ in range(200):
        if stops:
            break
        await asyncio.sleep(0.1)
    assert stops, "breakpoint stop never surfaced"
    loc = await s.helper("Devel::TdbHelper::location()")
    assert loc["line"] == 4


async def test_program_output_reaches_callback(session):
    s, outputs, stops = session
    s.resume("c")  # no breakpoints -> runs to completion
    for _ in range(200):
        if any("z=3" in t for t, _ in outputs):
            break
        await asyncio.sleep(0.1)
    assert any("z=3" in t for t, c in outputs if c == "stdout")


async def test_perl5db_chatter_not_forwarded_as_output(session):
    """perl5db echoes the current source line at every stop
    (`main::(file:line): code`). That is debugger chatter, not program
    output -- it must never surface through on_output."""
    s, outputs, stops = session
    await s.command("b 4")
    s.resume("c")
    for _ in range(200):
        if stops:
            break
        await asyncio.sleep(0.1)
    assert stops, "breakpoint stop never surfaced"
    console = [t for t, c in outputs if c == "console"]
    assert console == [], f"perl5db chatter leaked as output: {console!r}"


async def test_socket_has_nagle_disabled_after_helper_injection(session):
    """Without TCP_NODELAY on the RemotePort socket, every perl5db round
    trip stalls ~40ms in Nagle + delayed-ACK. helpers.pl must disable
    Nagle at load time (covers launch and TdbRemote attach alike)."""
    s, _, _ = session
    events = await s.command(
        ';{ use Socket; print {$DB::OUT} "nodelay=",'
        ' unpack("i", getsockopt($DB::OUT, Socket::IPPROTO_TCP(),'
        ' Socket::TCP_NODELAY())), "\\n" }'
    )
    text = "".join(e[1] for e in events if e[0] == "text")
    m = re.search(r"nodelay=(\d+)", text)
    assert m, f"probe produced no nodelay value: {text!r}"
    assert int(m.group(1)) != 0, "Nagle still enabled on perl5db socket"


async def test_debuggee_disconnect_raises_not_succeeds(tmp_path):
    # A compile error in the debuggee (e.g. `my $x = ;`) was expected to
    # sever the perl5db connection, but real perl5db (5.40.1) does not
    # disconnect on a compile error -- confirmed by probing: it drops
    # into an interactive post-mortem "DB<1>" prompt and stays connected
    # (launch() completes normally with stopped=True, helpers.pl still
    # loads fine since the perl interpreter itself is alive). So a
    # compile error cannot exercise the EOF path against real perl5db.
    #
    # Instead, use perl5db's own `q` command to force a genuine
    # connection close -- this reliably reproduces the "child gone
    # mid-command" condition Finding 1 is about, without relying on a
    # scenario real perl5db doesn't actually produce.
    from tdb.adapters.perl.session import PerlProtocolError

    bad = tmp_path / "toy.pl"
    bad.write_text(SCRIPT)
    s = PerlSession(on_output=lambda text, cat: None, on_stop=lambda: None)
    await asyncio.wait_for(
        s.launch(program=str(bad), args=[], cwd=str(tmp_path), env=None), WAIT
    )
    try:
        with pytest.raises(PerlProtocolError) as exc:
            await asyncio.wait_for(s.command("q"), WAIT)
        assert isinstance(exc.value.tail, str)
        assert s.stopped is False
    finally:
        await s.stop()


async def test_stop_is_clean_after_launch(session):
    s, _, _ = session
    await s.stop()
    tasks = [t for t in [s._reader_task, *s._pump_tasks] if t is not None]
    assert tasks, "expected reader/pump tasks to exist"
    for t in tasks:
        assert t.done()
    # second stop must not raise
    await s.stop()


async def test_helper_timeout_raises_with_tail(session):
    s, _, _ = session
    from tdb.adapters.perl.session import PerlProtocolError

    with pytest.raises(PerlProtocolError) as exc:
        # <STDIN> was expected to block since stdin is DEVNULL, but
        # DEVNULL makes <STDIN> return EOF immediately rather than
        # block -- confirmed against real perl5db (5.40.1), per the
        # brief's implementer note (b). `sleep 5` reliably starves the
        # prompt within the 2s timeout instead.
        await s.command("sleep 5", timeout=2.0)
    assert isinstance(exc.value.tail, str)


# --- Risk-2 fact-finding probes -----------------------------------------
# Flagged concern: under a stub debugger used in earlier tasks, `do`/
# `require`-loaded secondary files showed all-zero breakability marks.
# These exercise Devel::TdbHelper::breakable() against a REAL perl5db
# session on a `require`d second file. Real perl5db (5.40.1) populates
# the line table correctly once the file is loaded -- a positive result.
#
# NOTE: the two assertions ("empty before load" / "populated + breakable
# after load") are split across two independent sessions rather than one.
# `breakable(file)` autovivifies the perl5db source-line array for that
# filename as a side effect of merely reading it (`\@{"main::_<$file"}`
# in helpers.pl). Calling it on a file *before* that file is `require`d
# was observed to poison perl5db's own breakpoint machinery for that file
# permanently, even after the file loads and breakable() starts reporting
# real line numbers: `b <file>:<line>` is silently accepted but the
# program runs straight through it instead of stopping. This is a
# separate, real finding beyond the originally-flagged risk -- see the
# task report for a minimal repro. Splitting into two sessions here
# avoids that interaction so this test verifies real perl5db's own line
# table, not the interaction hazard (which product code must avoid by
# never calling breakable() on a file speculatively before it's loaded).

SECOND_FILE = """\
package Second;
sub greet {
    my $x = 1;
    my $y = 2;
    print "hello from second\\n";
    return $x + $y;
}
1;
"""


@pytest.fixture
def two_file_program(tmp_path):
    second = tmp_path / "second.pl"
    second.write_text(SECOND_FILE)
    main = tmp_path / "main.pl"
    main.write_text(
        "use strict; use warnings;\n"
        f"require {str(second)!r};\n"
        "Second::greet();\n"
        'print "back in main\\n";\n'
    )
    return str(main), str(second)


async def test_breakable_empty_before_require(two_file_program):
    main, second = two_file_program
    s = PerlSession(on_output=lambda text, cat: None, on_stop=lambda: None)
    await asyncio.wait_for(
        s.launch(program=main, args=[], cwd=str(tmp_path_of(main)), env=None), WAIT
    )
    try:
        # Before the require executes, second.pl isn't compiled yet.
        pre = await s.helper(f"Devel::TdbHelper::breakable({second!r})")
        assert pre["lines"] == []
    finally:
        await s.stop()


@pytest.fixture
def big_source_program(tmp_path):
    # Regression for the split-marker bug: source() returns the whole
    # compiled file as one TDB>>>...<<<TDB JSON payload. A file whose
    # source contains '<' (real Perl filehandle-read syntax) and whose
    # compiled text exceeds one 4096-byte socket read must still
    # round-trip through the marker parser intact.
    lines = [
        "use strict;\n",
        "use warnings;\n",
        "\n",
        "sub unused_reader {\n",
        "    my ($fh) = @_;\n",
        "    while (my $line = <$fh>) {\n",
        "        print $line;\n",
        "    }\n",
        "    return;\n",
        "}\n",
        "\n",
    ]
    for i in range(80):
        lines.append(
            f"# pad {i:04d} while (my $line = <$fh>) {{ }} <STDIN> filler filler\n"
        )
    lines.append('print "main done\\n";\n')
    text = "".join(lines)
    assert len(text.encode("utf-8")) > 4096
    p = tmp_path / "big.pl"
    p.write_text(text)
    return str(p), text


async def test_source_of_large_file_with_angle_brackets_round_trips(
    big_source_program,
):
    program, expected_text = big_source_program
    s = PerlSession(on_output=lambda text, cat: None, on_stop=lambda: None)
    await asyncio.wait_for(
        s.launch(program=program, args=[], cwd=str(tmp_path_of(program)), env=None),
        WAIT,
    )
    try:
        payload = await s.helper(f"Devel::TdbHelper::source({program!r})")
        assert payload["text"] == expected_text
    finally:
        await s.stop()


async def test_breakable_populates_and_breakpoint_lands_after_require(
    two_file_program,
):
    main, second = two_file_program
    stops: list[bool] = []
    s = PerlSession(
        on_output=lambda text, cat: None, on_stop=lambda: stops.append(True)
    )
    await asyncio.wait_for(
        s.launch(program=main, args=[], cwd=str(tmp_path_of(main)), env=None), WAIT
    )
    try:
        # Stop right after the require (main.pl line 3 is the call site).
        await s.command("b 3")
        s.resume("c")
        for _ in range(200):
            if stops:
                break
            await asyncio.sleep(0.1)
        assert stops, "expected stop at main.pl:3"

        # Now second.pl is loaded: breakable() should report real lines,
        # not an all-zero/empty table.
        post = await s.helper(f"Devel::TdbHelper::breakable({second!r})")
        assert post["lines"] == [3, 4, 5, 6]

        # And a breakpoint set by file:line inside it actually lands.
        stops.clear()
        await s.command(f"b {second}:5")
        s.resume("c")
        for _ in range(200):
            if stops:
                break
            await asyncio.sleep(0.1)
        assert stops, "expected stop inside second.pl"
        loc = await s.helper("Devel::TdbHelper::location()")
        assert loc["file"] == second
        assert loc["line"] == 5
        assert loc["sub"] == "Second::greet"
    finally:
        await s.stop()

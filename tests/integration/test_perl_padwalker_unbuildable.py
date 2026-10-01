"""Perl debugging proceeds when the bundled PadWalker cannot be built.

A wrapper perl fails every PadWalker probe and dies on `Makefile.PL`
with the error a perl lacking ExtUtils::MakeMaker prints (minimal
distro perls ship without it). The adapter must still launch, stop at
a breakpoint, answer stack/scopes/variables, and run to completion;
only the PadWalker console notice records the degradation."""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import subprocess
import sys

import pytest

from .perl_adapter_harness import AdapterClient

pytestmark = pytest.mark.skipif(
    sys.platform == "win32"
    or shutil.which("perl") is None
    or subprocess.run(["perl", "-e", "require v5.18"]).returncode != 0,
    reason="perl >= 5.18 and a POSIX shell required",
)

SCRIPT = """\
sub work {
    my ($label) = @_;
    my %info = ( label => $label, nums => [ 10, 20 ] );
    my $marker = 1;   # line 4: breakpoint here
    return \\%info;
}
my $r = work("go");
print "done\\n";
"""

MAKEMAKER_ERROR = (
    "Can't locate ExtUtils/MakeMaker.pm in @INC (you may need to install "
    "the ExtUtils::MakeMaker module)"
)

# PadWalker probes (`require PadWalker; PadWalker::peek_my(0) ...`) fail
# so neither a native nor a cached copy short-circuits the build; the
# build's first step dies the way it does without ExtUtils::MakeMaker;
# everything else (the debuggee itself) goes to the real perl.
WRAPPER = f"""#!/bin/sh
for a in "$@"; do
  case "$a" in
    *PadWalker::peek_my*) echo "Can't locate PadWalker.pm in @INC" >&2; exit 2 ;;
    Makefile.PL) echo "{MAKEMAKER_ERROR}" >&2
                 echo "BEGIN failed--compilation aborted at Makefile.PL line 1." >&2
                 exit 2 ;;
  esac
done
exec {shutil.which("perl")} "$@"
"""


@pytest.fixture
def unbuildable_perl(tmp_path, monkeypatch):
    monkeypatch.setenv("TDB_PADWALKER_CACHE", str(tmp_path / "cache"))
    wrapper = tmp_path / "perl"
    wrapper.write_text(WRAPPER)
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    return str(wrapper)


async def test_launch_and_inspect_without_padwalker(tmp_path, unbuildable_perl):
    p = tmp_path / "insp.pl"
    p.write_text(SCRIPT)
    c = AdapterClient()
    await c.start()
    try:
        await c.request("initialize", {"adapterID": "perl-tdb"})
        launch_fut = c.send(
            "launch",
            {
                "program": str(p),
                "args": [],
                "cwd": str(tmp_path),
                "stopOnEntry": False,
                "perl": unbuildable_perl,
            },
        )
        await c.wait_event("initialized")
        await c.request(
            "setBreakpoints",
            {"source": {"path": str(p)}, "breakpoints": [{"line": 4}]},
        )
        await c.request("configurationDone")
        launch = await asyncio.wait_for(launch_fut, 60)
        assert launch["success"] is True, launch
        await c.wait_event("stopped")

        st = await c.request("stackTrace", {"threadId": 1})
        frames = st["body"]["stackFrames"]
        assert frames[0]["line"] == 4
        sc = await c.request("scopes", {"frameId": frames[0]["id"]})
        by_name = {s["name"]: s for s in sc["body"]["scopes"]}
        assert "Lexicals" in by_name
        lex = await c.request(
            "variables",
            {"variablesReference": by_name["Lexicals"]["variablesReference"]},
        )
        assert lex["success"] is True
        names = {v["name"] for v in lex["body"]["variables"]}
        # Either the core-B fallback shows the frame's own lexicals or it
        # explains itself; it must answer, not fail.
        assert names, lex

        await c.request("continue")
        await c.wait_event("terminated")
    finally:
        await c.stop()

    notices = [
        e["body"]["output"]
        for e in c.events
        if e["event"] == "output" and "PadWalker unavailable" in e["body"]["output"]
    ]
    assert notices, [e for e in c.events if e["event"] == "output"]
    assert "ExtUtils::MakeMaker" in notices[0]
    assert not (tmp_path / "cache").exists() or not any(
        (tmp_path / "cache").rglob("PadWalker.pm")
    ), "no build artefact must be cached when Makefile.PL fails"
    assert os.environ.get("TDB_PADWALKER_CACHE") == str(tmp_path / "cache")

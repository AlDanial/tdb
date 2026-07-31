import asyncio
import shutil
import subprocess

import pytest

from .perl_adapter_harness import AdapterClient

pytestmark = pytest.mark.skipif(
    shutil.which("perl") is None
    or subprocess.run(["perl", "-e", "require v5.18"]).returncode != 0,
    reason="perl >= 5.18 required",
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


@pytest.fixture
async def at_breakpoint(tmp_path):
    p = tmp_path / "insp.pl"
    p.write_text(SCRIPT)
    c = AdapterClient()
    await c.start()
    await c.request("initialize", {"adapterID": "perl-tdb"})
    launch_fut = c.send(
        "launch",
        {
            "program": str(p),
            "args": [],
            "cwd": str(tmp_path),
            "stopOnEntry": False,
        },
    )
    await c.wait_event("initialized")
    await c.request(
        "setBreakpoints", {"source": {"path": str(p)}, "breakpoints": [{"line": 4}]}
    )
    await c.request("configurationDone")
    await asyncio.wait_for(launch_fut, 30)
    await c.wait_event("stopped")
    yield c, str(p)
    await c.stop()


async def test_stack_scopes_variables_expand(at_breakpoint):
    c, path = at_breakpoint
    st = await c.request("stackTrace", {"threadId": 1})
    frames = st["body"]["stackFrames"]
    assert frames[0]["source"]["path"] == path
    assert frames[0]["line"] == 4
    assert any("work" in f["name"] for f in frames)

    sc = await c.request("scopes", {"frameId": frames[0]["id"]})
    by_name = {s["name"]: s for s in sc["body"]["scopes"]}
    assert {"Lexicals", "Globals", "Specials"} <= set(by_name)

    lex = await c.request(
        "variables", {"variablesReference": by_name["Lexicals"]["variablesReference"]}
    )
    lex_vars = {v["name"]: v for v in lex["body"]["variables"]}
    if "<lexicals>" in lex_vars:
        assert "PadWalker" in lex_vars["<lexicals>"]["value"]
    else:
        assert "%info" in lex_vars
        nested = await c.request(
            "variables", {"variablesReference": lex_vars["%info"]["variablesReference"]}
        )
        names = {v["name"] for v in nested["body"]["variables"]}
        assert {"label", "nums"} <= names

    spec = await c.request(
        "variables", {"variablesReference": by_name["Specials"]["variablesReference"]}
    )
    spec_names = {v["name"] for v in spec["body"]["variables"]}
    assert "@_" in spec_names or "$0" in spec_names


async def test_evaluate_in_top_frame(at_breakpoint):
    c, _ = at_breakpoint
    ok = await c.request("evaluate", {"expression": "1 + 2", "context": "repl"})
    assert ok["body"]["result"] == "3"
    lex = await c.request("evaluate", {"expression": "$label", "context": "repl"})
    assert lex["body"]["result"] == "'go'"
    err = await c.request("evaluate", {"expression": "die 'nope'", "context": "repl"})
    assert err["success"] is False and "nope" in err["message"]

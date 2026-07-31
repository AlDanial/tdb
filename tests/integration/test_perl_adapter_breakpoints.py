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
my $total = 0;

for my $i (1 .. 5) {
    $total += $i;
}
print "total=$total\\n";
"""


@pytest.fixture
def script(tmp_path):
    p = tmp_path / "bp.pl"
    p.write_text(SCRIPT)
    return str(p)


@pytest.fixture
async def started(script, tmp_path):
    c = AdapterClient()
    await c.start()
    await c.request("initialize", {"adapterID": "perl-tdb"})
    launch_fut = c.send(
        "launch",
        {
            "program": script,
            "args": [],
            "cwd": str(tmp_path),
            "stopOnEntry": True,
        },
    )
    await c.wait_event("initialized")
    yield c, script, launch_fut
    await c.stop()


async def test_set_hit_and_conditional_breakpoints(started):
    c, script, launch_fut = started
    resp = await c.request(
        "setBreakpoints",
        {
            "source": {"path": script},
            "breakpoints": [{"line": 4, "condition": "$i == 3"}],
        },
    )
    (bp,) = resp["body"]["breakpoints"]
    assert bp["verified"] is True and bp["line"] == 4
    await c.request("configurationDone")
    await asyncio.wait_for(launch_fut, 30)
    await c.wait_event("stopped")  # entry
    await c.request("continue")
    stopped = await c.wait_event("stopped")  # conditional breakpoint
    assert stopped["body"]["reason"] == "breakpoint"


async def test_blank_line_snaps_forward(started):
    c, script, launch_fut = started
    resp = await c.request(
        "setBreakpoints",
        {
            "source": {"path": script},
            "breakpoints": [{"line": 2}],  # blank line
        },
    )
    (bp,) = resp["body"]["breakpoints"]
    assert bp["verified"] is True
    assert bp["line"] == 3


async def test_replace_clears_old_breakpoints(started):
    c, script, launch_fut = started
    await c.request(
        "setBreakpoints", {"source": {"path": script}, "breakpoints": [{"line": 4}]}
    )
    await c.request("setBreakpoints", {"source": {"path": script}, "breakpoints": []})
    await c.request("configurationDone")
    await asyncio.wait_for(launch_fut, 30)
    await c.wait_event("stopped")  # entry
    await c.request("continue")
    await c.wait_event("terminated", timeout=30)  # ran through: no bp left


# --- Task 10 guard proof: breakable() must not poison not-yet-loaded ---
# files (see server.py's _on_setBreakpoints comment and helpers.pl's
# breakable() guard). This drives the guard through the REAL DAP
# request path (not just PerlSession), covering the scenario the brief
# didn't anticipate: setBreakpoints against a module the debuggee
# hasn't `require`d yet.

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
        "my $dummy = 0;\n"
        f"require {str(second)!r};\n"
        "Second::greet();\n"
        'print "done\\n";\n'
    )
    return str(main), str(second)


@pytest.fixture
async def two_file_started(two_file_program, tmp_path):
    main, second = two_file_program
    c = AdapterClient()
    await c.start()
    await c.request("initialize", {"adapterID": "perl-tdb"})
    launch_fut = c.send(
        "launch",
        {"program": main, "args": [], "cwd": str(tmp_path), "stopOnEntry": True},
    )
    await c.wait_event("initialized")
    yield c, main, second, launch_fut
    await c.stop()


async def test_breakpoint_in_not_yet_loaded_module_still_fires(two_file_started):
    c, main, second, launch_fut = two_file_started

    # second.pl hasn't been require'd yet: v1's acceptable behavior is
    # verified=False (perl5db rejects `b <file>:<line>` on an unloaded
    # file with "not breakable" -- confirmed via probe), but critically
    # this must NOT poison second.pl's breakability for later.
    resp = await c.request(
        "setBreakpoints",
        {"source": {"path": second}, "breakpoints": [{"line": 5}]},
    )
    (bp,) = resp["body"]["breakpoints"]
    assert bp["verified"] is False

    # Anchor on main.pl's require call site so we stop right after
    # second.pl loads.
    await c.request(
        "setBreakpoints", {"source": {"path": main}, "breakpoints": [{"line": 3}]}
    )
    await c.request("configurationDone")
    await asyncio.wait_for(launch_fut, 30)
    await c.wait_event("stopped")  # entry
    await c.request("continue")
    stopped = await c.wait_event("stopped")  # main.pl:3, after require
    assert stopped["body"]["reason"] == "breakpoint"

    # Now that second.pl is loaded, re-request its breakpoint: this
    # proves the earlier unloaded attempt did not poison perl5db's
    # breakpoint machinery for this file -- it verifies and fires.
    resp2 = await c.request(
        "setBreakpoints",
        {"source": {"path": second}, "breakpoints": [{"line": 5}]},
    )
    (bp2,) = resp2["body"]["breakpoints"]
    assert bp2["verified"] is True and bp2["line"] == 5

    await c.request("continue")
    stopped2 = await c.wait_event("stopped")  # inside Second::greet
    assert stopped2["body"]["reason"] == "breakpoint"


# --- Task 10: `B <line>` deletions must target perl5db's per-file table ---
# for the requested `path`, not whatever file perl5db's debugger cursor
# happens to be parked on. Reproduced with two files: set + then clear a
# breakpoint in mod2.pl while stopped in main.pl -- the clear must not
# silently no-op (leaving the stale mod2.pl breakpoint live).

MOD2_FILE = """\
package mod2;
sub twice {
    my $x = 1;
    return $x * 2;
}
1;
"""


@pytest.fixture
def cross_file_program(tmp_path):
    mod2 = tmp_path / "mod2.pl"
    mod2.write_text(MOD2_FILE)
    main = tmp_path / "main.pl"
    main.write_text(
        "my $dummy = 0;\n"
        f"require {str(mod2)!r};\n"
        "mod2::twice();\n"
        "mod2::twice();\n"
        'print "done\\n";\n'
    )
    return str(main), str(mod2)


@pytest.fixture
async def cross_file_started(cross_file_program, tmp_path):
    main, mod2 = cross_file_program
    c = AdapterClient()
    await c.start()
    await c.request("initialize", {"adapterID": "perl-tdb"})
    launch_fut = c.send(
        "launch",
        {"program": main, "args": [], "cwd": str(tmp_path), "stopOnEntry": True},
    )
    await c.wait_event("initialized")
    yield c, main, mod2, launch_fut
    await c.stop()


async def test_replace_clears_old_breakpoints_in_other_file(cross_file_started):
    c, main, mod2, launch_fut = cross_file_started

    # Anchor on main.pl:3 (after the require, before either twice() call)
    # so we end up stopped in main.pl with mod2.pl already loaded.
    await c.request(
        "setBreakpoints", {"source": {"path": main}, "breakpoints": [{"line": 3}]}
    )
    await c.request("configurationDone")
    await asyncio.wait_for(launch_fut, 30)
    await c.wait_event("stopped")  # entry
    await c.request("continue")
    stopped = await c.wait_event("stopped")  # main.pl:3
    assert stopped["body"]["reason"] == "breakpoint"

    # Set a breakpoint in mod2.pl (now loaded) while stopped in main.pl.
    resp = await c.request(
        "setBreakpoints",
        {"source": {"path": mod2}, "breakpoints": [{"line": 3}]},
    )
    (bp,) = resp["body"]["breakpoints"]
    assert bp["verified"] is True and bp["line"] == 3

    # Replace with an empty set -- this must delete the mod2.pl
    # breakpoint even though perl5db's debugger cursor is on main.pl.
    await c.request("setBreakpoints", {"source": {"path": mod2}, "breakpoints": []})

    # Clear main.pl's own breakpoint too so we run straight through.
    await c.request("setBreakpoints", {"source": {"path": main}, "breakpoints": []})

    await c.request("continue")
    await c.wait_event("terminated", timeout=30)  # must NOT stop in mod2.pl

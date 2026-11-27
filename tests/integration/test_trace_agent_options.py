"""Trace agent options: capture, return, if, max_hits; the event cap; the
re-entrancy guard; signal-handler exceptions inside callbacks."""

from __future__ import annotations

import os
import sys

import pytest

from tests.integration.trace_helpers import run_traced, target

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="sys.monitoring")


def test_capture_values_on_entry(tmp_path):
    parts = run_traced(
        tmp_path,
        "def f(a, b):\n    pass\nf(1, 2)\n",
        [
            target(
                tmp_path / "prog.py",
                {"pattern": "f", "capture": ["a", "b * 2", "missing"]},
            )
        ],
    )
    assert parts.calls("f")[0]["v"] == {
        "a": "1",
        "b * 2": "4",
        "missing": "<error: NameError: name 'missing' is not defined>",
    }


def test_return_value_recorded_only_when_asked(tmp_path):
    src = "def f():\n    return {'k': 1}\ndef g():\n    return 5\nf(); g()\n"
    parts = run_traced(
        tmp_path,
        src,
        [target(tmp_path / "prog.py", {"pattern": "f", "return": True}, "g")],
    )
    rets = {
        parts.qual_of(c["_stem"], c["fn"]): parts.events_for(c["_stem"], c["c"])[-1]
        for c in parts.calls("f") + parts.calls("g")
    }
    assert rets["f"]["v"] == "{'k': 1}" and "v" not in rets["g"]


def test_if_condition_filters_whole_call(tmp_path):
    src = "import asyncio\nasync def h(n):\n    await asyncio.sleep(0)\nasync def main():\n    for n in range(4):\n        await h(n)\nasyncio.run(main())\n"
    parts = run_traced(
        tmp_path,
        src,
        [
            target(
                tmp_path / "prog.py",
                {"pattern": "h", "capture": ["n"], "if": "n % 2 == 0"},
            )
        ],
    )
    recorded = {c["c"] for c in parts.calls("h")}
    assert [c["v"]["n"] for c in parts.calls("h")] == ["0", "2"]
    assert all(r["c"] in recorded for k in ("susp", "res", "ret") for r in parts.all(k))


def test_condition_that_raises_skips_call(tmp_path):
    parts = run_traced(
        tmp_path,
        "def f(x):\n    pass\nf(1)\n",
        [target(tmp_path / "prog.py", {"pattern": "f", "if": "x.nope"})],
    )
    assert parts.calls("f") == [] and parts.returncode == 0


def test_max_hits(tmp_path):
    parts = run_traced(
        tmp_path,
        "def f():\n    pass\nfor _ in range(10):\n    f()\n",
        [target(tmp_path / "prog.py", {"pattern": "f", "max_hits": 3})],
    )
    assert len(parts.calls("f")) == 3 and len(parts.all("ret")) == 3


def test_event_cap(tmp_path):
    parts = run_traced(
        tmp_path, "def f():\n    pass\nfor _ in range(50):\n    f()\n", max_events=10
    )
    assert len(parts.calls("f")) == 5
    assert [n["msg"] for n in parts.all("note")] == [
        "event cap of 10 reached; later calls not recorded",
        "45 calls not recorded (event cap)",
    ]


def test_reentrant_capture_does_not_recurse(tmp_path):
    src = "def helper():\n    return 1\ndef f():\n    pass\nf(); helper()\n"
    parts = run_traced(
        tmp_path,
        src,
        [
            target(
                tmp_path / "prog.py",
                {"pattern": "f", "capture": ["helper()"]},
                "helper",
            )
        ],
    )
    assert (
        parts.calls("f")[0]["v"] == {"helper()": "1"}
        and len(parts.calls("helper")) == 1
    )


def test_hostile_values(tmp_path):  # Review Focus 4
    src = (
        "class Bad:\n    def __repr__(self):\n        raise RuntimeError('nope')\n"
        "def f(a, b, c, d):\n    pass\nf(Bad(), 'x' * 1_000_000, '\\udcff', b'\\xff')\n"
    )
    parts = run_traced(
        tmp_path,
        src,
        [
            target(
                tmp_path / "prog.py", {"pattern": "f", "capture": ["a", "b", "c", "d"]}
            )
        ],
    )
    v = parts.calls("f")[0]["v"]
    assert (
        v["a"] == "<error: RuntimeError: nope>"
        and len(v["b"]) <= 200
        and v["d"] == "b'\\xff'"
    )
    assert v["c"] == repr("\udcff")


def test_overlapping_entries_specific_wins(tmp_path):  # Review Focus 3
    src = "class C:\n    def m(self, x):\n        pass\nC().m(7)\n"
    prog = tmp_path / "prog.py"
    parts = run_traced(
        tmp_path,
        src,
        [target(prog, "C.*"), target(prog, {"pattern": "C.m", "capture": ["x"]})],
    )
    (c,) = parts.calls("C.m")
    assert c["v"] == {"x": "7"}


# --- beyond the brief's tests ----------------------------------------------


def test_first_matching_wildcard_entry_wins(tmp_path):
    src = "class C:\n    def m(self):\n        pass\nC().m()\n"
    entries = (
        {"pattern": "C.*", "capture": ["1"]},
        {"pattern": "*.m", "capture": ["2"]},
    )
    parts = run_traced(tmp_path, src, [target(tmp_path / "prog.py", *entries)])
    (c,) = parts.calls("C.m")
    assert c["v"] == {"1": "1"}


def test_wildcards_never_match_lambdas_or_genexprs(tmp_path):
    src = "f = lambda: 1\ndef g():\n    return list(y for y in (1,))\nf(); g()\n"
    parts = run_traced(tmp_path, src, [target(tmp_path / "prog.py", "*", "<lambda>")])
    assert sorted(r["qual"] for r in parts.all("fn")) == ["<lambda>", "g"]


def test_max_hits_counts_per_entry(tmp_path):
    # One entry, matched by two functions: its max_hits is shared.
    src = "def f():\n    pass\ndef g():\n    pass\nfor _ in range(3):\n    f(); g()\n"
    parts = run_traced(
        tmp_path, src, [target(tmp_path / "prog.py", {"pattern": "*", "max_hits": 3})]
    )
    assert (len(parts.calls("f")), len(parts.calls("g"))) == (2, 1)


def test_event_cap_keeps_ends_of_recorded_calls(tmp_path):
    src = (
        "import asyncio\nasync def inner():\n    await asyncio.sleep(0)\n"
        "async def outer():\n    for _ in range(20):\n        await inner()\n"
        "asyncio.run(outer())\n"
    )
    parts = run_traced(tmp_path, src, max_events=6)
    (outer,) = parts.calls("outer")
    kinds = [r["k"] for r in parts.events_for(outer["_stem"], outer["c"])]
    assert kinds == ["call"] + ["susp", "res"] * 20 + ["ret"]
    assert len(parts.calls("inner")) == 1
    assert [n["msg"] for n in parts.all("note")] == [
        "event cap of 6 reached; later calls not recorded",
        "19 calls not recorded (event cap)",
    ]


def test_failing_return_repr_is_an_error_value(tmp_path):
    src = (
        "class Bad:\n    def __repr__(self):\n        raise RuntimeError('nope')\n"
        "def f():\n    return Bad()\nf()\n"
    )
    parts = run_traced(
        tmp_path, src, [target(tmp_path / "prog.py", {"pattern": "f", "return": True})]
    )
    (c,) = parts.calls("f")
    assert (
        parts.events_for(c["_stem"], c["c"])[-1]["v"] == "<error: RuntimeError: nope>"
    )


def test_expression_that_does_not_compile(tmp_path):
    # tdb compiles the spec's expressions before launch, with its own
    # Python; a traced Python with another grammar may still reject one.
    src = "def f():\n    pass\ndef g():\n    pass\nf(); g()\n"
    entries = ({"pattern": "f", "capture": ["1 +", "2"]}, {"pattern": "g", "if": "1 +"})
    parts = run_traced(tmp_path, src, [target(tmp_path / "prog.py", *entries)])
    (c,) = parts.calls("f")
    assert c["v"]["1 +"].startswith("<error: SyntaxError: ") and c["v"]["2"] == "2"
    assert parts.calls("g") == [] and parts.returncode == 0


def test_expression_raising_base_exception_never_reaches_program(tmp_path):
    src = "import sys\ndef f():\n    pass\nf()\nprint('done')\n"
    parts = run_traced(
        tmp_path,
        src,
        [target(tmp_path / "prog.py", {"pattern": "f", "capture": ["sys.exit(3)"]})],
    )
    assert parts.calls("f")[0]["v"] == {"sys.exit(3)": "<error: SystemExit: 3>"}
    assert parts.returncode == 0 and parts.stdout == "done\n"


def test_capture_reentering_the_tracer_is_not_recorded(tmp_path):
    # Python delivers no events while a callback runs, so helper() in a
    # capture is never seen; sys.call_tracing turns them back on, and then
    # the re-entrancy guard must keep the nested callbacks out.
    src = "import sys\ndef helper():\n    return 1\ndef f():\n    pass\nf(); helper()\n"
    expr = "sys.call_tracing(helper, ())"
    parts = run_traced(
        tmp_path,
        src,
        [target(tmp_path / "prog.py", {"pattern": "f", "capture": [expr]}, "helper")],
    )
    assert parts.calls("f")[0]["v"] == {expr: "1"}
    assert len(parts.calls("helper")) == 1 and len(parts.all("ret")) == 2


_ALARM = """\
import functools, signal, time
class Alarm:
    def on_alarm(self, *args):
        raise TimeoutError("alarm")
def on_alarm(*args):
    raise TimeoutError("alarm")
def f(x):
    return x
signal.signal(signal.SIGALRM, HANDLER)
caught = 0
for _ in range(3):
    signal.setitimer(signal.ITIMER_REAL, 0.01)
    end = time.monotonic() + 0.25
    try:
        while time.monotonic() < end:
            f(1)
        signal.setitimer(signal.ITIMER_REAL, 0)  # in the try: it may be late
    except TimeoutError:
        caught += 1
print(caught)
"""


@pytest.mark.skipif(os.name == "nt", reason="SIGALRM")
@pytest.mark.parametrize(
    ("handler", "options"),
    [
        # Lands in the callbacks' own code.
        ("on_alarm", {}),
        # Lands while a capture, or an if, is evaluated.
        (
            "functools.partial(on_alarm, 0)",
            {"capture": ["sum([i for i in range(300)])"]},
        ),
        ("Alarm().on_alarm", {"if": "all([i >= 0 for i in range(300)])"}),
    ],
)
def test_signal_handler_exception_reaches_program(tmp_path, handler, options):
    # Python runs signal handlers inside monitoring callbacks too.  An
    # alarm-style timeout raised there must reach the program; each of the
    # three alarms below would otherwise be lost (and its round run out).
    parts = run_traced(
        tmp_path,
        _ALARM.replace("HANDLER", handler),
        [target(tmp_path / "prog.py", {"pattern": "f", **options})],
        timeout=30,
    )
    assert parts.stdout.strip() == "3", parts.stderr
    assert parts.calls("f") and parts.all("exit")  # tracing went on


@pytest.mark.parametrize("options", [{"capture": ["x"]}, {"if": "x is not None"}])
def test_options_keep_no_values_alive(tmp_path, options):
    # On Python 3.12 frame.f_locals is a snapshot dict the frame keeps:
    # once read for an expression, it would hold the call's values until
    # the call ends.  locals() must still work afterwards.
    src = (
        "import gc, weakref\nclass O:\n    pass\n"
        "def f(x):\n    r = weakref.ref(x)\n    del x\n    gc.collect()\n"
        "    print(r() is None, sorted(locals()))\nf(O())\n"
    )
    parts = run_traced(
        tmp_path, src, [target(tmp_path / "prog.py", {"pattern": "f", **options})]
    )
    assert parts.stdout == "True ['r']\n", parts.stderr
    assert len(parts.calls("f")) == 1


@pytest.mark.skipif(os.name == "nt", reason="SIGUSR1")
def test_signal_at_a_return_ends_the_call_with_its_exception(tmp_path):
    # The handler's exception is raised at f's return, inside the return
    # value's __repr__; f's frame then unwinds with it, so the call ends
    # with a raise record, never unfinished.
    src = (
        "import signal\ndef on_usr1(*args):\n    raise TimeoutError('alarm')\n"
        "signal.signal(signal.SIGUSR1, on_usr1)\n"
        "class Loud:\n    def __repr__(self):\n"
        "        signal.raise_signal(signal.SIGUSR1)\n        return 'loud'\n"
        "def f():\n    return Loud()\n"
        "try:\n    f()\nexcept TimeoutError:\n    print('caught')\n"
    )
    parts = run_traced(
        tmp_path, src, [target(tmp_path / "prog.py", {"pattern": "f", "return": True})]
    )
    assert parts.stdout == "caught\n", parts.stderr
    (c,) = parts.calls("f")
    ends = [(r["k"], r.get("exc")) for r in parts.events_for(c["_stem"], c["c"])[1:]]
    assert ends == [("raise", "TimeoutError: alarm")]

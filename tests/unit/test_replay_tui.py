"""ReplayDriver feeds a recording through the TUI's own gesture handlers."""

import asyncio

import pytest

from tdb.app import TdbApp
from tdb.dap.types import Scope, SourceBreakpoint, Variable
from tdb.persist import TdbConfig
from tdb.replay import Recording
from tdb.replay_tui import ReplayDriver
from tdb.session.state import SessionPhase
from tdb.widgets.evaluate_console import EvaluateConsole
from tdb.widgets.variable_view import VariableView


HEADER = {
    "tdb_recording": 1,
    "mode": "launch",
    "language": "python",
    "program": "/abs/p.py",
    "args": [],
    "cwd": "/abs",
}


def recording(*records):
    """Build a Recording from (action, params) pairs, 1 s apart."""
    recs = [
        {"t": float(i + 1), "action": action, "params": list(params)}
        for i, (action, params) in enumerate(records)
    ]
    return Recording(header=dict(HEADER), records=recs)


async def _no_sleep(_seconds):
    return None


def _mark_stopped(app):
    app.controller.state.transition_to(SessionPhase.STOPPED)
    app.stop_settled.set()


@pytest.fixture
async def app_pilot():
    app = TdbApp(program="", config=TdbConfig())
    async with app.run_test() as pilot:
        await pilot.pause()
        _mark_stopped(app)  # pretend the entry stop already landed
        yield app, pilot


def make_driver(rec, **kw):
    driver = ReplayDriver(rec, label="s.jsonl", **kw)
    driver.sleep = _no_sleep
    return driver


# --- stepping ---------------------------------------------------------


async def test_next_issues_step_over_and_waits_for_settled_stop(app_pilot, monkeypatch):
    app, _ = app_pilot
    calls = []

    async def fake_step_over():
        calls.append("step_over")
        assert not app.stop_settled.is_set()  # driver cleared it before stepping
        app.call_later(_mark_stopped, app)

    monkeypatch.setattr(app.controller, "step_over", fake_step_over)
    driver = make_driver(recording(("next", [])))
    errors = await driver.run(app)
    assert errors == 0
    assert calls == ["step_over"]


async def test_step_that_never_stops_reports_still_running_not_error(
    app_pilot, monkeypatch
):
    app, _ = app_pilot

    async def fake_continue():
        app.controller.state.transition_to(SessionPhase.RUNNING)

    monkeypatch.setattr(app.controller, "continue_", fake_continue)
    driver = make_driver(recording(("continue", [])), replay_timeout=0.05)
    errors = await driver.run(app)
    assert errors == 0
    assert any("still running" in n for n in driver.transcript)


async def test_driver_waits_for_entry_stop_before_first_command(app_pilot, monkeypatch):
    app, _ = app_pilot
    app.stop_settled.clear()
    app.controller.state.transition_to(SessionPhase.RUNNING)
    order = []

    async def fake_eval(expr):
        order.append(("eval", app.controller.state.phase))
        return "1"

    monkeypatch.setattr(app.controller, "evaluate", fake_eval)
    driver = make_driver(recording(("evaluate", ["x"])))
    task = asyncio.ensure_future(driver.run(app))
    await asyncio.sleep(0.05)
    assert order == []  # still parked, waiting for the entry stop
    _mark_stopped(app)
    errors = await task
    assert errors == 0
    assert order == [("eval", SessionPhase.STOPPED)]


# --- evaluate ---------------------------------------------------------


async def test_evaluate_echoes_expression_and_result_in_console(app_pilot, monkeypatch):
    app, pilot = app_pilot

    async def fake_eval(expr):
        return "42"

    monkeypatch.setattr(app.controller, "evaluate", fake_eval)
    driver = make_driver(recording(("evaluate", ["x + 1"])))
    errors = await driver.run(app)
    await pilot.pause()
    assert errors == 0
    console = app.query_one("#eval-console", EvaluateConsole)
    text = "\n".join(strip.text for strip in console.query_one("#eval-output").lines)
    assert ">>> x + 1" in text
    assert "42" in text


# --- pacing / progress / summary --------------------------------------


async def test_pacing_sleeps_recorded_deltas(app_pilot, monkeypatch):
    app, _ = app_pilot
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    async def fake_eval(expr):
        return ""

    monkeypatch.setattr(app.controller, "evaluate", fake_eval)
    rec = Recording(
        header=dict(HEADER),
        records=[
            {"t": 0.5, "action": "evaluate", "params": ["a"]},
            {"t": 2.0, "action": "evaluate", "params": ["b"]},
            {"t": 2.0, "action": "evaluate", "params": ["c"]},
        ],
    )
    driver = ReplayDriver(rec, label="s.jsonl")
    driver.sleep = fake_sleep
    await driver.run(app)
    assert slept == [0.5, 1.5]


async def test_progress_and_summary_are_surfaced(app_pilot, monkeypatch):
    app, _ = app_pilot
    titles = []
    notes = []

    async def fake_sleep(s):
        titles.append(app.title)

    async def fake_eval(expr):
        return ""

    monkeypatch.setattr(app.controller, "evaluate", fake_eval)
    monkeypatch.setattr(
        app, "notify", lambda msg, **kw: notes.append((msg, kw.get("severity")))
    )
    driver = ReplayDriver(
        recording(("evaluate", ["a"]), ("stack_up", []), ("evaluate", ["b"])),
        label="s.jsonl",
    )
    driver.sleep = fake_sleep
    errors = await driver.run(app)
    assert errors == 1  # stack_up with no frames
    # Sampled at each pacing sleep, i.e. just before command i runs.
    assert titles == ["tdb", "tdb ▶ s.jsonl 1/3", "tdb ▶ s.jsonl 2/3"]
    assert app.title == "tdb"  # restored
    assert any(sev == "error" for _, sev in notes)
    assert notes[-1][0] == "Replay finished: 3 commands, 1 errors"


# --- breakpoints ------------------------------------------------------


@pytest.fixture
def fake_breakpoints(app_pilot, monkeypatch):
    """Stub the controller's breakpoint calls; record them in `calls`."""
    app, _ = app_pilot
    calls = []
    bps = app.controller.state.breakpoints

    async def toggle(path, line):
        calls.append(("toggle", path, line))
        lst = bps.setdefault(path, [])
        if any(bp.line == line for bp in lst):
            lst[:] = [bp for bp in lst if bp.line != line]
        else:
            lst.append(SourceBreakpoint(line=line))

    async def set_condition(path, line, condition, hit_condition):
        calls.append(("condition", path, line, condition, hit_condition))

    monkeypatch.setattr(app.controller, "toggle_breakpoint", toggle)
    monkeypatch.setattr(app.controller, "set_breakpoint_condition", set_condition)
    return calls


async def test_set_breakpoint_toggles_through_code_view_path(
    app_pilot, fake_breakpoints
):
    app, _ = app_pilot
    driver = make_driver(recording(("set_breakpoint", ["/abs/p.py:3"])))
    assert await driver.run(app) == 0
    assert fake_breakpoints == [("toggle", "/abs/p.py", 3)]


async def test_set_breakpoint_with_condition_applies_condition(
    app_pilot, fake_breakpoints
):
    app, _ = app_pilot
    driver = make_driver(recording(("set_breakpoint", ["/abs/p.py:3", "x > 1", ""])))
    assert await driver.run(app) == 0
    assert fake_breakpoints == [
        ("toggle", "/abs/p.py", 3),
        ("condition", "/abs/p.py", 3, "x > 1", None),
    ]


async def test_set_breakpoint_already_present_does_not_toggle_it_off(
    app_pilot, fake_breakpoints
):
    app, _ = app_pilot
    app.controller.state.breakpoints["/abs/p.py"] = [SourceBreakpoint(line=3)]
    driver = make_driver(recording(("set_breakpoint", ["/abs/p.py:3"])))
    assert await driver.run(app) == 0
    assert fake_breakpoints == []


async def test_remove_breakpoint_toggles_existing_and_errors_on_missing(
    app_pilot, fake_breakpoints
):
    app, _ = app_pilot
    app.controller.state.breakpoints["/abs/p.py"] = [SourceBreakpoint(line=3)]
    driver = make_driver(
        recording(
            ("remove_breakpoint", ["/abs/p.py:3"]),
            ("remove_breakpoint", ["/abs/p.py:9"]),
        )
    )
    assert await driver.run(app) == 1
    assert fake_breakpoints == [("toggle", "/abs/p.py", 3)]


async def test_malformed_breakpoint_spec_is_error(app_pilot, fake_breakpoints):
    app, _ = app_pilot
    driver = make_driver(recording(("set_breakpoint", ["nonsense"])))
    assert await driver.run(app) == 1
    assert fake_breakpoints == []


# --- inspect (variable expansion) -------------------------------------


def _load_variables(app):
    data = Variable(
        name="data",
        value="{...}",
        type="dict",
        variables_reference=7,
        evaluate_name="data",
    )
    app.controller.state.variables = {5: [data]}
    var_view = app.query_one("#variable-view", VariableView)
    var_view.update_variables(
        [Scope(name="Locals", variables_reference=5)], {5: [data]}
    )
    return var_view


async def test_inspect_expands_the_matching_variable_node(app_pilot, monkeypatch):
    app, pilot = app_pilot
    var_view = _load_variables(app)

    class FakeClient:
        async def variables(self, ref):
            return (
                [Variable(name="x", value="1", evaluate_name="data['x']")]
                if ref == 7
                else []
            )

    monkeypatch.setattr(
        type(app.controller), "active_client", property(lambda self: FakeClient())
    )
    driver = make_driver(recording(("inspect", ["data"])))
    assert await driver.run(app) == 0
    await pilot.pause()
    node = next(n for n in var_view.root.children[0].children if n.data == 7)
    assert node.is_expanded
    assert [str(c.label) for c in node.children] == ["x = 1"]


async def test_inspect_unknown_expression_is_error(app_pilot):
    app, _ = app_pilot
    _load_variables(app)
    driver = make_driver(recording(("inspect", ["nope"])))
    assert await driver.run(app) == 1


# --- restart / quit ---------------------------------------------------


async def test_quit_closes_the_app(app_pilot, monkeypatch):
    app, _ = app_pilot

    async def fake_stop():
        return None

    monkeypatch.setattr(app.controller, "stop", fake_stop)
    driver = make_driver(recording(("quit", []), ("evaluate", ["never"])))
    assert await driver.run(app) == 0
    assert app._exit
    assert len(driver.transcript) == 1  # nothing after quit


async def test_restart_relaunches_and_waits_for_new_entry_stop(app_pilot, monkeypatch):
    app, _ = app_pilot
    old_controller = app.controller
    seen = []

    async def fake_restart(new_program=None, *, start_immediately=True):
        # Simulate what the real worker does: stale terminated from the
        # old session, new controller, then its entry stop.
        app.stop_settled.set()  # stale: must not satisfy the driver
        await asyncio.sleep(0.02)
        from tdb.session.controller import DebugController

        app.controller = DebugController(app._event_handler, profile=app._profile)
        await asyncio.sleep(0.02)
        _mark_stopped(app)

    async def fake_eval(self, expr):
        seen.append(self is old_controller)
        return ""

    from tdb.session.controller import DebugController

    monkeypatch.setattr(
        app, "_restart_session", lambda *a, **k: app.run_worker(fake_restart(*a, **k))
    )
    # Class-level: the restart swaps in a fresh controller instance.
    monkeypatch.setattr(DebugController, "evaluate", fake_eval)
    driver = make_driver(recording(("restart", []), ("evaluate", ["x"])))
    monkeypatch.setattr(
        app.controller.__class__, "supports_restart", property(lambda s: True)
    )
    assert await driver.run(app) == 0
    assert seen == [False]  # evaluate ran against the NEW controller


async def test_quiet_driver_skips_per_action_toasts_but_keeps_errors_and_summary(
    app_pilot, monkeypatch
):
    app, _ = app_pilot
    notes = []

    async def fake_eval(expr):
        return ""

    monkeypatch.setattr(app.controller, "evaluate", fake_eval)
    monkeypatch.setattr(
        app, "notify", lambda msg, **kw: notes.append((msg, kw.get("severity")))
    )
    driver = make_driver(
        recording(("evaluate", ["a"]), ("stack_up", [])), announce=False
    )
    assert await driver.run(app) == 1
    assert [m for m, _ in notes if m.startswith("evaluate")] == []
    assert any(sev == "error" for _, sev in notes)  # failed stack_up still shown
    assert notes[-1][0] == "Replay finished: 2 commands, 1 errors"


async def test_fixed_interval_replaces_recorded_pacing(app_pilot, monkeypatch):
    app, _ = app_pilot
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    async def fake_eval(expr):
        return ""

    monkeypatch.setattr(app.controller, "evaluate", fake_eval)
    rec = Recording(
        header=dict(HEADER),
        records=[
            {"t": 0.5, "action": "evaluate", "params": ["a"]},
            {"t": 9.0, "action": "evaluate", "params": ["b"]},
            {"t": 9.0, "action": "evaluate", "params": ["c"]},
        ],
    )
    driver = ReplayDriver(rec, label="s.jsonl", interval=0.25)
    driver.sleep = fake_sleep
    await driver.run(app)
    assert slept == [0.25, 0.25, 0.25]

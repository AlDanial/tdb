"""gdb-style `display` list in DebugController.

`state.display` holds expressions the user asked to watch at every stop.
`fetch_scopes_and_variables` renders them as a synthetic "Display" scope
placed *first*, and an expression that does not resolve in the current
frame is simply omitted (unlike the Interactive scope, which marks such
names unavailable). `display X` / `undisplay X` typed at the Evaluate
console are intercepted by `evaluate_console` instead of being sent to
the adapter.
"""

from __future__ import annotations

from tdb.dap.types import Scope, Variable
from tdb.session.state import DISPLAY_SCOPE_REF

from tests.unit.test_controller_actions import _make


async def test_fetch_scopes_prepends_display_scope_with_values():
    ctrl, fake, _ = _make()
    ctrl.state.display.append("x")
    fake.evaluate_raw_effects.append(
        {"result": "42", "type": "int", "variablesReference": 0}
    )
    await ctrl.fetch_scopes_and_variables(11)
    assert ctrl.state.scopes[0] == Scope(
        name="Display", variables_reference=DISPLAY_SCOPE_REF
    )
    assert ctrl.state.variables[DISPLAY_SCOPE_REF] == [
        Variable(
            name="x", value="42", type="int", variables_reference=0, evaluate_name="x"
        )
    ]
    assert fake.calls_to("evaluate_raw") == [("evaluate_raw", "x", 11, "watch")]


async def test_fetch_scopes_omits_display_expression_undefined_in_frame():
    ctrl, fake, _ = _make()
    ctrl.state.display.extend(["gone", "here"])
    fake.evaluate_raw_effects.append(RuntimeError("NameError: gone"))
    fake.evaluate_raw_effects.append({"result": "1", "variablesReference": 0})
    await ctrl.fetch_scopes_and_variables(11)
    assert [v.name for v in ctrl.state.variables[DISPLAY_SCOPE_REF]] == ["here"]
    # Still on the list: it reappears in a frame where it is defined.
    assert ctrl.state.display == ["gone", "here"]


async def test_fetch_scopes_adds_no_display_scope_when_nothing_resolves():
    ctrl, fake, _ = _make()
    ctrl.state.display.append("gone")
    fake.evaluate_raw_effects.append(RuntimeError("NameError: gone"))
    await ctrl.fetch_scopes_and_variables(11)
    assert [s.name for s in ctrl.state.scopes] == ["Locals"]
    assert DISPLAY_SCOPE_REF not in ctrl.state.variables


async def test_fetch_scopes_without_display_list_adds_no_scope():
    ctrl, fake, _ = _make()
    await ctrl.fetch_scopes_and_variables(11)
    assert [s.name for s in ctrl.state.scopes] == ["Locals"]
    assert fake.calls_to("evaluate_raw") == []


async def test_display_command_adds_expression_without_evaluating_it():
    ctrl, fake, _ = _make()
    result = await ctrl.evaluate_console("display  x ")
    assert ctrl.state.display == ["x"]
    assert "x" in result
    assert fake.calls_to("evaluate") == []
    # Scopes re-fetched so the Display scope shows without a step.
    assert fake.calls_to("scopes") == [("scopes", 11)]


async def test_display_command_ignores_duplicates():
    ctrl, fake, _ = _make()
    await ctrl.evaluate_console("display x")
    await ctrl.evaluate_console("display x")
    assert ctrl.state.display == ["x"]


async def test_undisplay_command_removes_expression():
    ctrl, fake, _ = _make()
    await ctrl.evaluate_console("display x")
    await ctrl.evaluate_console("display y")
    await ctrl.evaluate_console("undisplay x")
    assert ctrl.state.display == ["y"]
    assert fake.calls_to("evaluate") == []


async def test_undisplay_unknown_expression_reports_it():
    ctrl, fake, _ = _make()
    result = await ctrl.evaluate_console("undisplay nope")
    assert "nope" in result
    assert ctrl.state.display == []


async def test_bare_display_lists_expressions():
    ctrl, fake, _ = _make()
    await ctrl.evaluate_console("display a")
    await ctrl.evaluate_console("display b")
    result = await ctrl.evaluate_console("display")
    assert result.split() == ["a", "b"]
    assert fake.calls_to("evaluate") == []


async def test_display_prefix_of_identifier_is_evaluated_normally():
    """`displayed` or `display_fn(x)` is an ordinary expression."""
    ctrl, fake, _ = _make()
    await ctrl.evaluate_console("displayed")
    await ctrl.evaluate_console("display_fn(1)")
    assert ctrl.state.display == []
    assert [c[1] for c in fake.calls_to("evaluate")] == ["displayed", "display_fn(1)"]


async def test_set_display_toggles_and_refreshes():
    """The Variables View click path: add then remove, refreshing scopes
    each time."""
    ctrl, fake, _ = _make()
    await ctrl.set_display("obj.count", True)
    assert ctrl.state.display == ["obj.count"]
    await ctrl.set_display("obj.count", False)
    assert ctrl.state.display == []
    assert fake.calls_to("scopes") == [("scopes", 11), ("scopes", 11)]

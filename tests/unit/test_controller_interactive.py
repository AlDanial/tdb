"""Interactive-variable tracking in DebugController.

`evaluate_console` is the Evaluate console's entry point: it evaluates
like `evaluate`, then records any variable the expression created (per
the profile's `interactive_variable` capability) and refreshes the
frame's scopes. `fetch_scopes_and_variables` renders tracked names as
an extra "Interactive" scope by evaluating each read-back expression.
"""

from __future__ import annotations

from tdb.dap.types import Scope, Variable
from tdb.languages import registry
from tdb.languages.base import InteractiveVariable
from tdb.session.state import INTERACTIVE_SCOPE_REF

from tests.unit.test_controller_actions import _make


async def test_evaluate_console_tracks_created_variable():
    ctrl, fake, _ = _make()
    assert await ctrl.evaluate_console("newvar = 42") == "42"
    assert ctrl.state.interactive == [InteractiveVariable("newvar", "newvar")]


async def test_evaluate_console_ignores_non_assignments_and_duplicates():
    ctrl, fake, _ = _make()
    await ctrl.evaluate_console("newvar = 1")
    await ctrl.evaluate_console("newvar = 2")
    await ctrl.evaluate_console("print(newvar)")
    assert ctrl.state.interactive == [InteractiveVariable("newvar", "newvar")]


async def test_evaluate_console_does_not_track_failed_evaluate():
    ctrl, fake, _ = _make()
    fake.evaluate_effects.append(RuntimeError("SyntaxError"))
    fake.evaluate_raw_effects.append(RuntimeError("NameError: newvar"))
    assert await ctrl.evaluate_console("newvar = ") == "SyntaxError"
    assert ctrl.state.interactive == []
    assert fake.calls_to("scopes") == []


async def test_evaluate_console_tracks_when_errored_assignment_reads_back():
    """lldb-dap answers `int $x = 42` with an error yet creates $x. A
    matched assignment whose evaluate failed is kept if its read-back
    expression resolves afterwards."""
    ctrl, fake, _ = _make(profile=registry.resolve("cpp", adapter="lldb-dap"))
    fake.evaluate_effects.append(RuntimeError("DAP evaluate failed: unknown error"))
    fake.evaluate_raw_effects.append({"result": "42", "variablesReference": 0})
    await ctrl.evaluate_console("int $newvar = 42")
    assert ctrl.state.interactive == [InteractiveVariable("$newvar", "$newvar")]
    assert fake.calls_to("scopes") == [("scopes", 11)]


async def test_evaluate_console_without_capability_tracks_nothing():
    ctrl, fake, _ = _make(profile=registry.resolve("go"))
    await ctrl.evaluate_console("x = 1")
    assert ctrl.state.interactive == []
    # The scope refresh still happens: evaluate can mutate existing
    # variables even where it cannot create them.
    assert fake.calls_to("scopes") == [("scopes", 11)]
    assert all(s.name != "Interactive" for s in ctrl.state.scopes)


async def test_evaluate_console_refreshes_scopes_after_success():
    ctrl, fake, _ = _make()
    await ctrl.evaluate_console("newvar = 42")
    assert fake.calls_to("scopes") == [("scopes", 11)]
    assert any(s.name == "Interactive" for s in ctrl.state.scopes)


async def test_evaluate_console_skips_refresh_on_synthetic_frames():
    ctrl, fake, _ = _make()
    ctrl.state.set_stack(list(fake.frames_result), synthetic=True)
    await ctrl.evaluate_console("newvar = 42")
    assert fake.calls_to("scopes") == []


async def test_fetch_scopes_appends_interactive_scope_with_read_back_values():
    ctrl, fake, _ = _make()
    ctrl.state.interactive.append(InteractiveVariable("$newvar", "$newvar"))
    fake.evaluate_raw_effects.append(
        {"result": "42", "type": "int", "variablesReference": 0}
    )
    await ctrl.fetch_scopes_and_variables(11)
    assert ctrl.state.scopes[-1] == Scope(
        name="Interactive", variables_reference=INTERACTIVE_SCOPE_REF
    )
    assert ctrl.state.variables[INTERACTIVE_SCOPE_REF] == [
        Variable(
            name="$newvar",
            value="42",
            type="int",
            variables_reference=0,
            evaluate_name="$newvar",
        )
    ]
    assert fake.calls_to("evaluate_raw") == [("evaluate_raw", "$newvar", 11, "watch")]


async def test_fetch_scopes_keeps_container_reference_for_expansion():
    ctrl, fake, _ = _make()
    ctrl.state.interactive.append(InteractiveVariable("d", "d"))
    fake.evaluate_raw_effects.append(
        {"result": "{...}", "type": "dict", "variablesReference": 77}
    )
    await ctrl.fetch_scopes_and_variables(11)
    assert ctrl.state.variables[INTERACTIVE_SCOPE_REF][0].variables_reference == 77


async def test_fetch_scopes_marks_unresolvable_name_unavailable():
    ctrl, fake, _ = _make()
    ctrl.state.interactive.append(InteractiveVariable("gone", "gone"))
    fake.evaluate_raw_effects.append(RuntimeError("NameError: gone"))
    await ctrl.fetch_scopes_and_variables(11)
    assert ctrl.state.variables[INTERACTIVE_SCOPE_REF] == [
        Variable(name="gone", value="<unavailable>", variables_reference=0)
    ]


async def test_fetch_scopes_without_tracked_names_adds_no_scope():
    ctrl, fake, _ = _make()
    await ctrl.fetch_scopes_and_variables(11)
    assert [s.name for s in ctrl.state.scopes] == ["Locals"]
    assert INTERACTIVE_SCOPE_REF not in ctrl.state.variables
    assert fake.calls_to("evaluate_raw") == []

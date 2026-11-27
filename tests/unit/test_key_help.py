"""`K`: a modal of every key the focused view accepts."""

from __future__ import annotations

import pytest
from textual.widgets import Input

from tdb.keybindings import KeybindingConfig, Mode
from tdb.widgets.key_help import KeyHelpModal, collect_key_help

ESC_ENTRY = ("Esc", "Next mode: Debug → Navigate → Edit")


@pytest.fixture
def tdb_app(monkeypatch):
    from tdb.app import TdbApp
    from tdb.persist import TdbConfig
    from tdb.session.controller import DebugController

    async def no_op(self, *args, **kwargs):
        pass

    monkeypatch.setattr(DebugController, "start", no_op)
    monkeypatch.setattr(DebugController, "stop", no_op)
    return TdbApp(program="", config=TdbConfig())


def _code_view(app):
    from tdb.widgets.code_view import CodeView

    return app.query_one("#code-view", CodeView)


async def _focus(app, pilot, view: str) -> None:
    getattr(app, f"action_focus_{view}")()
    await pilot.pause()


async def test_k_in_code_view_debug_lists_full_debug_table(tdb_app):
    app = tdb_app
    async with app.run_test(size=(120, 40)) as pilot:
        await _focus(app, pilot, "code")
        code_view = _code_view(app)
        code_view.mode = Mode.DEBUG
        help = collect_key_help(app.screen)
        assert help.view == code_view.key_help_entries()
        assert help.view == [ESC_ENTRY] + code_view.keybindings.format_bindings(
            Mode.DEBUG
        )
        assert help.title.startswith("Keys: Code")
        await pilot.press("K")
        await pilot.pause()
        assert isinstance(app.screen, KeyHelpModal)
        text = app.screen.render_text()
        assert "Step over" in text
        assert "footer_hint" not in text


async def test_k_in_code_view_navigation_lists_navigation_table(tdb_app):
    app = tdb_app
    async with app.run_test(size=(120, 40)) as pilot:
        await _focus(app, pilot, "code")
        code_view = _code_view(app)
        code_view.mode = Mode.NAVIGATION
        help = collect_key_help(app.screen)
        assert help.view[0] == ESC_ENTRY
        assert help.view[1:] == code_view.keybindings.format_bindings(Mode.NAVIGATION)


async def test_k_lists_emacs_table_for_emacs_scheme(tdb_app):
    app = tdb_app
    async with app.run_test(size=(120, 40)) as pilot:
        await _focus(app, pilot, "code")
        code_view = _code_view(app)
        code_view.keybindings = KeybindingConfig.from_scheme("emacs")
        code_view.mode = Mode.NAVIGATION
        help = collect_key_help(app.screen)
        assert help.view[1:] == KeybindingConfig.from_scheme("emacs").format_bindings(
            Mode.NAVIGATION
        )


async def test_no_modal_in_code_view_edit_mode(tdb_app):
    app = tdb_app
    async with app.run_test(size=(120, 40)) as pilot:
        await _focus(app, pilot, "code")
        _code_view(app).mode = Mode.EDIT
        await pilot.press("K")
        await pilot.pause()
        assert not isinstance(app.screen, KeyHelpModal)


async def test_k_in_breakpoints_lists_its_keys(tdb_app):
    app = tdb_app
    async with app.run_test(size=(120, 40)) as pilot:
        await _focus(app, pilot, "breakpoints")
        help = collect_key_help(app.screen)
        view = [desc for _, desc in help.view]
        everywhere = [desc for _, desc in help.everywhere]
        for desc in ("Enable/Disable", "Clear All", "Disable All"):
            assert desc in view
        assert "Quit" in everywhere
        assert help.title == "Keys: Breakpoints"
        await pilot.press("K")
        await pilot.pause()
        assert isinstance(app.screen, KeyHelpModal)


async def test_k_typed_in_evaluate_console(tdb_app):
    app = tdb_app
    async with app.run_test(size=(120, 40)) as pilot:
        await _focus(app, pilot, "eval")
        await pilot.press("K")
        await pilot.pause()
        assert not isinstance(app.screen, KeyHelpModal)
        assert isinstance(app.focused, Input)
        assert app.focused.value == "K"


@pytest.mark.parametrize(
    "view", ["code", "console", "variables", "stack", "breakpoints"]
)
async def test_every_tdb_binding_has_a_description(tdb_app, view):
    app = tdb_app
    async with app.run_test(size=(120, 40)) as pilot:
        await _focus(app, pilot, view)
        missing = [
            f"{type(active.node).__name__}: {key} -> {active.binding.action}"
            for key, active in app.screen.active_bindings.items()
            if type(active.node).__module__.startswith("tdb.")
            and not active.binding.description
        ]
        assert missing == []


async def test_k_closes_key_help_and_survives_other_modals(tdb_app):
    app = tdb_app
    async with app.run_test(size=(120, 40)) as pilot:
        await _focus(app, pilot, "breakpoints")
        await pilot.press("K")
        await pilot.pause()
        assert isinstance(app.screen, KeyHelpModal)
        await pilot.press("K")
        await pilot.pause()
        assert not isinstance(app.screen, KeyHelpModal)
        app.action_keybindings()
        await pilot.pause()
        await pilot.press("K")
        await pilot.pause()
        assert app.is_running


async def test_collect_with_nothing_focused(tdb_app):
    app = tdb_app
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.screen.set_focus(None)
        await pilot.pause()
        help = collect_key_help(app.screen)
        assert help.view == []
        assert help.everywhere
        assert help.title == "Keys"

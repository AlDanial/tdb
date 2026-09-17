"""TdbApp side of Edit mode: pane title, breakpoint remap after save,
refusal flags for replay / post-mortem, and the unsaved-edits guard on
quit and restart (Task 9 appends to this file)."""

from __future__ import annotations

from pathlib import Path

from tdb.app import TdbApp
from tdb.dap.types import SourceBreakpoint
from tdb.persist import TdbConfig
from tdb.widgets.code_editor import CodeEditor
from tdb.widgets.code_view import CodeView


def _write(tmp_path: Path, text: str = "x = 1\ny = 2\nz = 3\n") -> str:
    p = tmp_path / "prog.py"
    p.write_text(text, encoding="utf-8")
    return str(p)


async def _enter_edit(app: TdbApp, pilot, path: str) -> CodeView:
    cv = app.query_one("#code-view", CodeView)
    cv.load_file(path)
    cv.focus()
    await pilot.press("escape", "escape")
    await pilot.pause()
    assert cv.is_editing
    return cv


async def test_title_reflects_edit_state(tmp_path):
    app = TdbApp(program="", config=TdbConfig(keybindings="vim"))
    async with app.run_test() as pilot:
        await pilot.pause()
        cv = await _enter_edit(app, pilot, _write(tmp_path))
        assert "Edit" in cv.border_title and "*" not in cv.border_title
        await pilot.press("i", "q")
        await pilot.pause()
        assert "Edit:INSERT*" in cv.border_title
        await pilot.press("escape")
        await pilot.pause()
        assert "Edit*" in cv.border_title and "INSERT" not in cv.border_title


async def test_save_remaps_breakpoints_and_notifies(tmp_path):
    app = TdbApp(program="", config=TdbConfig(keybindings="default"))
    async with app.run_test() as pilot:
        await pilot.pause()
        path = _write(tmp_path)
        app.controller.state.breakpoints[path] = [
            SourceBreakpoint(line=2, condition="y"),
            SourceBreakpoint(line=3),
        ]
        sent: list[tuple[str, list[int]]] = []

        async def fake_replace(source_path, bps):
            app.controller.state.breakpoints[source_path] = list(bps)
            sent.append((source_path, [bp.line for bp in bps]))

        app.controller.replace_breakpoints = fake_replace
        notes: list[str] = []
        app.notify = lambda msg, **kw: notes.append(msg)

        cv = await _enter_edit(app, pilot, path)
        cv.current_line = 2
        ed = app.query_one(CodeEditor)
        ed.move_cursor((0, 0))
        await pilot.press("enter")  # insert a blank line above everything
        await pilot.press("ctrl+s")
        await pilot.pause()

        assert sent == [(path, [3, 4])]
        assert app.controller.state.breakpoints[path][0].condition == "y"
        assert cv.current_line is None
        assert any("Saved prog.py" in n for n in notes)


class _FakeReplayDriver:
    """on_mount schedules `replay_driver.run(app)` as a task; a no-op
    coroutine is enough to exercise the edit_enabled gate."""

    async def run(self, app):
        return None


async def test_replay_disables_editing():
    app = TdbApp(program="", config=TdbConfig(), replay_driver=_FakeReplayDriver())
    async with app.run_test() as pilot:
        await pilot.pause()
        cv = app.query_one("#code-view", CodeView)
        assert cv.edit_enabled is False
        assert cv.edit_refusal_reason() is not None

"""TdbApp side of Edit mode: pane title, breakpoint remap after save,
refusal flags for replay / post-mortem, and the unsaved-edits guard on
quit and restart (Task 9 appends to this file)."""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from pathlib import Path

from tdb.app import TdbApp
from tdb.dap.types import SourceBreakpoint
from tdb.persist import TdbConfig
from tdb.widgets.code_editor import CodeEditor, _UnsavedChangesModal
from tdb.widgets.code_view import CodeView
from tdb.widgets.modals import _QuitConfirmModal


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


# ---- Task 9: unsaved-edits guard ----


async def test_ctrl_q_with_dirty_buffer_prompts_and_cancel_aborts(tmp_path):
    app = TdbApp(program="", config=TdbConfig(keybindings="default"))
    async with app.run_test() as pilot:
        await pilot.pause()
        cv = await _enter_edit(app, pilot, _write(tmp_path))
        await pilot.press("z")
        exits: list[str] = []
        app.exit = lambda *a, **kw: exits.append("exit")
        await pilot.press("ctrl+q")
        await pilot.pause()
        assert isinstance(app.screen, _UnsavedChangesModal)
        await pilot.press("escape")
        await pilot.pause()
        assert exits == [] and cv.is_editing and cv.is_dirty
        assert app._is_quitting is False


async def test_ctrl_q_with_dirty_buffer_save_then_quits(tmp_path):
    app = TdbApp(program="", config=TdbConfig(keybindings="default"))
    async with app.run_test() as pilot:
        await pilot.pause()
        path = _write(tmp_path)
        await _enter_edit(app, pilot, path)
        await pilot.press("z")
        exits: list[str] = []
        app.exit = lambda *a, **kw: exits.append("exit")

        async def fake_stop():
            return None

        app.controller.stop = fake_stop
        await pilot.press("ctrl+q")
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        await pilot.pause()
        assert exits == ["exit"]
        assert Path(path).read_text(encoding="utf-8").startswith("z")


async def test_restart_with_dirty_buffer_prompts_and_discard_proceeds(
    tmp_path, monkeypatch
):
    app = TdbApp(program="", config=TdbConfig(keybindings="default"))
    async with app.run_test() as pilot:
        await pilot.pause()
        path = _write(tmp_path)
        cv = await _enter_edit(app, pilot, path)
        await pilot.press("z")
        monkeypatch.setattr(
            type(app.controller), "supports_restart", property(lambda self: True)
        )
        recorded: list[str] = []
        app.recorder.record = lambda name, args: recorded.append(name)

        async def fake_restart_body(*a, **kw):
            recorded.append("body")

        # Stop the real relaunch: patch what runs after the guard.
        monkeypatch.setattr(app, "_start_session", lambda *a, **kw: None)
        app._restart_session()
        await pilot.pause()
        assert isinstance(app.screen, _UnsavedChangesModal)
        await pilot.press("d")
        await pilot.pause()
        await pilot.pause()
        assert not cv.is_editing
        assert "restart" in recorded
        assert Path(path).read_text(encoding="utf-8") == "x = 1\ny = 2\nz = 3\n"


async def test_double_ctrl_q_does_not_stack_prompts(tmp_path):
    app = TdbApp(program="", config=TdbConfig(keybindings="default"))
    async with app.run_test() as pilot:
        await pilot.pause()
        cv = await _enter_edit(app, pilot, _write(tmp_path))
        await pilot.press("z")
        exits: list[str] = []
        app.exit = lambda *a, **kw: exits.append("exit")
        await pilot.press("ctrl+q")
        await pilot.pause()
        await pilot.press("ctrl+q")
        await pilot.pause()
        assert sum(isinstance(s, _UnsavedChangesModal) for s in app.screen_stack) == 1
        await pilot.press("escape")
        await pilot.pause()
        assert exits == [] and cv.is_editing


async def test_q_during_unsaved_prompt_does_not_push_confirm(tmp_path):
    app = TdbApp(program="", config=TdbConfig(keybindings="default"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _enter_edit(app, pilot, _write(tmp_path))
        await pilot.press("z")
        await pilot.press("ctrl+q")
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
        assert isinstance(app.screen, _UnsavedChangesModal)
        assert not any(isinstance(s, _QuitConfirmModal) for s in app.screen_stack)


# ---- Task 10: Edit menu + $EDITOR ----


async def test_edit_menu_exists_and_alt_e_opens_it():
    app = TdbApp(program="", config=TdbConfig())
    async with app.run_test() as pilot:
        await pilot.pause()
        from tdb.widgets.menu_bar import MenuBar

        bar = app.query_one("#menu-bar", MenuBar)
        assert "Edit" in bar._menus
        assert bar._menus["Edit"] == [
            "Save",
            "Revert to Disk",
            "Discard and Exit Edit Mode",
            "Open in $EDITOR",
        ]
        await pilot.press("alt+e")
        await pilot.pause()
        assert bar._open_menu == "Edit"


async def test_edit_menu_save_and_discard(tmp_path):
    app = TdbApp(program="", config=TdbConfig(keybindings="default"))
    async with app.run_test() as pilot:
        await pilot.pause()
        path = _write(tmp_path)
        notes: list[str] = []
        app.notify = lambda msg, **kw: notes.append(msg)
        app._edit_menu_action("Save")
        assert notes[-1].startswith("Nothing to save")
        cv = await _enter_edit(app, pilot, path)
        await pilot.press("z")
        app._edit_menu_action("Save")
        await pilot.pause()
        assert Path(path).read_text(encoding="utf-8").startswith("z")
        await pilot.press("q")
        app._edit_menu_action("Revert to Disk")
        assert not cv.is_dirty and cv.is_editing
        await pilot.press("q")
        app._edit_menu_action("Discard and Exit Edit Mode")
        await pilot.pause()
        assert not cv.is_editing
        assert Path(path).read_text(encoding="utf-8") == "zx = 1\ny = 2\nz = 3\n"


async def test_open_in_external_editor_reloads_and_remaps(tmp_path, monkeypatch):
    app = TdbApp(program="", config=TdbConfig(keybindings="default"))
    async with app.run_test() as pilot:
        await pilot.pause()
        path = _write(tmp_path)
        cv = app.query_one("#code-view", CodeView)
        cv.load_file(path)
        app.controller.state.breakpoints[path] = [SourceBreakpoint(line=2)]

        async def fake_replace(source_path, bps):
            app.controller.state.breakpoints[source_path] = list(bps)

        app.controller.replace_breakpoints = fake_replace
        monkeypatch.setenv("EDITOR", "fake-editor")

        @contextmanager
        def fake_suspend():
            yield

        monkeypatch.setattr(app, "suspend", fake_suspend)
        calls: list[list[str]] = []

        def fake_run(argv, **kw):
            calls.append(list(argv))
            Path(path).write_text("# new\nx = 1\ny = 2\nz = 3\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        await app._open_in_external_editor()
        await pilot.pause()
        assert calls == [["fake-editor", path]]
        assert cv.lines()[0] == "# new"
        assert [bp.line for bp in app.controller.state.breakpoints[path]] == [3]

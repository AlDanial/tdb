import pytest
from textual.app import App
from textual.widgets import Tree

from tdb.app import TdbApp
from tdb.persist import TdbConfig
from tdb.session.controller import DebugController
from tdb.widgets.code_editor import CodeEditor, _UnsavedChangesModal
from tdb.widgets.code_view import CodeView
from tdb.widgets.source_modal import SourceModal


class PickerApp(App):
    def __init__(self, paths, cwd):
        super().__init__()
        self.paths, self.cwd = paths, cwd
        self.result = "pending"

    def on_mount(self):
        self.push_screen(SourceModal(self.paths, self.cwd), self.selected)

    def selected(self, result):
        self.result = result


async def test_tree_expanded_navigation_collapse_and_select(tmp_path):
    paths = [
        tmp_path / "pkg" / "two.py",
        tmp_path / "pkg" / "one.py",
        tmp_path / "main.py",
    ]
    app = PickerApp(paths, tmp_path)
    async with app.run_test() as pilot:
        tree = app.screen.query_one(Tree)
        assert tree.root.is_expanded
        branch, leaf = tree.root.children
        assert str(branch.label) == "pkg" and branch.is_expanded
        assert str(leaf.label) == "main.py"
        assert [str(n.label) for n in branch.children] == ["one.py", "two.py"]
        await pilot.press("j")
        assert tree.cursor_node == branch
        # Clicking the directory label (not just its disclosure icon) toggles it.
        await pilot.click(Tree, offset=(8, 1))
        assert not branch.is_expanded
        await pilot.press("enter", "down", "j", "k")
        assert tree.cursor_node == branch.children[0]
        await pilot.press("enter")
        assert app.result == str(tmp_path / "pkg" / "one.py")


async def test_external_paths_and_cancel(tmp_path):
    app = PickerApp(
        [tmp_path / "a" / "same.py", tmp_path / "b" / "same.py"], tmp_path / "a"
    )
    async with app.run_test() as pilot:
        tree = app.screen.query_one(Tree)
        assert str(tree.root.label) == str(tmp_path)
        assert [str(n.label) for n in tree.root.children] == ["a", "b"]
        await pilot.press("escape")
        assert app.result is None


async def test_empty_picker(tmp_path):
    app = PickerApp([], tmp_path)
    async with app.run_test() as pilot:
        assert app.screen.query_one("#source-empty").display
        await pilot.press("enter")
        assert app.result == "pending"
        await pilot.press("escape")
        assert app.result is None


@pytest.fixture
def isolated_debugger(monkeypatch):
    async def no_op(self, *args, **kwargs):
        pass

    monkeypatch.setattr(DebugController, "start", no_op)
    monkeypatch.setattr(DebugController, "stop", no_op)


async def test_source_action_browses_without_restart_and_refreshes(
    tmp_path, isolated_debugger, monkeypatch
):
    main = tmp_path / "main.py"
    main.write_text("import other\n")
    other = tmp_path / "other.py"
    other.write_text("value = 1\n")
    app = TdbApp(program=str(main), config=TdbConfig())
    async with app.run_test() as pilot:
        controller = app.controller
        monkeypatch.setattr(
            type(controller), "supports_restart", property(lambda self: False)
        )
        state = controller.state
        before = (state.phase, state.current_frame_id)
        labels = list(app.query(".action-label"))
        assert [label.id for label in labels[:2]] == ["open-file-label", "source-label"]
        await pilot.press("alt+s")
        await pilot.pause()
        assert isinstance(app.screen, SourceModal)
        await pilot.press("down", "down", "enter")
        await pilot.pause()
        cv = app.query_one(CodeView)
        assert cv.source_path == str(other)
        assert cv.lines() == ["value = 1"]
        assert app._program == str(main) and app.controller is controller
        assert (state.phase, state.current_frame_id) == before
        assert cv.current_line is None
        main.write_text("import other, new\n")
        (tmp_path / "new.py").write_text("")
        await pilot.click("#source-label")
        await pilot.pause()
        tree = app.screen.query_one(Tree)
        assert {str(n.label) for n in tree.root.children} == {
            "main.py",
            "new.py",
            "other.py",
        }
        await pilot.press("escape")
        assert cv.source_path == str(other)


async def test_cancel_unsaved_edits_preserves_buffer(tmp_path, isolated_debugger):
    main = tmp_path / "main.py"
    main.write_text("value = 1\n")
    other = tmp_path / "other.py"
    other.write_text("other = 2\n")
    app = TdbApp(
        program=str(main), source_files=[other], config=TdbConfig(keybindings="vim")
    )
    async with app.run_test() as pilot:
        cv = app.query_one(CodeView)
        cv.focus()
        await pilot.press("escape", "escape", "i", "x", "escape")
        assert cv.is_dirty
        await pilot.press("alt+s")
        await pilot.pause()
        await pilot.press("down", "down", "enter")
        await pilot.pause()
        assert isinstance(app.screen, _UnsavedChangesModal)
        await pilot.press("escape")
        await pilot.pause()
        assert cv.source_path == str(main) and cv.is_dirty
        edited = app.query_one(CodeEditor).text
        cv.discard_edits()
        await pilot.pause()
        assert edited.startswith("x")


@pytest.mark.parametrize("scheme", ["default", "emacs", "vim"])
async def test_alt_s_opens_picker_while_editing(tmp_path, isolated_debugger, scheme):
    main = tmp_path / "main.py"
    main.write_text("value = 1\n")
    app = TdbApp(program=str(main), config=TdbConfig(keybindings=scheme))
    async with app.run_test() as pilot:
        cv = app.query_one(CodeView)
        await cv.enter_edit_mode()
        await pilot.pause()
        if scheme == "vim":
            await pilot.press("i")
        await pilot.press("alt+s")
        await pilot.pause()
        opened = isinstance(app.screen, SourceModal)
        if opened:
            await pilot.press("escape")
        cv.discard_edits()
        await pilot.pause()
        assert opened
        assert main.read_text() == "value = 1\n"


async def test_paths_without_common_ancestor_have_separate_roots():
    # PureWindowsPath allows exercising cross-drive tree construction on Unix.
    from pathlib import PureWindowsPath

    paths = [
        PureWindowsPath("C:/project/main.py"),
        PureWindowsPath("D:/shared/extra.py"),
    ]
    app = PickerApp(paths, PureWindowsPath("C:/project"))
    async with app.run_test() as pilot:
        tree = app.screen.query_one(Tree)
        assert [str(n.label) for n in tree.root.children] == ["C:\\", "D:\\"]
        leaf = tree.root.children[1].children[0].children[0]
        tree.move_cursor(leaf)
        await pilot.press("enter")
        assert app.result == str(paths[1])


@pytest.mark.parametrize("choice", ["s", "d"])
async def test_source_navigation_saves_or_discards_edits(
    tmp_path, isolated_debugger, choice
):
    main = tmp_path / "main.py"
    main.write_text("value = 1\n")
    other = tmp_path / "other.py"
    other.write_text("other = 2\n")
    app = TdbApp(
        program=str(main), source_files=[other], config=TdbConfig(keybindings="vim")
    )
    async with app.run_test() as pilot:
        cv = app.query_one(CodeView)
        await cv.enter_edit_mode()
        await pilot.pause()
        await pilot.press("i", "x", "escape", "alt+s")
        await pilot.pause()
        await pilot.press("down", "down", "enter")
        await pilot.pause()
        assert isinstance(app.screen, _UnsavedChangesModal)
        await pilot.press(choice)
        await pilot.pause()
        assert cv.source_path == str(other)
        assert cv.lines() == ["other = 2"]
        assert not cv.is_editing
        assert main.read_text() == ("xvalue = 1\n" if choice == "s" else "value = 1\n")
        assert app._program == str(main)

import pytest
from textual.app import App
from textual.widgets import OptionList

from tdb.app import TdbApp
from tdb.persist import TdbConfig
from tdb.session.controller import DebugController
from tdb.widgets.code_editor import CodeEditor, _UnsavedChangesModal
from tdb.widgets.code_view import CodeView
from tdb.widgets.source_modal import SourceModal


class PickerApp(App):
    def __init__(self, paths, cwd, program=None, **modal_options):
        super().__init__()
        self.paths, self.cwd, self.program = paths, cwd, program
        self.result = "pending"
        self.modal_options = modal_options

    def on_mount(self):
        self.push_screen(
            SourceModal(
                self.paths, self.cwd, program=self.program, **self.modal_options
            ),
            self.selected,
        )

    def selected(self, result):
        self.result = result


async def test_flat_list_debuggee_first_navigation_and_select(tmp_path):
    main = tmp_path / "zmain.py"
    paths = [
        tmp_path / "pkg" / "two.py",
        main,
        tmp_path / "pkg" / "one.py",
        tmp_path / "channels.py",
        main,
    ]
    app = PickerApp(paths, tmp_path, program=main)
    async with app.run_test() as pilot:
        files = app.screen.query_one(OptionList)
        assert [
            str(files.get_option_at_index(i).prompt) for i in range(files.option_count)
        ] == ["./zmain.py", "./channels.py", "./pkg/one.py", "./pkg/two.py"]
        assert files.highlighted == 0
        await pilot.press("j", "down", "up", "k")
        assert files.highlighted == 0
        await pilot.press("down", "j", "enter")
        assert app.result == str(tmp_path / "pkg" / "one.py")


async def test_external_paths_and_cancel(tmp_path):
    app = PickerApp(
        [tmp_path / "a" / "same.py", tmp_path / "b" / "same.py"], tmp_path / "a"
    )
    async with app.run_test() as pilot:
        files = app.screen.query_one(OptionList)
        assert {
            str(files.get_option_at_index(i).prompt) for i in range(files.option_count)
        } == {"./same.py", str(tmp_path / "b" / "same.py")}
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
    monkeypatch.chdir(tmp_path)
    main = tmp_path / "zmain.py"
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
        await pilot.press("down", "enter")
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
        files = app.screen.query_one(OptionList)
        assert [
            str(files.get_option_at_index(i).prompt) for i in range(files.option_count)
        ] == ["./zmain.py", "./new.py", "./other.py"]
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
        await pilot.press("down", "enter")
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


async def test_paths_on_different_drives_are_selectable():
    # PureWindowsPath allows exercising cross-drive path display on Unix.
    from pathlib import PureWindowsPath

    paths = [
        PureWindowsPath("C:/project/main.py"),
        PureWindowsPath("D:/shared/extra.py"),
    ]
    app = PickerApp(paths, PureWindowsPath("C:/project"))
    async with app.run_test() as pilot:
        files = app.screen.query_one(OptionList)
        assert [
            str(files.get_option_at_index(i).prompt) for i in range(files.option_count)
        ] == ["./main.py", str(paths[1])]
        await pilot.press("down", "enter")
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
        await pilot.press("down", "enter")
        await pilot.pause()
        assert isinstance(app.screen, _UnsavedChangesModal)
        await pilot.press(choice)
        await pilot.pause()
        assert cv.source_path == str(other)
        assert cv.lines() == ["other = 2"]
        assert not cv.is_editing
        assert main.read_text() == ("xvalue = 1\n" if choice == "s" else "value = 1\n")
        assert app._program == str(main)


async def test_remove_cursor_empty_and_save_default(tmp_path):
    from tdb.source_search import SourceSelection

    first, second = tmp_path / "a.py", tmp_path / "b.py"
    first.touch()
    second.touch()
    listing = tmp_path / "sources.txt"
    selection = SourceSelection(filename=listing)
    app = PickerApp([first, second], tmp_path, selection=selection)
    async with app.run_test() as pilot:
        files = app.screen.query_one(OptionList)
        await pilot.press("down", "x")
        assert files.option_count == 1 and files.highlighted == 0
        assert second.exists() and selection.excluded == {second}
        await pilot.press("s")
        await app.workers.wait_for_complete()
        assert listing.read_text() == str(first) + "\n"
        await pilot.press("x", "s")
        await app.workers.wait_for_complete()
        assert listing.read_text() == ""
        assert app.screen.query_one("#source-empty").display
        assert first.exists()


async def test_dynamic_search_repeat_respects_exclusions(tmp_path):
    from tdb.source_search import SourceSelection

    main, external = tmp_path / "main.py", tmp_path / "external.py"
    selection = SourceSelection()
    app = PickerApp(
        [main], tmp_path, selection=selection, dynamic_search=lambda: [external]
    )
    async with app.run_test() as pilot:
        await pilot.press("d")
        await app.workers.wait_for_complete()
        assert app.screen._paths == [external, main]
        await pilot.press("home", "x", "d")
        await app.workers.wait_for_complete()
        assert app.screen._paths == [main]
        await pilot.press("escape")
        app.push_screen(SourceModal([main], tmp_path, selection=selection))
        await pilot.pause()
        assert app.screen._paths == [main]


async def test_save_prompt_cancel_filename_and_error(tmp_path):
    from textual.widgets import Input

    source = tmp_path / "a.py"
    app = PickerApp([source], tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("s")
        entry = app.screen.query_one(Input)
        assert entry.has_focus
        # These letters are ordinary filename input, not modal actions.
        await pilot.press("d", "x", "s", "q")
        assert entry.value == "dxsq"
        assert app.screen._paths == [source]
        await pilot.press("escape")
        assert isinstance(app.screen, SourceModal)
        assert not entry.display
        await pilot.press("s")
        entry.value = str(tmp_path / "missing" / "sources.txt")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        assert "Cannot save" in str(app.screen.query_one("#source-status").render())
        assert entry.display
        entry.value = str(tmp_path / "sources.txt")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        assert (tmp_path / "sources.txt").read_text() == str(source) + "\n"
        assert not entry.display


async def test_dynamic_failure_preserves_list(tmp_path):
    def fail():
        raise OSError("interpreter missing")

    main = tmp_path / "main.py"
    app = PickerApp([main], tmp_path, dynamic_search=fail)
    async with app.run_test() as pilot:
        await pilot.press("d")
        await app.workers.wait_for_complete()
        assert app.screen._paths == [main]
        assert "interpreter missing" in str(
            app.screen.query_one("#source-status").render()
        )


async def test_close_during_dynamic_search(tmp_path):
    import threading

    entered, release = threading.Event(), threading.Event()

    def search():
        entered.set()
        release.wait(5)
        return [tmp_path / "found.py"]

    app = PickerApp([], tmp_path, dynamic_search=search)
    try:
        async with app.run_test() as pilot:
            await pilot.press("d")
            assert entered.is_set()
            await pilot.press("escape")
            release.set()
            await pilot.pause()
            assert app.result is None
    finally:
        release.set()


async def test_app_retains_removed_entries_across_static_refresh(
    tmp_path, isolated_debugger
):
    main = tmp_path / "main.py"
    main.write_text("import other")
    other = tmp_path / "other.py"
    other.touch()
    app = TdbApp(program=str(main), config=TdbConfig())
    async with app.run_test() as pilot:
        await pilot.press("alt+s")
        await pilot.pause()
        await pilot.press("down", "x", "escape", "alt+s")
        await pilot.pause()
        assert app.screen._paths == [main]
        assert other.exists()


async def test_empty_dynamic_results_can_be_opened(tmp_path):
    source = tmp_path / "found.py"
    app = PickerApp([], tmp_path, dynamic_search=lambda: [source])
    async with app.run_test() as pilot:
        await pilot.press("d")
        await app.workers.wait_for_complete()
        await pilot.press("enter")
        assert app.result == str(source)


async def test_non_python_discovery_reports_unavailable(tmp_path):
    source = tmp_path / "main.c"
    app = PickerApp([source], tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("d")
        assert "only for Python" in str(app.screen.query_one("#source-status").render())
        assert app.screen._paths == [source]


async def test_save_finishes_after_modal_close_and_serializes_reopened_save(
    tmp_path, monkeypatch
):
    import threading

    from textual.widgets import Input

    from tdb.source_search import SourceSelection, write_source_list

    entered, release = threading.Event(), threading.Event()
    writes = []

    def slow_write(filename, paths):
        writes.append(list(paths))
        entered.set()
        release.wait(5)
        write_source_list(filename, paths)

    monkeypatch.setattr("tdb.widgets.source_modal.write_source_list", slow_write)
    source = tmp_path / "a.py"
    filename = tmp_path / "sources.txt"
    selection = SourceSelection()
    app = PickerApp([source], tmp_path, selection=selection)
    try:
        async with app.run_test() as pilot:
            await pilot.press("s")
            app.screen.query_one(Input).value = str(filename)
            await pilot.press("enter")
            assert entered.is_set()
            await pilot.press("escape", "escape")
            assert app.result is None
            app.push_screen(SourceModal([source], tmp_path, selection=selection))
            await pilot.pause()
            await pilot.press("x", "s")
            assert not app.screen.query_one(Input).display
            release.set()
            await app.workers.wait_for_complete()
            assert selection.filename == filename
            assert filename.read_text() == str(source) + "\n"
            await pilot.press("s")
            await app.workers.wait_for_complete()
            assert filename.read_text() == ""
            assert writes == [[source], []]
    finally:
        release.set()


async def test_app_dynamic_search_open_reopen_and_save(
    tmp_path, isolated_debugger, monkeypatch
):
    import sys

    main = tmp_path / "project/main.py"
    main.parent.mkdir()
    main.write_text("import dependency")
    external = tmp_path / "external/dependency.py"
    external.parent.mkdir()
    external.write_text("value = 42\n")
    monkeypatch.setenv("PYTHONPATH", str(external.parent))
    listing = tmp_path / "sources.txt"
    app = TdbApp(
        program=str(main),
        python=sys.executable,
        source_filename=str(listing),
        config=TdbConfig(),
    )
    async with app.run_test() as pilot:
        await pilot.press("alt+s")
        await pilot.pause()
        assert app.screen._paths == [main]
        await pilot.press("d")
        # The app's source-navigation worker waits for the picker to close;
        # wait only for the modal-owned discovery worker here.
        for worker in list(app.workers):
            if worker.group == "source-discovery":
                await worker.wait()
        assert app.screen._paths == [main, external]
        await pilot.press("down", "enter")
        await pilot.pause()
        assert app.query_one(CodeView).source_path == str(external)
        await pilot.press("alt+s")
        await pilot.pause()
        assert app.screen._paths == [main, external]
        await pilot.press("x", "s")
        for worker in list(app.workers):
            if worker.group == "source-save":
                await worker.wait()
        assert listing.read_text() == str(external) + "\n"

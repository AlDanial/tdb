"""Vim-lite key layer for CodeEditor: sub-modes, motions, insert
entry, and the ':' command line. Editing operators are in Task 7's
section at the bottom of this file."""

from __future__ import annotations

from textual.app import App

from tdb.widgets.code_editor import CodeEditor, VimLayer

TEXT = "one two three\n  four five\nsix\nseven eight\n"


class _EdApp(App):
    def __init__(self, text: str = TEXT):
        super().__init__()
        self._text = text
        self.leave: list[bool] = []
        self.saves = 0
        self.searches: list[bool] = []
        self.steps: list[bool] = []
        self.submodes: list[str] = []

    def compose(self):
        yield CodeEditor(self._text, scheme="vim", id="ed")

    def on_mount(self):
        ed = self.query_one("#ed", CodeEditor)
        ed._set_layer(VimLayer(ed))
        ed.focus()

    def on_code_editor_leave_requested(self, msg):
        self.leave.append(msg.discard)

    def on_code_editor_save_requested(self, msg):
        self.saves += 1

    def on_code_editor_search_requested(self, msg):
        self.searches.append(msg.backward)

    def on_code_editor_search_step_requested(self, msg):
        self.steps.append(msg.forward)

    def on_code_editor_submode_changed(self, msg):
        self.submodes.append(self.query_one("#ed", CodeEditor).submode)


async def test_starts_in_normal_mode_and_swallows_letters():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        assert ed.submode == "normal"
        await pilot.press("q", "z", "enter")
        assert ed.text == TEXT


async def test_hjkl_with_counts_and_arrows():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("3", "l")
        assert ed.cursor_location == (0, 3)
        await pilot.press("2", "j")
        assert ed.cursor_location[0] == 2
        await pilot.press("k", "h")
        assert ed.cursor_location == (1, 2)
        await pilot.press("down", "right")
        # From (1, 2): down preserves the goal column onto "six" -> (2, 2);
        # right then advances one column onto the end-of-line position,
        # which TextArea permits (col == len(line)) -> (2, 3). Verified
        # against plain TextArea (scheme="default", no layer) pressing the
        # same native "down"/"right" keys: identical (2, 3) result, so this
        # is stock TextArea cursor behavior, not a layer quirk.
        assert ed.cursor_location == (2, 3)


async def test_line_motions():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("j", "$")
        assert ed.cursor_location == (1, 11)
        await pilot.press("0")
        assert ed.cursor_location == (1, 0)
        await pilot.press("^")
        assert ed.cursor_location == (1, 2)


async def test_word_motions():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("w")
        assert ed.cursor_location == (0, 4)
        await pilot.press("w")
        assert ed.cursor_location == (0, 8)
        await pilot.press("b")
        assert ed.cursor_location == (0, 4)
        await pilot.press("e")
        assert ed.cursor_location == (0, 6)


async def test_gg_G_and_count_G():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("G")
        assert ed.cursor_location[0] == 4  # trailing empty line
        await pilot.press("g", "g")
        assert ed.cursor_location == (0, 0)
        await pilot.press("3", "G")
        assert ed.cursor_location == (2, 0)


async def test_insert_entry_points_and_escape_back_to_normal():
    app = _EdApp(text="abc\n")
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("i", "X", "escape")
        assert ed.text == "Xabc\n" and ed.submode == "normal"
        await pilot.press("0", "a", "Y", "escape")
        assert ed.text == "XYabc\n"
        await pilot.press("A", "Z", "escape")
        assert ed.text == "XYabcZ\n"
        await pilot.press("I", "W", "escape")
        assert ed.text == "WXYabcZ\n"
        # Pilot.press() sends a multi-char string as ONE named key, not as
        # separate characters (Textual App._press_keys: len(key) != 1 means
        # char=None, so nothing is typed) — "new" would be a no-op key and
        # "up" would fire the literal Up-arrow. Unpack to press each
        # character individually so this actually types the text.
        await pilot.press("o", *"new", "escape")
        assert ed.text == "WXYabcZ\nnew\n"
        await pilot.press("O", *"up", "escape")
        assert ed.text == "WXYabcZ\nup\nnew\n"
        assert app.leave == []  # Esc from insert never left Edit mode
        assert "insert" in app.submodes and app.submodes[-1] == "normal"


async def test_escape_in_normal_mode_requests_leave():
    app = _EdApp()
    async with app.run_test() as pilot:
        await pilot.press("escape")
        assert app.leave == [False]


async def test_command_line_w_q_wq_qbang_and_line_number():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press(":")
        assert ed.submode == "command"
        await pilot.press("w", "q")
        assert ed.command_text == "wq"
        await pilot.press("backspace")
        assert ed.command_text == "w"
        await pilot.press("enter")
        assert app.saves == 1 and app.leave == [] and ed.submode == "normal"
        await pilot.press(":", "q", "enter")
        assert app.leave == [False]
        await pilot.press(":", "q", "!", "enter")
        assert app.leave == [False, True]
        await pilot.press(":", "w", "q", "enter")
        assert app.saves == 2 and app.leave == [False, True, False]
        await pilot.press(":", "3", "enter")
        assert ed.cursor_location == (2, 0)
        await pilot.press(":", "x", "y", "escape")
        assert ed.submode == "normal" and ed.command_text == ""


async def test_unknown_command_notifies():
    app = _EdApp()
    async with app.run_test() as pilot:
        notes: list[str] = []
        app.notify = lambda msg, **kw: notes.append(msg)
        await pilot.press(":", "b", "o", "g", "u", "s", "enter")
        assert notes and "Unknown command" in notes[0]


async def test_search_keys_post_messages():
    app = _EdApp()
    async with app.run_test() as pilot:
        await pilot.press("/", "?", "n", "N")
        assert app.searches == [False, True]
        assert app.steps == [True, False]


async def test_page_keys():
    app = _EdApp(text="\n".join(f"l{i}" for i in range(200)) + "\n")
    async with app.run_test(size=(80, 24)) as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("ctrl+f")
        assert ed.cursor_location[0] > 5
        await pilot.press("ctrl+b")
        assert ed.cursor_location[0] == 0

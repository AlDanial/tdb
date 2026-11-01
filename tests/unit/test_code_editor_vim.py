"""Vim scheme for CodeEditor: ViLayer routes Textual keys into the headless
prompt_toolkit vi engine and mirrors the engine back into the TextArea.

Expected results follow real vim (nvim), not the old vim-lite layer."""

from __future__ import annotations

import time
from typing import ClassVar

from textual import events
from textual.app import App
from textual.binding import Binding, BindingType
from textual.events import Key

import tdb.vi.engine as vi_engine
from tdb.vi import ViSession
from tdb.widgets.code_editor import CodeEditor, ViLayer

TEXT = "one two three\n  four five\nsix\nseven eight\n"

PANE_KEYS = ("ctrl+o", "ctrl+e", "ctrl+v", "ctrl+b", "ctrl+c")


class _EdApp(App):
    BINDINGS: ClassVar[list[BindingType]] = [
        *(Binding(k, f"pane('{k}')", show=False) for k in PANE_KEYS),
        Binding("alt+e", "menu('edit')", show=False),
        Binding("ctrl+right", "menu('file')", show=False),
    ]

    def __init__(self, text: str = TEXT):
        super().__init__()
        self._text = text
        self.leave: list[bool] = []
        self.saves = 0
        self.submodes: list[str] = []
        self.panes: list[str] = []
        self.menus: list[str] = []

    def compose(self):
        yield CodeEditor(self._text, scheme="vim", id="ed")

    def on_mount(self):
        self.ed = ed = self.query_one("#ed", CodeEditor)
        ed._set_layer(ViLayer(ed, session=ViSession()))
        ed.focus()

    def action_pane(self, key: str) -> None:
        self.panes.append(key)

    def action_menu(self, name: str) -> None:
        self.menus.append(name)

    def on_code_editor_leave_requested(self, msg):
        self.leave.append(msg.discard)

    def on_code_editor_save_requested(self, msg):
        self.saves += 1

    def on_code_editor_submode_changed(self, msg):
        # Messages can arrive while run_test tears the editor down.
        self.submodes.append(self.ed.submode)


async def _send(pilot, key: str, character: str | None) -> None:
    """Deliver a Key exactly as a real terminal would (Pilot.press gives
    multi-character key names no character, e.g. alt+e arrives as
    character "e" from a terminal but None from Pilot)."""
    event = Key(key, character)
    event.set_sender(pilot.app)
    pilot.app._driver.send_message(event)
    await pilot.pause()


async def test_starts_in_normal_mode_and_swallows_letters():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        assert ed.submode == "normal"
        # "q z" would now start recording into register z (real vim).
        await pilot.press("Z", "enter")
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
        # vim normal mode never puts the cursor past the last character:
        # "six" -> (2, 2), and Right there is a no-op.
        assert ed.cursor_location == (2, 2)


async def test_line_motions():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("j", "$")
        assert ed.cursor_location == (1, 10)  # on the last char, as in vim
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
        assert ed.cursor_location[0] == 3  # last real line, not the phantom row
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
        # Pilot.press sends a multi-char string as ONE named key; unpack.
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
        assert ed.command_text == ":wq"
        await pilot.press("backspace")
        assert ed.command_text == ":w"
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
        assert app.leave == [False, True, False]  # Esc only cancelled the line


async def test_unknown_command_notifies():
    app = _EdApp()
    async with app.run_test() as pilot:
        notes: list[str] = []
        app.notify = lambda msg, **kw: notes.append(msg)
        await pilot.press(":", "b", "o", "g", "u", "s", "enter")
        assert notes and "Not an editor command" in notes[0]


async def test_x_and_X_with_count():
    app = _EdApp(text="abcdef\n")
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("l", "x")
        assert ed.text == "acdef\n"
        await pilot.press("2", "x")
        assert ed.text == "aef\n"
        await pilot.press("l", "X")
        assert ed.text == "af\n"


async def test_dd_yy_p_P_linewise_with_counts():
    app = _EdApp(text="a\nb\nc\nd\n")
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("j", "2", "d", "d")
        assert ed.text == "a\nd\n"
        assert ed.cursor_location[0] == 1
        await pilot.press("p")
        assert ed.text == "a\nd\nb\nc\n"
        # G lands on the last real line ("c"), so P puts "a" above it.
        await pilot.press("g", "g", "y", "y", "G", "P")
        assert ed.text == "a\nd\nb\na\nc\n"


async def test_dd_on_last_line_removes_preceding_newline():
    app = _EdApp(text="a\nb")
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("j", "d", "d")
        assert ed.text == "a"


async def test_dw_D_and_J():
    app = _EdApp(text="one two three\nfour\n")
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("d", "w")
        assert ed.text == "two three\nfour\n"
        await pilot.press("w", "D")
        assert ed.text == "two \nfour\n"
        # Not J straight away: prompt_toolkit's J adds a second space after
        # trailing white space (vim does not); join fixes are Task 7's.
        await pilot.press("x", "J")
        assert ed.text == "two four\n"


async def test_undo_redo():
    app = _EdApp(text="abc\n")
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("x")
        assert ed.text == "bc\n"
        await pilot.press("u")
        assert ed.text == "abc\n"
        await pilot.press("ctrl+r")
        assert ed.text == "bc\n"


async def test_charwise_put_after_dw():
    app = _EdApp(text="one two\n")
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("d", "w", "$", "p")
        assert ed.text == "twoone \n"


async def test_linewise_put_after_on_phantom_last_row():
    app = _EdApp(text="a\nb\nc\n")
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("g", "g", "y", "y", "G", "p")
        assert ed.text == "a\nb\nc\na\n"
        assert ed.cursor_location == (3, 0)


async def test_ctrl_p_not_claimed_from_app_on_vim_scheme():
    """check_consume_key must not strip the App's ctrl+p (command
    palette) priority binding for vim users."""
    app = _EdApp()
    async with app.run_test():
        ed = app.query_one("#ed", CodeEditor)
        assert ed._layer is not None
        assert ed.check_consume_key("ctrl+p") is False


async def test_ctrl_s_in_normal_mode_saves_instead_of_being_swallowed():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        assert ed.submode == "normal"
        await pilot.press("ctrl+s")
        assert app.saves == 1
        assert ed.text == TEXT


async def test_ctrl_q_in_normal_mode_bubbles_to_app():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        assert ed.submode == "normal"
        await pilot.press("ctrl+q")
        assert ed.text == TEXT
        assert app.leave == [] and app.saves == 0


async def test_ctrl_q_in_command_mode_bubbles_to_app():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press(":")
        assert ed.submode == "command"
        await pilot.press("ctrl+q")
        assert ed.submode == "command"  # key bubbled, command line untouched
        assert ed.command_text == ":"


async def test_charwise_put_places_cursor_on_pasted_text():
    app = _EdApp(text="ab\n")
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("x", "p")
        assert ed.text == "ba\n"
        assert ed.cursor_location == (0, 1)
        await pilot.press("x")
        assert ed.text == "b\n"


# ---- ViLayer: routing, Esc layering, mirroring ----


async def test_pane_keys_bubble_in_every_mode():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        layer = ed._layer
        for enter, mode in (
            ("", "normal"),
            ("i", "insert"),
            ("v", "visual"),
            (":", "command"),
        ):
            if enter:
                await pilot.press(enter)
            assert ed.submode == mode
            app.panes.clear()
            cursor = ed.cursor_location
            await pilot.press(*PANE_KEYS)
            assert app.panes == list(PANE_KEYS), mode
            assert ed.submode == mode and ed.text == TEXT
            assert ed.cursor_location == cursor
            assert layer.engine.text == TEXT
            if enter:
                await pilot.press("escape")
        assert app.leave == []


async def test_escape_cancels_pending_then_leaves():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("d")
        assert ed.command_text == "d"
        await pilot.press("escape")
        assert app.leave == [] and ed.command_text == ""
        await pilot.press("escape")
        assert app.leave == [False]


async def test_escape_in_insert_returns_to_normal_without_leave():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("i")
        assert ed.submode == "insert"
        await pilot.press("escape")
        assert ed.submode == "normal" and app.leave == []


async def test_alt_letter_in_insert_is_escape_then_letter():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("i", "x", "alt+j")
        assert ed.submode == "normal"
        assert ed.text == "x" + TEXT
        assert ed.cursor_location == (1, 0)
        assert app.menus == [] and app.leave == []
        # As a real terminal delivers it (with a character).
        await pilot.press("i")
        await _send(pilot, "alt+k", "k")
        assert ed.submode == "normal" and ed.cursor_location == (0, 0)
        assert ed.text == "x" + TEXT


async def test_alt_letter_in_normal_bubbles():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("alt+e")
        assert app.menus == ["edit"]
        await _send(pilot, "alt+e", "e")  # real terminals send character "e"
        assert app.menus == ["edit", "edit"]
        assert ed.text == TEXT and ed.submode == "normal"
        # Textual's rewrite of Alt+F is ctrl+right: the File menu key.
        await pilot.press("ctrl+right")
        assert app.menus == ["edit", "edit", "file"]
        assert ed.cursor_location == (0, 0)


async def test_ctrl_right_in_insert_is_escape_f():
    app = _EdApp(text="ab cd\n")
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("i", "ctrl+right")
        assert ed.submode == "normal" and ed.command_text == "f"
        await pilot.press("d")
        assert ed.cursor_location == (0, 4)
        await pilot.press("A", "ctrl+left")  # Esc b: back to the start of "cd"
        assert ed.submode == "normal" and ed.cursor_location == (0, 3)
        assert ed.text == "ab cd\n" and app.menus == []


async def test_mirroring_keeps_textarea_in_sync():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        engine = ed._layer.engine
        await pilot.press("w", "c", "i", "w", "Z", "escape")
        assert ed.text == engine.text == "one Z three\n  four five\nsix\nseven eight\n"
        assert ed.cursor_location == engine.cursor == (0, 4)
        await pilot.press("j", "d", "d", "p")
        assert ed.text == engine.text
        assert ed.cursor_location == engine.cursor


async def test_visual_selection_is_mirrored():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("v", "l")
        assert ed.selected_text == "on"
        await pilot.press("V")
        assert ed.selected_text == "one two three"
        await pilot.press("j")
        assert ed.selected_text == "one two three\n  four five"
        await pilot.press("escape")
        assert ed.selected_text == "" and ed.submode == "normal"


async def test_cursor_mirroring_with_tabs_and_wide_chars():
    app = _EdApp(text="\té漢😀x\n")
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("f", "x")
        assert ed._layer.engine.cursor == (0, 4)
        assert ed.cursor_location == (0, 4)


async def test_engine_exception_shows_warning_and_keeps_text(monkeypatch):
    from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings

    original = vi_engine.load_tdb_vi_bindings

    def with_boom(engine):
        kb = KeyBindings()

        @kb.add("Q")
        def _(event):
            event.app.current_buffer.insert_text("JUNK")
            raise RuntimeError("boom")

        return merge_key_bindings([original(engine), kb])

    monkeypatch.setattr(vi_engine, "load_tdb_vi_bindings", with_boom)
    app = _EdApp()
    async with app.run_test() as pilot:
        notes: list[tuple[str, str]] = []
        app.notify = lambda msg, **kw: notes.append((msg, kw.get("severity")))
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("Q")
        assert ed.text == TEXT and ed.submode == "normal"
        assert len(notes) == 1 and notes[0][1] == "warning"
        await pilot.press("x")
        assert ed.text == TEXT[1:]


async def test_typing_latency_on_large_file():
    text = "".join(f"line {i} with some words here\n" for i in range(5000))
    keys = list("2500G") + list("ciwfoo") + ["escape"]
    keys += ["j", "w", "c", "i", "w", *"bar", "escape"] * 20
    keys += ["x", "j"] * 40
    keys += ["i", *"typed text ", "escape"] * 8
    keys = keys[:300]
    events = [
        Key("space", " ") if k == " " else Key(k, k if len(k) == 1 else None)
        for k in keys
    ]
    # 2 s leaves room for slow CI runners (Windows took 1.5 s with coverage
    # on); one retry, each from a fresh editor, as test_global_performance.
    for _ in range(2):
        app = _EdApp(text=text)
        async with app.run_test():
            ed = app.query_one("#ed", CodeEditor)
            layer = ed._layer
            start = time.perf_counter()
            for event in events:
                layer.handle_key(event)
            elapsed = time.perf_counter() - start
            assert ed.text == layer.engine.text
        if elapsed < 2.0:
            break
    assert elapsed < 2.0, f"{len(events)} keys took {elapsed:.2f}s"


async def test_ctrl_z_leaves_text_unchanged_in_normal_and_insert():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("l", "ctrl+z")
        assert ed.text == TEXT and ed.submode == "normal"
        await pilot.press("i", "ctrl+z")
        assert ed.text == TEXT and ed.submode == "insert"
        assert ed._layer.engine.text == TEXT


# ---------------------------------------------------------------------------
# Mouse, paste, PageUp/PageDown


def _cell(ed: CodeEditor, row: int, col: int) -> tuple[int, int]:
    """Pilot offset that lands on document (row, col) (Pilot is 1-based here)."""
    return ed.gutter_width + 1 + col, row + 1


async def test_click_moves_cursor_and_leaves_visual():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        engine = ed._layer.engine
        await pilot.press("v", "l", "l")
        assert engine.mode == "visual"
        await pilot.click(CodeEditor, offset=_cell(ed, 3, 4))
        assert engine.mode == "normal" and ed.submode == "normal"
        assert engine.cursor == ed.cursor_location == (3, 4)
        assert ed.selected_text == ""
        await pilot.press("x")  # the engine, not just the widget, moved
        assert ed.text.splitlines()[3] == "seve eight"


async def test_click_past_line_end_clamps_in_normal_mode():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.click(CodeEditor, offset=_cell(ed, 2, 30))
        assert ed._layer.engine.cursor == ed.cursor_location == (2, 2)


async def test_drag_starts_visual():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.mouse_down(CodeEditor, offset=_cell(ed, 0, 4))
        await pilot.hover(CodeEditor, offset=_cell(ed, 0, 6))
        await pilot.mouse_up(CodeEditor, offset=_cell(ed, 0, 6))
        engine = ed._layer.engine
        assert engine.mode == "visual" and ed.submode == "visual"
        assert engine.selection == ((0, 4), (0, 6))
        assert ed.selected_text == "two"  # vim includes the end character
        await pilot.press("d")
        assert ed.text.startswith("one  three\n") and ed.text == engine.text


async def test_paste_is_one_undo_step_any_mode():
    app = _EdApp(text="ab\ncd\n")
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        engine = ed._layer.engine
        ed.focus()
        # Normal mode: inserted at the cursor, mode stays normal.
        app.post_message(events.Paste("X\nY\r\nZ"))
        await pilot.pause()
        assert ed.text == engine.text == "X\nY\nZab\ncd\n"
        assert ed.submode == "normal"
        await pilot.press("u")
        assert ed.text == engine.text == "ab\ncd\n"
        # Insert mode: mode stays insert, still a single step.
        await pilot.press("A")
        app.post_message(events.Paste("123"))
        await pilot.pause()
        assert ed.submode == "insert" and ed.text == "ab123\ncd\n"
        await pilot.press("escape")
        assert engine.cursor == ed.cursor_location


async def test_pageup_pagedown_keys():
    text = "".join(f"line {i}\n" for i in range(200))
    app = _EdApp(text=text)
    async with app.run_test(size=(80, 24)) as pilot:
        ed = app.query_one("#ed", CodeEditor)
        first, last = ed.visible_rows()
        page = last - first + 1
        await pilot.press("pagedown")
        assert ed.scroll_offset.y == page - 2
        assert ed.cursor_location[0] == page - 2 == ed._layer.engine.cursor[0]
        await pilot.press("pagedown")
        assert ed.scroll_offset.y == 2 * (page - 2)
        await pilot.press("pageup")
        assert ed.scroll_offset.y == page - 2
        await pilot.press("ctrl+d")
        assert ed.visible_rows()[0] == page - 2 + page // 2


async def test_inline_search_line_mirrors_cursor():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.press("slash", "f", "i")
        assert (ed.submode, ed.command_text) == ("search", "/fi")
        assert ed.cursor_location == (1, 7)  # incremental: on "five"
        await pilot.press("escape")
        assert (ed.submode, ed.cursor_location) == ("normal", (0, 0))
        assert app.leave == []  # Esc only cancelled the search
        await pilot.press("slash", "s", "enter", "n")
        assert ed.cursor_location == (3, 0)
        await pilot.press("question_mark", "f", "o", "enter")
        assert ed.cursor_location == (1, 2)


async def test_h_l_use_the_real_window():
    text = "".join(f"line {i}\n" for i in range(200))
    app = _EdApp(text=text)
    async with app.run_test(size=(80, 24)) as pilot:
        ed = app.query_one("#ed", CodeEditor)
        first, last = ed.visible_rows()
        await pilot.press("L")
        assert ed.cursor_location[0] == last
        await pilot.press("z", "t")
        assert ed.scroll_offset.y == last


async def test_mouse_up_finalizes_selection_once(monkeypatch):
    from textual.widgets import TextArea

    calls = []
    real = TextArea._on_mouse_up

    async def counting(self, event):
        calls.append(1)
        await real(self, event)

    monkeypatch.setattr(TextArea, "_on_mouse_up", counting)
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.query_one("#ed", CodeEditor)
        await pilot.click(CodeEditor, offset=_cell(ed, 0, 2))
        assert len(calls) == 1 and not ed._selecting


async def test_paste_while_search_line_is_open_goes_to_the_line():
    app = _EdApp()
    async with app.run_test() as pilot:
        ed = app.ed
        await _send(pilot, "slash", "/")
        ed.post_message(events.Paste("five"))
        await pilot.pause()
        assert ed.text == TEXT
        assert ed.command_text == "/five"
        await _send(pilot, "enter", "\r")
        assert ed.cursor_location == (1, 7)


async def test_bar_motion_uses_the_textarea_tab_width():
    app = _EdApp("\tx = 1\n")
    async with app.run_test() as pilot:
        ed = app.ed
        assert ed.indent_width == 4
        await _send(pilot, "5", "5")
        await _send(pilot, "|", "|")
        assert ed.cursor_location == (0, 1)  # x is drawn at screen column 4

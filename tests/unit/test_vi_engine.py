"""ViEngine: headless prompt_toolkit vi engine core.

Async tests so an asyncio loop is running (some prompt_toolkit paths call
``app.create_background_task``).
"""

from __future__ import annotations

import pytest

from tdb.vi import LeaveRequest, Notify, SaveRequest, ViEngine, ViKey, ViSession


def boom_bindings(key: str):
    """Extra bindings with *key* bound to: edit the buffer, then raise."""
    from prompt_toolkit.key_binding import KeyBindings

    kb = KeyBindings()

    @kb.add(key)
    def _(event):
        event.app.current_buffer.insert_text("JUNK")
        event.app.vi_state.input_mode = "vi-insert"
        raise RuntimeError("boom")

    return kb


async def test_cw_changes_word():
    e = ViEngine("foo bar\n")
    e.feed_keys("cwX<Esc>")
    assert (e.text, e.cursor, e.mode) == ("X bar\n", (0, 0), "normal")


@pytest.mark.parametrize(
    ("text", "keys", "expected"),
    [
        ("foo bar\n", "lcwX<Esc>", "fX bar\n"),
        ("foo bar baz\n", "c2wX<Esc>", "X baz\n"),
        ("foo bar baz\n", "2cwX<Esc>", "X baz\n"),
        ("a.b c\n", "cWX<Esc>", "X c\n"),
        ("a bc\n", "cwX<Esc>", "X bc\n"),
        ("a  bc\n", "lcwX<Esc>", "aXbc\n"),  # on blanks: like dw
        ("foo bar\n", "dw", "bar\n"),  # d still eats the white space
    ],
)
async def test_cw_vim_special_case(text, keys, expected):
    e = ViEngine(text)
    e.feed_keys(keys)
    assert e.text == expected


async def test_trailing_newline_is_not_a_line():
    e = ViEngine("a\nb\n")
    e.feed_keys("Gdd")
    assert e.text == "a\n" and e.cursor == (0, 0)
    e = ViEngine("a\nb")
    e.feed_keys("Gyyp")
    assert e.text == "a\nb\nb"  # no final newline added


async def test_pending_operator_and_status():
    e = ViEngine("abc\n")
    e.feed_keys("2d")
    assert e.has_pending and e.status == "2d" and e.text == "abc\n"
    e.feed_keys("<Esc>")
    assert not e.has_pending and e.status == ""


async def test_redo_ctrl_r():
    e = ViEngine("abc\n")
    e.feed_keys("xu<C-r>")
    assert e.text == "bc\n"


async def test_ctrl_v_is_noop():
    e = ViEngine("abc\nabc\n")
    e.feed_keys("<C-v>")
    assert e.mode == "normal" and not e.has_pending
    e.feed_keys("jx")
    assert e.text == "abc\nbc\n"


async def test_ctrl_v_in_visual_is_noop():
    e = ViEngine("abc\n")
    e.feed_keys("v<C-v>")
    assert e.mode == "visual"


async def test_exception_restores_buffer():
    kb = boom_bindings("Z")
    e = ViEngine("abc\n", _extra_bindings=kb)
    e.feed_keys("Z")
    assert e.text == "abc\n" and e.mode == "normal"
    assert [type(x) for x in e.drain_events()] == [Notify]
    assert e.drain_events() == []
    assert not e.has_pending  # key processor reset to normal mode
    e.feed_keys("x")
    assert e.text == "bc\n"
    e.feed_keys("u")
    assert e.text == "abc\n"


async def test_exception_keeps_undo_and_redo_history():
    """The failing key's save-before undo entry must not survive it."""
    e = ViEngine("abc\n", _extra_bindings=boom_bindings("Z"))
    e.feed_keys("xxu")
    assert e.text == "bc\n"
    e.feed_keys("Z")
    e.feed_keys("<C-r>")  # redo stack not cleared by the failed key
    assert e.text == "c\n"
    e.feed_keys("uu")
    assert e.text == "abc\n"


async def test_insert_text_is_one_undo_step():
    e = ViEngine("ab\n")
    e.insert_text("XY\nZ")
    assert e.text == "XY\nZab\n" and e.mode == "normal"
    e.feed_keys("u")
    assert e.text == "ab\n"


async def test_reset_text_is_undoable():
    e = ViEngine("ab\n")
    e.reset_text("cd\n")
    assert e.text == "cd\n"
    e.feed_keys("u")
    assert e.text == "ab\n"


async def test_reset_text_tracks_final_newline():
    e = ViEngine("ab\n")
    e.reset_text("cd")
    assert e.text == "cd"


@pytest.mark.parametrize(("first", "second"), [("ab\n", "ab"), ("ab", "ab\n")])
async def test_undo_redo_restore_final_newline(first, second):
    e = ViEngine(first)
    e.reset_text(second)
    assert e.text == second
    e.feed_keys("u")
    assert e.text == first
    e.feed_keys("<C-r>")
    assert e.text == second


async def test_command_line_stub():
    e = ViEngine("abc\n  def\nghi\n")
    e.feed_keys(":")
    assert e.mode == "command" and e.status == ":" and e.has_pending
    e.feed_keys("wq")
    assert e.status == ":wq"
    e.feed_keys("<BS>")
    assert e.status == ":w"
    e.feed_keys("<CR>")
    assert e.mode == "normal" and e.status == ""
    assert [type(x) for x in e.drain_events()] == [SaveRequest]
    e.feed_keys(":q<CR>:q!<CR>:wq<CR>:x<CR>")
    assert e.drain_events() == [
        LeaveRequest(False),
        LeaveRequest(True),
        SaveRequest(),
        LeaveRequest(False),
        SaveRequest(),
        LeaveRequest(False),
    ]
    e.feed_keys("ll:2<CR>")  # nvim (nostartofline) keeps the column
    assert e.cursor == (1, 2)
    e.feed_keys(":99<CR>")
    assert e.cursor == (2, 2)
    e.feed_keys(":bogus<CR>")
    assert e.drain_events() == [Notify("Unknown command: :bogus", "warning")]
    e.feed_keys(":xy<Esc>")
    assert e.mode == "normal" and e.drain_events() == []
    e.feed_keys(":a<BS><BS>")  # Backspace on an empty line cancels
    assert e.mode == "normal"
    e.feed_keys(":<CR>")
    assert e.mode == "normal" and e.drain_events() == []
    assert e.text == "abc\n  def\nghi\n"


async def test_modes_and_selection():
    e = ViEngine("abc\ndef\n")
    e.feed_keys("i")
    assert e.mode == "insert" and e.has_pending
    e.feed_keys("<Esc>R")
    assert e.mode == "replace"
    e.feed_keys("<Esc>lvj")
    assert e.mode == "visual"
    assert e.selection == ((0, 1), (1, 1))
    e.feed_keys("<Esc>V")
    assert e.mode == "visual-line" and e.has_pending
    e.feed_keys("<Esc>")
    assert e.mode == "normal" and e.selection is None


async def test_set_cursor_leaves_visual_and_select_enters_it():
    e = ViEngine("abc\ndef\n")
    e.select((0, 1), (1, 2))
    assert e.mode == "visual" and e.selection == ((0, 1), (1, 2))
    e.set_cursor(1, 1)
    assert e.mode == "normal" and e.cursor == (1, 1) and e.selection is None


async def test_initial_cursor_and_clamp():
    e = ViEngine("abc\ndef\n", cursor=(1, 2))
    assert e.cursor == (1, 2)
    e.set_cursor(99, 99)
    assert e.cursor[0] == 1


async def test_register_prefix_is_pending():
    e = ViEngine("abc\n")
    e.feed_keys('"a')
    assert e.has_pending and e.status == '"a'


async def test_session_shared_but_fresh_mode():
    s = ViSession()
    e1 = ViEngine("abc\n", session=s)
    e1.feed_keys("i")
    e2 = ViEngine("xyz\n", session=s)
    assert e2.mode == "normal" and not e2.has_pending
    e2.feed_keys("yyp")
    assert e2.text == "xyz\nxyz\n"
    e3 = ViEngine("q\n", session=s)
    e3.feed_keys("P")
    assert e3.text == "xyz\nq\n"  # clipboard shared


async def test_recording_status():
    e = ViEngine("abc\n")
    e.feed_keys("qa")
    assert e.status == "recording @a"
    e.feed_keys("xq")
    assert e.status == ""


@pytest.mark.parametrize(
    ("notation", "names"),
    [
        ("ciw<Esc>", ["c", "i", "w", "escape"]),
        ("<CR><BS><Tab><lt>", ["enter", "backspace", "tab", "<"]),
        ("<esc><c-R><C-x>", ["escape", "ctrl+r", "ctrl+x"]),
        ("a b", ["a", " ", "b"]),
        ("<", ["<"]),
        ("<x", ["<", "x"]),
    ],
)
def test_vikey_parse(notation, names):
    assert ViKey.parse(notation) == [ViKey(n) for n in names]

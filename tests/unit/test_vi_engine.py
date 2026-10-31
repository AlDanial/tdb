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


@pytest.mark.parametrize("prefix", ["", "i", "v", "R"])
async def test_ctrl_z_never_edits(prefix):
    """prompt_toolkit's basic bindings insert a literal \\x1a for Ctrl+Z."""
    e = ViEngine("abc\ndef\n", cursor=(0, 1))
    e.feed_keys(prefix)
    mode = e.mode
    e.feed(ViKey("ctrl+z"))
    assert e.text == "abc\ndef\n" and e.mode == mode


# ---------------------------------------------------------------------------
# Viewport commands (fake 20-row window over a 100-line buffer)


class FakeViewport:
    def __init__(self, top: int = 0, height: int = 20) -> None:
        self.top = top
        self.height = height
        self.calls: list[int] = []

    def visible_rows(self) -> tuple[int, int]:
        return self.top, self.top + self.height - 1

    def scroll_to_top_row(self, row: int) -> None:
        self.calls.append(row)
        self.top = row


def _vp_engine(top: int = 0, cursor: tuple[int, int] = (0, 0), height: int = 20):
    text = "".join(f"  line {i}\n" for i in range(100))
    vp = FakeViewport(top, height)
    return ViEngine(text, cursor=cursor, viewport=vp), vp


async def test_ctrl_d_and_ctrl_u_scroll_half_a_window():
    e, vp = _vp_engine()
    e.feed_keys("<C-d>")
    assert (e.cursor[0], vp.top) == (10, 10)
    e.feed_keys("<C-d>")
    assert (e.cursor[0], vp.top) == (20, 20)
    e.feed_keys("<C-u>")
    assert (e.cursor[0], vp.top) == (10, 10)
    e.feed_keys("<C-u><C-u>")
    assert (e.cursor[0], vp.top) == (0, 0)


async def test_ctrl_d_count_sets_the_scroll_amount():
    e, vp = _vp_engine()
    e.feed_keys("5<C-d>")
    assert (e.cursor[0], vp.top) == (5, 5)
    e.feed_keys("<C-d>")  # the count is sticky, like vim's 'scroll'
    assert (e.cursor[0], vp.top) == (10, 10)


async def test_ctrl_d_keeps_the_column_and_stops_at_buffer_end():
    e, vp = _vp_engine(top=75, cursor=(90, 3))
    e.feed_keys("<C-d>")
    assert e.cursor == (99, 3) and vp.top == 80  # last line at the window bottom


async def test_ctrl_f_moves_a_page_minus_two_lines():
    e, vp = _vp_engine()
    e.feed_keys("<C-f>")
    assert vp.top == 18 and e.cursor[0] == 18
    e.feed_keys("2<C-f>")
    assert vp.top == 54 and e.cursor[0] == 54


async def test_page_keys_use_the_ctrl_f_ctrl_b_code():
    e, vp = _vp_engine()
    e.feed_keys("<PageDown>")
    assert vp.top == 18 and e.cursor[0] == 18
    e.feed_keys("<PageUp>")
    assert vp.top == 0 and e.cursor[0] == 18  # cursor still in view


async def test_ctrl_y_scrolls_without_moving_the_cursor_unless_it_must():
    e, vp = _vp_engine(top=5, cursor=(10, 0))
    e.feed_keys("<C-y>")
    assert (vp.top, e.cursor[0]) == (4, 10)
    e, vp = _vp_engine(top=5, cursor=(24, 0))
    e.feed_keys("<C-y>")
    assert (vp.top, e.cursor[0]) == (4, 23)  # bottom row of the new view


async def test_h_m_l_keep_the_column():
    e, _ = _vp_engine(top=30, cursor=(35, 5))
    e.feed_keys("H")
    assert e.cursor == (30, 5)
    e.feed_keys("L")
    assert e.cursor == (49, 5)
    e.feed_keys("M")
    assert e.cursor == (39, 5)  # vim: row 10 of a 20-row window
    e.feed_keys("20|")
    e.feed_keys("H")
    assert e.cursor == (30, 8)  # clamped to the last character


async def test_scroll_commands_keep_the_column():
    for keys in ("<C-d>", "<C-u>", "<C-f>", "<C-b>", "<PageDown>", "<PageUp>", "<C-y>"):
        e, _ = _vp_engine(top=30, cursor=(40, 5))
        e.feed_keys(keys)
        assert e.cursor[1] == 5, keys


async def test_h_and_l_counts():
    e, _ = _vp_engine(top=30, cursor=(35, 0))
    e.feed_keys("3H")
    assert e.cursor[0] == 32
    e.feed_keys("3L")
    assert e.cursor[0] == 47


async def test_l_clamps_to_the_last_line_of_a_short_buffer():
    e = ViEngine("a\nb\nc\n", viewport=FakeViewport())
    e.feed_keys("L")
    assert e.cursor[0] == 2
    e.feed_keys("M")
    assert e.cursor[0] == 1


async def test_dL_deletes_to_window_bottom_linewise():
    text = "".join(f"l{i}\n" for i in range(30))
    e = ViEngine(text, cursor=(5, 0), viewport=FakeViewport(0, 10))
    e.feed_keys("dL")
    assert e.text == "".join(f"l{i}\n" for i in [*range(5), *range(10, 30)])
    e.feed_keys("dH")
    assert e.text == "".join(f"l{i}\n" for i in range(11, 30))


async def test_zt_zz_zb_place_the_cursor_row():
    e, vp = _vp_engine(cursor=(50, 4))
    e.feed_keys("zt")
    assert vp.top == 50 and e.cursor == (50, 4)
    e.feed_keys("zz")
    assert vp.top == 41  # vim centers a 20-row window on row +9
    e.feed_keys("zb")
    assert vp.top == 31


async def test_z_cr_dot_minus_also_go_to_first_non_blank():
    for keys, top in (("z<CR>", 50), ("z.", 41), ("z-", 31)):
        e, vp = _vp_engine(cursor=(50, 6))
        e.feed_keys(keys)
        assert (vp.top, e.cursor) == (top, (50, 2)), keys


async def test_z_with_count_goes_to_that_line():
    e, vp = _vp_engine()
    e.feed_keys("61zt")
    assert (vp.top, e.cursor[0]) == (60, 60)


async def test_zz_near_top_clamps_at_row_zero():
    e, vp = _vp_engine(cursor=(3, 0))
    e.feed_keys("zz")
    assert vp.top == 0


async def test_viewport_commands_without_viewport_are_noops():
    e = ViEngine("a\nb\nc\n")
    e.feed_keys("<C-d><C-f>HLMzzzt<C-y>")
    assert e.cursor == (0, 0) and e.text == "a\nb\nc\n" and not e.has_pending


@pytest.mark.parametrize(("unit", "expected"), [("    ", "    x\n"), ("\t", "\tx\n")])
async def test_insert_tab_inserts_the_indent_unit(unit, expected):
    e = ViEngine("x\n", indent_unit=unit)
    e.feed_keys("i<Tab><Esc>")
    assert e.text == expected


# ----------------------------------------------------------------------
# Registers, `.` repeat, macros (Task 6)


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        ('"+yy', "abc\ndef\n"),
        ('"*p', "abc\ndef\n"),
        ('"+dd', "abc\ndef\n"),
        ('"*yyjp', "abc\ndef\nabc\n"),  # "*yy went nowhere: p puts "abc"
    ],
)
async def test_clipboard_registers_rejected(keys, expected):
    e = ViEngine("abc\ndef\n")
    e.feed_keys("yy")
    e.feed_keys(keys)
    assert e.text == expected
    assert Notify("Clipboard registers are not supported", "warning") in (
        e.drain_events()
    )
    assert e.session.clipboard.get_data().text == "abc"
    assert not e.has_pending


async def test_registers_shared_across_engines():
    s = ViSession()
    e1 = ViEngine("abc\ndef\n", session=s)
    e1.feed_keys('"ayyjyy')
    e2 = ViEngine("xyz\n", session=s)
    e2.feed_keys('"ap')
    assert e2.text == "xyz\nabc\n"
    e2.feed_keys("p")
    assert e2.text == "xyz\nabc\ndef\n"


async def test_dot_repeat_shared_across_engines():
    s = ViSession()
    ViEngine("abc\n", session=s).feed_keys("x")
    e2 = ViEngine("xyz\n", session=s)
    e2.feed_keys(".")
    assert e2.text == "yz\n"


async def test_last_change_kept_only_for_changes():
    e = ViEngine("abc def\nxyz\n")
    e.feed_keys("dw")
    assert e.session.last_change == ViKey.parse("dw")
    for keys in ("j", "yy", "u", "<C-r>", "v<Esc>", "qaq", "@a", "3l"):
        e.feed_keys(keys)
        assert e.session.last_change == ViKey.parse("dw"), keys


def watch_replaying(engine_box: list, seen: list):
    """Extra binding: Z records ``engine.is_replaying``."""
    from prompt_toolkit.key_binding import KeyBindings

    kb = KeyBindings()

    @kb.add("Z")
    def _(event):
        seen.append(engine_box[0].is_replaying)
        event.app.current_buffer.insert_text("Z")

    return kb


async def test_is_replaying_during_dot_and_macro():
    box: list = []
    seen: list[bool] = []
    e = ViEngine("abc\n", _extra_bindings=watch_replaying(box, seen))
    box.append(e)
    assert not e.is_replaying
    e.feed_keys("Z.qaZq@a")
    assert seen == [False, True, False, True]
    assert not e.is_replaying


async def test_error_during_replay_restores_buffer():
    e = ViEngine("abc\n", _extra_bindings=boom_bindings("Z"))
    e.feed_keys("x")
    e.session.last_change = ViKey.parse("xZ")
    e.feed_keys(".")
    assert e.text == "bc\n" and e.mode == "normal" and not e.is_replaying
    assert [type(x) for x in e.drain_events()] == [Notify]
    e.feed_keys("x")
    assert e.text == "c\n"


async def test_runaway_macro_is_stopped():
    e = ViEngine("abc\n")
    e.feed_keys("qa@aq")  # a was empty, so this records "@a" into a
    e.feed_keys("@a")
    assert any(
        isinstance(x, Notify) and x.severity == "warning" for x in e.drain_events()
    )
    assert not e.has_pending and not e.is_replaying
    e.feed_keys("x")
    assert e.text == "bc\n"


async def test_recording_includes_register_and_dot_keys():
    e = ViEngine("abc\n")
    e.feed_keys('xqa"byy.q')
    assert e.session.vi_state.named_registers["a"].text == '"byy.'


async def test_macro_with_special_keys_round_trips():
    e = ViEngine("abc\n")
    e.feed_keys("qaA<Left>X<Esc>q")
    e.feed_keys("@a")
    assert e.text == "abXXc\n"


@pytest.mark.parametrize(
    ("keys", "counts"),
    [
        ("d3w", {1}),
        ("2d3w", {0, 2}),
        ("d10w", {1, 2}),
        ('3"a2dd', {0, 3}),
        ("r3", set()),
        ("df3", set()),
        ("i3<Esc>", set()),
        ("d0", set()),
    ],
)
async def test_dot_marks_count_digits(keys, counts):
    e = ViEngine("a b c d e f g h i j k l 3\nx\ny\nz\n", cursor=(0, 2))
    e.feed_keys(keys)
    assert e.session.last_change == ViKey.parse(keys)
    assert e.session.last_change_counts == counts


async def test_register_dropped_by_q_at_and_dot():
    e = ViEngine("a\nb\n")
    e.feed_keys('"a@b')
    assert not e.has_pending and e.status == ""
    e.feed_keys('"+.')
    assert not e.has_pending
    e.feed_keys("dd")
    assert e.text == "b\n"  # not rolled back by the dropped "+


async def test_error_after_grouped_change_keeps_undo_history():
    e = ViEngine("abc\n", _extra_bindings=boom_bindings("Z"))
    e.feed_keys("ixy<Esc>")
    stack = list(e.buffer._undo_stack)
    e.feed_keys("Z")
    assert e.buffer._undo_stack == stack
    e.feed_keys("u")
    assert e.text == "abc\n"


async def test_yank_sets_zero_but_delete_does_not():
    e = ViEngine("\n")
    e.feed_keys('dd"0p')  # a delete that leaves the text unchanged
    assert e.text == "\n"
    e = ViEngine("abc def\n")
    e.feed_keys('yw"0P')
    assert e.text == "abc abc def\n"


async def test_last_visual_after_escape_operator_and_switch():
    e = ViEngine("alpha beta\ngamma delta\n")
    assert e.last_visual is None
    e.feed_keys("vl<Esc>")
    assert e.last_visual == ((0, 0), (0, 1), False)
    e.feed_keys("jVj")
    assert e.last_visual == ((0, 0), (0, 1), False)  # still selecting
    e.feed_keys("<Esc>")
    assert e.last_visual == ((1, 1), (1, 1), True)
    e.feed_keys("kvjv")  # the second v leaves Visual again
    assert e.mode == "normal"
    assert e.last_visual is not None and e.last_visual[2] is False
    e = ViEngine("alpha beta\ngamma delta\n")
    e.feed_keys("vl<Esc>$gvd")
    assert (e.text, e.cursor) == ("pha beta\ngamma delta\n", (0, 0))


async def test_last_visual_orders_start_and_end():
    e = ViEngine("alpha beta\ngamma delta\n", cursor=(1, 3))
    e.feed_keys("vkh<Esc>")
    assert e.last_visual == ((0, 2), (1, 3), False)


async def test_ctrl_v_stays_unreachable_with_visual_additions():
    e = ViEngine("abc\n")
    e.feed_keys("<C-v>")
    assert e.mode == "normal"
    e.feed_keys("v<C-v>")
    assert e.mode == "visual"


async def test_counted_insert_is_one_undo_step_and_dot_repeats():
    e = ViEngine("foo\nbar\n")
    e.feed_keys("3ix<Esc>")
    assert e.text == "xxxfoo\nbar\n"
    e.feed_keys("j0.")
    assert e.text == "xxxfoo\nxxxbar\n"
    e.feed_keys("u")
    assert e.text == "xxxfoo\nbar\n"
    e.feed_keys("u")
    assert e.text == "foo\nbar\n"


async def test_counted_insert_cancelled_by_other_exit_is_not_repeated():
    e = ViEngine("foo\n")
    e.feed_keys("3ix<Esc>")
    e.feed_keys("ay<Esc>")  # a later uncounted insert stays single
    assert e.text == "xxxyfoo\n"


async def test_increment_number_with_dot_and_count():
    e = ViEngine("y = 10\n")
    e.feed_keys("3<C-a>")
    assert e.text == "y = 13\n"
    e.feed_keys("5.")
    assert e.text == "y = 18\n"
    e.feed_keys("u")
    assert e.text == "y = 13\n"


async def test_increment_without_number_changes_nothing():
    e = ViEngine("hello\n")
    e.feed_keys("<C-a><C-x>")
    assert e.text == "hello\n"
    assert e.session.last_change is None


# ----------------------------------------------------------------------
# Search (/ ? n N * #)


def notices(e: ViEngine) -> list[tuple[str, str]]:
    return [(n.text, n.severity) for n in e.drain_events() if isinstance(n, Notify)]


async def test_incremental_search_moves_cursor():
    e = ViEngine("abc\nfree tree\n", cursor=(0, 1))
    e.feed_keys("/re")
    assert (e.mode, e.status, e.cursor) == ("search", "/re", (1, 1))
    e.feed_keys("<Esc>")
    assert (e.mode, e.status, e.cursor) == ("normal", "", (0, 1))
    e.feed_keys("/re<CR>")
    assert (e.mode, e.status, e.cursor) == ("normal", "", (1, 1))
    assert e.session.last_search == ("re", False)


async def test_incremental_search_without_match_keeps_cursor():
    e = ViEngine("abc\nfree\n", cursor=(0, 1))
    e.feed_keys("/rez")
    assert (e.mode, e.cursor) == ("search", (0, 1))
    e.feed_keys("<BS>")
    assert e.cursor == (1, 1)


async def test_backspace_on_empty_search_line_cancels():
    e = ViEngine("abc\nfree\n", cursor=(0, 1))
    e.feed_keys("/r<BS>")
    assert (e.mode, e.status) == ("search", "/")
    e.feed_keys("<BS>")
    assert (e.mode, e.status, e.cursor) == ("normal", "", (0, 1))
    assert e.session.last_search is None


async def test_question_mark_searches_backward():
    e = ViEngine("foo\nbar\nfoo\n", cursor=(2, 1))
    e.feed_keys("?o")
    assert (e.status, e.cursor) == ("?o", (0, 2))
    e.feed_keys("<CR>")
    assert e.cursor == (0, 2)
    assert e.session.last_search == ("o", True)


async def test_search_history_up_down():
    s = ViSession()
    e = ViEngine("alpha beta gamma\n", session=s)
    e.feed_keys("/beta<CR>/gamma<CR>/alpha<CR>")
    assert s.search_history == ["beta", "gamma", "alpha"]
    e.feed_keys("/<Up>")
    assert e.status == "/alpha"
    e.feed_keys("<Up>")
    assert e.status == "/gamma"
    e.feed_keys("<Down>")
    assert e.status == "/alpha"
    e.feed_keys("<Down>")
    assert e.status == "/"
    e.feed_keys("b<Up>")  # a typed prefix filters the history
    assert e.status == "/beta"
    e.feed_keys("<Down>")
    assert e.status == "/b"
    e.feed_keys("<Esc>/beta<CR>")  # a repeated pattern moves to the end
    assert s.search_history == ["gamma", "alpha", "beta"]


async def test_search_not_found_notifies():
    e = ViEngine("abc\n", cursor=(0, 1))
    e.feed_keys("/zzz<CR>")
    assert notices(e) == [("Pattern not found: zzz", "warning")]
    assert (e.cursor, e.mode) == ((0, 1), "normal")


async def test_search_wrap_notifies():
    e = ViEngine("foo\nbar\n", cursor=(1, 0))
    e.feed_keys("/foo<CR>")
    assert e.cursor == (0, 0)
    assert notices(e) == [("search hit BOTTOM, continuing at TOP", "information")]
    e.feed_keys("?bar<CR>")
    assert e.cursor == (1, 0)
    assert notices(e) == [("search hit TOP, continuing at BOTTOM", "information")]
    e.feed_keys("n")
    assert notices(e) == [("search hit TOP, continuing at BOTTOM", "information")]


async def test_invalid_regex_notifies_error():
    e = ViEngine("a(b\n")
    e.feed_keys("/(<CR>")
    [(text, severity)] = notices(e)
    assert severity == "error" and text.startswith("Invalid pattern: ")
    assert (e.text, e.cursor, e.mode) == ("a(b\n", (0, 0), "normal")
    assert e.session.last_search is None
    e.feed_keys("d/(<CR>")  # the operator is cancelled
    assert (e.text, e.mode, e.has_pending) == ("a(b\n", "normal", False)


async def test_empty_pattern_uses_last_search():
    e = ViEngine("xay a\n")
    e.feed_keys("/<CR>")
    assert notices(e) == [("No previous regular expression", "error")]
    e.feed_keys("/a<CR>/<CR>")
    assert e.cursor == (0, 4)
    e.feed_keys("?<CR>")
    assert e.cursor == (0, 1)
    assert e.session.last_search == ("a", True)


async def test_search_is_operator_target():
    e = ViEngine("one two xyz\n")
    e.feed_keys("d/x<CR>")
    assert (e.text, e.cursor, e.mode) == ("xyz\n", (0, 0), "normal")
    e.feed_keys("u")
    assert e.text == "one two xyz\n"


async def test_search_operator_with_escape_cancels():
    e = ViEngine("one two xyz\n")
    e.feed_keys("d/x<Esc>")
    assert (e.mode, e.has_pending) == ("normal", False)
    e.feed_keys("x")  # a plain x, not the motion of a pending d
    assert e.text == "ne two xyz\n"


async def test_search_operator_repeats_with_dot():
    e = ViEngine("a x b x c x\n")
    e.feed_keys("d/x<CR>l.")
    assert e.text == "xx c x\n"


async def test_search_counts():
    e = ViEngine("foo\nbar\nfoo\n")
    e.feed_keys("3/o<CR>")
    assert e.cursor == (2, 1)
    e.feed_keys("2N")
    assert e.cursor == (0, 1)


async def test_search_in_visual_mode_extends_selection():
    e = ViEngine("one two xyz\n")
    e.feed_keys("v/x<CR>")
    assert (e.mode, e.selection) == ("visual", ((0, 0), (0, 8)))
    e.feed_keys("d")
    assert e.text == "yz\n"


async def test_search_sets_previous_jump_mark():
    e = ViEngine("foo\nbar\nfoo\n", cursor=(1, 1))
    e.feed_keys("/foo<CR>")
    assert e.cursor == (2, 0)
    assert e.marks.get("'") == (1, 1)
    e.feed_keys("/zzz<CR>")  # no move: the mark is kept
    assert e.marks.get("'") == (1, 1)


async def test_n_after_question_mark_goes_backward():
    e = ViEngine("x foo x foo x\n", cursor=(0, 12))
    e.feed_keys("?foo<CR>")
    assert e.cursor == (0, 8)
    e.feed_keys("n")
    assert e.cursor == (0, 2)
    e.feed_keys("N")
    assert e.cursor == (0, 8)


async def test_n_without_previous_search_notifies():
    e = ViEngine("abc\n")
    e.feed_keys("n")
    assert notices(e) == [("No previous regular expression", "error")]
    assert e.cursor == (0, 0)


async def test_star_and_hash():
    e = ViEngine("foo.bar foo x foobar foo\n", cursor=(0, 1))
    e.feed_keys("*")
    assert e.cursor == (0, 8)
    assert e.session.last_search == (r"\bfoo\b", False)
    assert e.session.search_history[-1] == r"\bfoo\b"
    e.feed_keys("n")  # foobar is not the word foo
    assert e.cursor == (0, 21)
    e.feed_keys("#")
    assert e.cursor == (0, 8)
    assert e.session.last_search == (r"\bfoo\b", True)
    e.feed_keys("n")
    assert e.cursor == (0, 0)


async def test_star_uses_next_word_on_line_and_is_operator_target():
    e = ViEngine("  foo bar foo\n")
    e.feed_keys("*")  # on blanks: the next keyword
    assert e.cursor == (0, 10)
    e = ViEngine("foo bar foo\n")
    e.feed_keys("d*")
    assert e.text == "foo\n"
    e = ViEngine("  ..  \n")
    e.feed_keys("*")
    assert notices(e) == [("No string under cursor", "error")]


async def test_last_search_shared_via_session():
    s = ViSession()
    a = ViEngine("foo\n", session=s)
    b = ViEngine("x foo foo\n", session=s)
    a.feed_keys("/foo<CR>")
    b.feed_keys("n")
    assert b.cursor == (0, 2)
    b.feed_keys("#")
    a.feed_keys("n")  # # made the shared search backward
    assert s.last_search == (r"\bfoo\b", True)


async def test_search_in_macro():
    e = ViEngine("a x b x c x d x\n")
    e.feed_keys("qqd/x<CR>lq@q")
    assert e.text == "xx c x d x\n"


async def test_search_on_empty_buffer():
    e = ViEngine("")
    e.feed_keys("/x<CR>n*#")
    assert (e.text, e.cursor, e.mode) == ("", (0, 0), "normal")


async def test_search_with_register_and_cancel():
    e = ViEngine("one two xyz\n")
    e.feed_keys('"ad/x<CR>')
    assert e.text == "xyz\n"
    assert e.session.vi_state.named_registers["a"].text == "one two "
    e.feed_keys('"bd/y<Esc>')
    assert (e.has_pending, e.status) == (False, "")
    e.feed_keys('"cd/(<CR>')
    assert (e.has_pending, e.status) == (False, "")


async def test_search_escape_in_visual_mode_keeps_selection():
    e = ViEngine("one two xyz\n")
    e.feed_keys("vl/x")
    assert e.cursor == (0, 8)
    e.feed_keys("<Esc>")
    assert (e.mode, e.selection) == ("visual", ((0, 0), (0, 1)))


async def test_ex_line_history_up_down():
    s = ViSession()
    e = ViEngine("a\nb\nc\n", session=s)
    e.feed_keys(":2<CR>:3<CR>:<Up><Up>")
    assert e.status == ":2"
    e.feed_keys("<CR>")
    assert e.cursor == (1, 0)
    assert s.ex_history == ["3", "2"]

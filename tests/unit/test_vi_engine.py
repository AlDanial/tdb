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


async def test_command_line_basics():
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
    assert e.drain_events() == [Notify("Not an editor command: bogus", "error")]
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
    e = ViEngine("  ..  \nx .. y\n")
    e.feed_keys("*")  # no keyword: the non-blank run, searched literally
    assert e.cursor == (1, 2)
    assert e.session.last_search == (r"\.\.", False)
    e = ViEngine("q ab.. ..\nab.. ..\n", cursor=(0, 4))
    e.feed_keys("*")  # the run is the non-keyword characters only (nvim)
    assert e.cursor == (0, 7)
    e = ViEngine("  ..  \n", cursor=(0, 4))
    e.feed_keys("*")  # only blanks left on the line
    assert notices(e) == [("No string under cursor", "error")]
    assert e.cursor == (0, 4)


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


async def test_dot_repeats_the_accepted_search_pattern():
    e = ViEngine("a x b y c x d y\n")
    e.feed_keys("/x<CR>0")
    e.feed_keys("d/<Up><CR>")  # recalls x
    assert e.text == "x b y c x d y\n"
    e.feed_keys("/y<CR>")  # the history now ends with y
    e.feed_keys(".")
    assert e.text == "x b x d y\n"  # d/x again, not d/y
    assert e.session.last_change == ViKey.parse("d/x<CR>")


async def test_macro_keeps_raw_search_line_keys():
    e = ViEngine("a x b y c x d y\n")
    e.feed_keys("/x<CR>0")
    e.feed_keys("qqd/<Up><CR>q")
    assert e.session.vi_state.named_registers["q"].text == "d/\x80ku\r"


async def test_colon_in_visual_mode_prefills_the_visual_range():
    e = ViEngine("a\nb\nc\nd\n")
    e.feed_keys("jVj:")
    assert (e.mode, e.status) == ("command", ":'<,'>")
    e.feed_keys("d<CR>")
    assert e.text == "a\nd\n"
    e.feed_keys("vl:")
    assert e.status == ":'<,'>"
    e.feed_keys("<Esc>")
    assert e.mode == "normal" and e.selection is None


async def test_count_before_colon_prefills_a_relative_range():
    e = ViEngine("a\nb\nc\nd\n", cursor=(1, 0))
    e.feed_keys("3:")
    assert e.status == ":.,.+2"
    e.feed_keys("<Esc>1:")
    assert e.status == ":."


async def test_percent_normal_is_one_undo_step():
    e = ViEngine("a\nb\nc\n")
    e.feed_keys(":%normal A;<CR>")
    assert e.text == "a;\nb;\nc;\n"
    e.feed_keys("u")
    assert e.text == "a\nb\nc\n"
    e.feed_keys("<C-r>")
    assert e.text == "a;\nb;\nc;\n"
    e.feed_keys("uu")  # nothing older to undo
    assert e.text == "a\nb\nc\n"


async def test_ex_commands_are_single_undo_steps_and_clear_redo():
    e = ViEngine("a\nb\nc\nd\n")
    for cmd in (":1,2m$<CR>", ":1t0<CR>", ":2,3d<CR>"):
        before = e.text
        e.feed_keys(cmd)
        assert e.text != before
        e.feed_keys("u")
        assert e.text == before
        e.feed_keys("<C-r>")
    e.feed_keys("u:1d<CR><C-r>")  # a new change drops the redo
    assert e.text == "c\nd\na\nb\n"
    assert e.buffer._redo_stack == []


async def test_noop_move_leaves_no_undo_entry():
    e = ViEngine("a\nb\nc\n")
    undo = list(e.buffer._undo_stack)
    e.feed_keys(":2m1<CR>")
    assert (e.text, e.cursor) == ("a\nb\nc\n", (1, 0))
    assert e.buffer._undo_stack == undo


async def test_put_after_ex_delete_and_yank():
    e = ViEngine("1\n2\n3\n4\n5\n")
    e.feed_keys(":3,4d<CR>")
    assert e.session.clipboard.get_data().text == "3\n4"
    e.feed_keys("ggp")
    assert e.text == "1\n3\n4\n2\n5\n"
    e.feed_keys(':2y a<CR>G"ap')
    assert e.text == "1\n3\n4\n2\n5\n3\n"
    e.feed_keys(":1y<CR>")  # an unnamed yank also sets "0
    assert e.session.vi_state.named_registers["0"].text == "1"


async def test_dot_after_ex_delete_repeats_the_normal_change():
    e = ViEngine("abc\ndef\nghi\n")
    e.feed_keys("x:2d<CR>.")
    assert e.text == "bc\nhi\n"
    assert e.session.last_change == ViKey.parse("x")


async def test_macro_records_and_replays_ex_commands():
    e = ViEngine("1\n2\n3\n4\n")
    e.feed_keys("qq:1m$<CR>q")
    assert e.session.vi_state.named_registers["q"].text == ":1m$\r"
    assert e.text == "2\n3\n4\n1\n"
    e.feed_keys("2@q")
    assert e.text == "4\n1\n2\n3\n"
    e.feed_keys("u")  # the whole macro run is one undo step
    assert e.text == "2\n3\n4\n1\n"


async def test_marks_follow_moved_and_copied_lines():
    e = ViEngine("a\nb\nc\nd\n")
    e.feed_keys("majmb:1,2m$<CR>")
    assert (e.marks.get("a"), e.marks.get("b")) == ((2, 0), (3, 0))
    e.feed_keys(":1t0<CR>")
    assert (e.marks.get("a"), e.marks.get("b")) == ((3, 0), (4, 0))
    e.feed_keys(":1d<CR>")
    assert (e.marks.get("a"), e.marks.get("b")) == ((2, 0), (3, 0))
    e.feed_keys(":3d<CR>")
    assert (e.marks.get("a"), e.marks.get("b")) == (None, (2, 0))


async def test_marks_follow_normal_on_each_line():
    e = ViEngine("a\nb\nc\n")
    e.feed_keys("Gmc:1normal O<CR>")
    assert e.marks.get("c") == (3, 0)
    assert e.text == "\na\nb\nc\n"


async def test_ex_errors_notify_and_change_nothing():
    e = ViEngine("a\nb\n")
    e.feed_keys(":'zd<CR>")
    assert e.drain_events() == [Notify("Mark not set: z", "error")]
    e.feed_keys(":9d<CR>:1,2m1<CR>:g/a/<CR>")
    assert e.drain_events() == [
        Notify("Invalid range", "error"),
        Notify("Cannot move a range of lines into itself", "error"),
        Notify(":global without a command is not supported", "error"),
    ]
    assert e.text == "a\nb\n" and e.mode == "normal"


async def test_normal_ends_an_unfinished_command():
    e = ViEngine("abc\ndef\n")
    e.feed_keys(":%normal Ax<CR>")
    assert (e.text, e.mode) == ("abcx\ndefx\n", "normal")
    e.feed_keys(":normal d<CR>")
    assert (e.has_pending, e.status) == (False, "")
    e.feed_keys(":normal v<CR>")
    assert e.mode == "normal"
    e.feed_keys(":normal :d<CR>")  # an unfinished ':' line is dropped
    assert (e.text, e.mode) == ("abcx\ndefx\n", "normal")


async def test_normal_keys_are_not_recorded_twice_in_a_macro():
    e = ViEngine("a\nb\n")
    e.feed_keys("qq:normal Ax<CR>q")
    assert e.session.vi_state.named_registers["q"].text == ":normal Ax\r"


async def test_ex_goto_and_jump_mark():
    e = ViEngine("a\nb\nc\n")
    e.feed_keys(":3<CR>")
    assert e.cursor == (2, 0) and e.marks.get("'") == (0, 0)
    e.feed_keys("''")
    assert e.cursor == (0, 0)


async def test_normal_with_no_net_change_leaves_undo_intact():
    e = ViEngine("a\nb\nc\n")
    e.feed_keys(":normal ddP<CR>")
    assert e.text == "a\nb\nc\n"
    e.feed_keys("u")
    assert e.text == "a\nb\nc\n"
    e.feed_keys("x:normal ddP<CR>")
    assert e.buffer._redo_stack == []
    e.feed_keys("u")
    assert e.text == "a\nb\nc\n"
    e.feed_keys("<C-r>")  # the redo survived the no-op command
    assert e.text == "\nb\nc\n"


async def test_normal_u_keeps_the_redo():
    e = ViEngine("a\nb\nc\n")
    e.feed_keys("x:normal u<CR>")
    assert e.text == "a\nb\nc\n"
    e.feed_keys("<C-r>")
    assert e.text == "\nb\nc\n"
    e.feed_keys("jx:normal u<CR>u")
    assert e.text == "a\nb\nc\n"


async def test_recursive_normal_fails_cleanly(caplog):
    e = ViEngine("a\nb\n")
    e.feed_keys("qq:normal @q<CR>q")
    e.drain_events()
    with caplog.at_level("DEBUG"):
        e.feed_keys("@q")
    assert e.drain_events() == [Notify("Command too recursive", "error")]
    assert caplog.records == []  # no traceback logged
    assert (e.text, e.mode) == ("a\nb\n", "normal")


# ----------------------------------------------------------------------
# :s substitute (Task 11)

SUB_TEXT = "  ax bx\n  cc\n  xx x\n  dd\n"


async def test_substitute_reports_count_and_is_one_undo_step():
    e = ViEngine("x x\nx\ny\n")
    e.feed_keys(":%s/x/y/g<CR>")
    assert e.text == "y y\ny\ny\n"
    assert e.drain_events() == [Notify("3 substitutions on 2 lines")]
    assert e.cursor == (1, 0)  # the last changed line, first non-blank
    e.feed_keys("u")
    assert (e.text, e.cursor) == ("x x\nx\ny\n", (0, 0))
    e.feed_keys("<C-r>")
    assert e.text == "y y\ny\ny\n"


async def test_substitute_cursor_and_quiet_for_few_changes():
    e = ViEngine(SUB_TEXT, cursor=(3, 1))
    e.feed_keys(":%s/x/y/<CR>")
    assert (e.text, e.cursor) == ("  ay bx\n  cc\n  yx x\n  dd\n", (2, 2))
    assert e.drain_events() == []  # vim reports more than 'report' (2) only
    assert e.marks.get("'") == (3, 1)


async def test_substitute_pattern_not_found():
    e = ViEngine(SUB_TEXT, cursor=(3, 3))
    undo = list(e.buffer._undo_stack)
    e.feed_keys(":%s/q/y/<CR>")
    assert e.drain_events() == [Notify("Pattern not found: q", "warning")]
    assert (e.text, e.cursor) == (SUB_TEXT, (3, 3))
    assert e.buffer._undo_stack == undo
    e.feed_keys(":%s/q/y/e<CR>")  # e: no message
    assert e.drain_events() == []


async def test_substitute_errors_notify_and_change_nothing():
    e = ViEngine("a(b\n")
    e.feed_keys(":s/(/x/<CR>")
    [event] = e.drain_events()
    assert event.severity == "error" and event.text.startswith("Invalid pattern:")
    e.feed_keys(r":s/a/\3/<CR>")
    [event] = e.drain_events()
    assert event.severity == "error" and event.text.startswith("Invalid replacement:")
    e.feed_keys(":s<CR>&")
    assert e.drain_events() == [
        Notify("No previous regular expression", "error"),
        Notify("No previous regular expression", "error"),
    ]
    assert (e.text, e.mode) == ("a(b\n", "normal")


async def test_substitute_then_ampersand_repeats_on_another_line():
    e = ViEngine("xx\nxx\nxx\nxx\nxx\n")
    e.feed_keys(":s/x/y/g<CR>j:&&<CR>")  # :&& keeps the flags
    assert e.text == "yy\nyy\nxx\nxx\nxx\n"
    e.feed_keys("j&")  # & drops them
    assert e.text == "yy\nyy\nyx\nxx\nxx\n"
    e.feed_keys("j.")  # & and :s are not repeated by `.`
    assert e.text == "yy\nyy\nyx\nxx\nxx\n"
    e.feed_keys("2&")  # a count: that many lines
    assert (e.text, e.cursor) == ("yy\nyy\nyx\nyx\nyx\n", (4, 0))
    e.feed_keys("u")
    assert e.text == "yy\nyy\nyx\nxx\nxx\n"


async def test_g_ampersand_uses_last_search_on_all_lines():
    e = ViEngine("xa\nxb\nxc\n")
    e.feed_keys(":s/x/y/<CR>/b<CR>g&")
    assert e.text == "ya\nxy\nxc\n"


async def test_substitute_sets_last_search_and_empty_pattern_uses_it():
    e = ViEngine("ab ab\nab\n")
    e.feed_keys(":s/b/B/<CR>gg0n")
    assert e.cursor == (0, 4)
    e.feed_keys("/a<CR>:%s//A/<CR>")
    assert e.text == "AB ab\nAb\n"


async def test_substitute_newline_split_moves_marks():
    e = ViEngine("a,b\nc\n", cursor=(1, 0))
    e.feed_keys(r"mc:1s/,/\n/<CR>")
    assert (e.text, e.cursor) == ("a\nb\nc\n", (1, 0))
    assert e.marks.get("c") == (2, 0)
    e.feed_keys("u")
    assert e.text == "a,b\nc\n"


async def test_substitute_in_visual_range():
    e = ViEngine("x\nx\nx\n")
    e.feed_keys("Vj:s/x/y/<CR>")
    assert e.text == "y\ny\nx\n"


async def test_substitute_confirm_prompt():
    e = ViEngine(SUB_TEXT)
    e.feed_keys(":%s/x/y/gc<CR>")
    assert (e.mode, e.status) == ("confirm", "replace with y (y/n/a/q/l)?")
    assert e.cursor == (0, 3)
    assert e.has_pending
    e.feed_keys("y")
    assert (e.text.split("\n")[0], e.cursor) == ("  ay bx", (0, 6))
    e.feed_keys("n")
    assert e.cursor == (2, 2)
    e.feed_keys("zu")  # other keys are ignored
    assert (e.cursor, e.mode) == ((2, 2), "confirm")
    e.feed_keys("y")
    assert e.cursor == (2, 3)
    e.feed_keys("q")
    assert (e.mode, e.status) == ("normal", "")
    assert (e.text, e.cursor) == ("  ay bx\n  cc\n  yx x\n  dd\n", (2, 3))
    e.feed_keys("u")  # the whole confirm session is one undo step
    assert (e.text, e.cursor) == (SUB_TEXT, (0, 0))
    e.feed_keys("u")
    assert e.text == SUB_TEXT


@pytest.mark.parametrize(
    ("keys", "text", "cursor"),
    [
        ("yyyyy", "  ay by\n  cc\n  yy y\n  dd\n", (2, 0)),
        ("nnnnn", SUB_TEXT, (2, 5)),
        ("yynnn", "  ay by\n  cc\n  xx x\n  dd\n", (2, 5)),
        ("nyl", "  ax by\n  cc\n  yx x\n  dd\n", (2, 0)),
        ("na", "  ax by\n  cc\n  yy y\n  dd\n", (2, 2)),
        ("y<Esc>", "  ay bx\n  cc\n  xx x\n  dd\n", (0, 6)),
        ("y<C-c>", "  ay bx\n  cc\n  xx x\n  dd\n", (0, 6)),
        ("ynyq", "  ay bx\n  cc\n  yx x\n  dd\n", (2, 3)),
    ],
)
async def test_substitute_confirm_answers(keys, text, cursor):
    e = ViEngine(SUB_TEXT)
    e.feed_keys(":%s/x/y/gc<CR>" + keys)
    assert (e.text, e.cursor, e.mode) == (text, cursor, "normal")
    e.feed_keys("u")
    assert e.text == SUB_TEXT


async def test_substitute_confirm_reports_count_at_the_end():
    e = ViEngine(SUB_TEXT)
    e.feed_keys(":%s/x/y/gc<CR>yyy")
    assert e.drain_events() == []
    e.feed_keys("q")
    assert e.drain_events() == [Notify("3 substitutions on 2 lines")]


async def test_substitute_confirm_with_split_and_marks():
    e = ViEngine(SUB_TEXT, cursor=(3, 1))
    e.feed_keys(r"md:%s/x/Q\rR/gc<CR>nyq")
    assert e.text == "  ax bQ\nR\n  cc\n  xx x\n  dd\n"
    assert e.cursor == (3, 2)  # the match the q was given on
    assert e.marks.get("d") == (4, 1)
    assert e.marks.get("'") == (4, 1)
    e.feed_keys("u")
    assert (e.text, e.marks.get("d")) == (SUB_TEXT, (3, 1))


async def test_substitute_confirm_no_match():
    e = ViEngine(SUB_TEXT)
    e.feed_keys(":%s/q/y/c<CR>")
    assert e.mode == "normal"
    assert e.drain_events() == [Notify("Pattern not found: q", "warning")]


async def test_macro_replays_confirm_session():
    e = ViEngine("xx\nxx\nxx\n")
    e.feed_keys("qq:s/x/y/gc<CR>nyjq")
    assert e.session.vi_state.named_registers["q"].text == ":s/x/y/gc\rnyj"
    assert e.text == "xy\nxx\nxx\n"
    e.feed_keys("@q")
    assert (e.text, e.cursor) == ("xy\nxy\nxx\n", (2, 0))
    e.feed_keys("u")
    assert e.text == "xy\nxx\nxx\n"


async def test_macro_leaving_confirm_open_is_one_undo_step():
    from prompt_toolkit.clipboard import ClipboardData

    e = ViEngine("xxx\n")
    e.session.vi_state.named_registers["q"] = ClipboardData(":s/x/y/gc\ry")
    e.feed_keys("@q")
    assert (e.text, e.mode, e.cursor) == ("yxx\n", "confirm", (0, 1))
    e.feed_keys("y<Esc>")
    assert e.text == "yyx\n"
    e.feed_keys("u")
    assert e.text == "xxx\n"


async def test_normal_ends_a_confirm_session():
    e = ViEngine("xx\n")
    e.feed_keys(":normal :s/x/y/gc<lt>CR>y<CR>")
    assert (e.text, e.mode) == ("yx\n", "normal")


async def test_revert_during_confirm_ends_the_session():
    e = ViEngine("ax\nbx\n")
    e.feed_keys(":%s/x/y/c<CR>y")
    assert (e.text, e.mode) == ("ay\nbx\n", "confirm")
    e.reset_text("zz\nqq\n")  # Edit > Revert to Disk
    assert (e.text, e.mode, e.status) == ("zz\nqq\n", "normal", "")
    e.feed_keys("y<Esc>")  # a normal-mode y now, not an answer
    assert e.text == "zz\nqq\n"
    e.feed_keys("u")  # the revert is its own step
    assert e.text == "ay\nbx\n"
    e.feed_keys("u")  # then the confirm session
    assert e.text == "ax\nbx\n"


async def test_paste_during_confirm_ends_the_session():
    e = ViEngine("ax\nbx\n")
    e.feed_keys(":%s/x/y/c<CR>y")
    e.insert_text("P")
    assert (e.text, e.mode) == ("ay\nbPx\n", "normal")  # at the match
    e.feed_keys("u")
    assert e.text == "ay\nbx\n"


async def test_click_during_confirm_ends_the_session():
    e = ViEngine("ax\nbx\n")
    e.feed_keys(":%s/x/y/c<CR>")
    e.set_cursor(0, 0)
    assert (e.mode, e.cursor) == ("normal", (0, 0))
    e.feed_keys(":%s/x/y/c<CR>")
    e.select((0, 0), (0, 1))
    assert e.mode == "visual"
    assert e.text == "ax\nbx\n"


async def test_ex_args_arrive_left_stripped():
    e = ViEngine("xx\nxx\n")
    e.feed_keys(":s/x/y/<CR>j:s   g<CR>")
    assert e.text == "yx\nyy\n"

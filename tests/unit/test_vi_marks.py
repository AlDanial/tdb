"""Vi marks: MarkTable adjustment through edits, and the engine's mark commands."""

from __future__ import annotations

import pytest
from prompt_toolkit.key_binding import KeyBindings

from tdb.vi import MarkTable, Notify, ViEngine
from tdb.vi.marks import MAXCOL, edit_rows
from tdb.vi.textedit import diff_text

TEXT = "zero\none\n    two\nthree\nfour\n"


def _apply(marks: MarkTable, old: str, new: str) -> None:
    """Adjust *marks* for the change *old* -> *new*, as the engine does."""
    edit = diff_text(old, new)
    assert edit is not None
    first, old_last, new_last, cols = edit_rows(edit, old)
    marks.adjust(first, old_last, new_last, last_cols=cols)


def test_set_get_names_and_backtick_alias():
    m = MarkTable()
    assert m.get("a") is None and m.names() == []
    m.set("b", (1, 2))
    m.set("`", (3, 0))
    m.set("a", (0, 1))
    assert m.get("'") == m.get("`") == (3, 0)
    assert m.names() == ["a", "b", "'"]
    with pytest.raises(ValueError):
        m.set("A", (0, 0))


def test_inserting_two_lines_above_shifts_mark_down_two():
    m = MarkTable()
    m.set("a", (3, 2))
    m.adjust(0, 0, 2)  # row 0 became rows 0..2
    assert m.get("a") == (5, 2)
    _apply(m, TEXT, "new1\nnew2\n" + TEXT)
    assert m.get("a") == (7, 2)


def test_deleting_lines_above_shifts_mark_up():
    m = MarkTable()
    m.set("a", (3, 1))
    _apply(m, TEXT, "three\nfour\n")  # rows 0..2 deleted
    assert m.get("a") == (0, 1)


def test_deleting_the_marks_line_removes_it():
    m = MarkTable()
    m.set("a", (2, 4))
    m.set("b", (3, 0))
    _apply(m, TEXT, "zero\none\nthree\nfour\n")
    assert m.get("a") is None and m.get("b") == (2, 0)
    assert m.names() == ["b"]


def test_deleting_the_last_line_removes_its_mark():
    m = MarkTable()
    m.set("a", (4, 1))
    m.set("b", (3, 1))
    _apply(m, "zero\none\n    two\nthree\nfour", "zero\none\n    two\nthree")
    assert m.get("a") is None and m.get("b") == (3, 1)


def test_deleting_an_indented_line_below_an_indented_line():
    # diff_text matches the common indent, so the raw edit starts mid-line.
    old = "  a\n  b\n  c\n"
    m = MarkTable()
    m.set("a", (1, 2))
    m.set("c", (2, 2))
    _apply(m, old, "  a\n  c\n")
    assert m.get("a") is None and m.get("c") == (1, 2)


def test_edit_within_the_marks_line_keeps_row_and_column():
    m = MarkTable()
    m.set("a", (2, 6))
    _apply(m, TEXT, "zero\none\n    t\nthree\nfour\n")
    assert m.get("a") == (2, 6)  # the column is clamped when jumping


def test_jump_and_visual_marks_move_instead_of_being_deleted():
    m = MarkTable()
    for name in ("'", "<", ">"):
        m.set(name, (2, 1))
    m.set("a", (2, 1))
    _apply(m, TEXT, "zero\none\nthree\nfour\n")
    assert m.get("a") is None
    assert m.get("'") == m.get("<") == m.get(">") == (2, 1)


def test_visual_marks_adjust():
    m = MarkTable()
    m.set("<", (2, 0))
    m.set(">", (3, MAXCOL))
    _apply(m, TEXT, "x\n" + TEXT)
    assert (m.get("<"), m.get(">")) == ((3, 0), (4, MAXCOL))


def test_join_moves_the_mark_with_its_text():
    m = MarkTable()
    m.set("a", (3, 2))
    _apply(m, TEXT, "zero\none\n    two three\nfour\n")
    assert m.get("a") == (2, 10)


# ---------------------------------------------------------------------------
# Engine


def test_unset_mark_notifies_and_does_not_move():
    e = ViEngine(TEXT, cursor=(1, 1))
    e.feed_keys("'z")
    assert e.cursor == (1, 1)
    assert Notify("Mark not set: z", "warning") in e.drain_events()
    e.feed_keys("d`z")
    assert e.text == TEXT and not e.has_pending
    assert e.drain_events() == [Notify("Mark not set: z", "warning")]


def test_jump_to_mark_clamps_to_the_shorter_line():
    e = ViEngine(TEXT, cursor=(2, 6))
    e.feed_keys("ma0d$ix<Esc>gg`a")
    assert e.cursor == (2, 0)
    e.feed_keys("gg'a")
    assert e.cursor == (2, 0)


def test_marks_follow_edits_and_undo():
    e = ViEngine(TEXT, cursor=(3, 2))
    e.feed_keys("maggdd")
    assert e.marks.get("a") == (2, 2)
    e.feed_keys("u")
    assert e.marks.get("a") == (3, 2)
    e.feed_keys("4Gdd")  # its own line
    assert e.marks.get("a") is None


def test_engine_uses_the_given_mark_table():
    marks = MarkTable()
    marks.set("a", (2, 4))
    e = ViEngine(TEXT, marks=marks)
    e.feed_keys("'a")
    assert e.cursor == (2, 4)
    e.feed_keys("jmb")
    assert marks.get("b") == (3, 4)


def test_previous_jump_mark_starts_at_the_top():
    e = ViEngine(TEXT, cursor=(3, 1))
    assert e.marks.get("'") == (0, 0)
    e.feed_keys("``")
    assert e.cursor == (0, 0)


@pytest.mark.parametrize("keys", ["G", "gg", "3G", "}", "{", "%", "'a", "`a"])
def test_jumps_set_the_previous_jump_mark(keys):
    text = "(a\nb)\n\nc\nd\n"
    e = ViEngine(text, cursor=(3, 0))
    e.marks.set("a", (0, 1))
    if keys == "%":
        e.set_cursor(0, 0)
    start = e.cursor
    e.feed_keys(keys)
    assert e.cursor != start
    assert e.marks.get("'") == start


def test_motions_that_are_not_jumps_keep_the_previous_jump_mark():
    e = ViEngine(TEXT, cursor=(1, 0))
    e.feed_keys("jjwkma")
    assert e.marks.get("'") == (0, 0)


def test_jump_that_does_not_move_keeps_the_mark():
    e = ViEngine(TEXT, cursor=(4, 0))
    e.feed_keys("G")
    assert e.marks.get("'") == (0, 0)


def test_operator_with_jump_sets_mark():
    e = ViEngine(TEXT, cursor=(1, 0))
    e.feed_keys("dgg")
    assert e.marks.get("'") == (0, 0)  # (1, 0), then moved by the delete
    e = ViEngine(TEXT, cursor=(1, 0))
    e.feed_keys("y4G")  # the cursor stays: nvim keeps the mark too
    assert e.marks.get("'") == (0, 0)
    e.feed_keys("3Gmajjy`a")
    assert e.cursor == (2, 0) and e.marks.get("'") == (4, 0)


def test_colon_number_sets_previous_jump_mark():
    e = ViEngine(TEXT, cursor=(1, 2))
    e.feed_keys(":4<CR>")
    assert e.cursor == (3, 2)
    assert e.marks.get("'") == (1, 2)
    e.feed_keys("''")
    assert e.cursor == (1, 0)


def test_visual_marks_charwise_and_linewise():
    e = ViEngine(TEXT, cursor=(1, 1))
    e.feed_keys("vjl<Esc>")
    assert (e.marks.get("<"), e.marks.get(">")) == ((1, 1), (2, 2))
    e.feed_keys("Vk<Esc>")
    assert (e.marks.get("<"), e.marks.get(">")) == ((1, 0), (2, MAXCOL))
    e.feed_keys("gg`>")
    assert e.cursor == (2, 6)


def test_gv_area_follows_inserted_and_deleted_lines():
    e = ViEngine(TEXT, cursor=(2, 4))
    e.feed_keys("vjl<Esc>ggOnew<Esc>")
    assert e.last_visual_raw == ((3, 4), (4, 5), False)
    e.feed_keys("ggdd")
    assert e.last_visual_raw == ((2, 4), (3, 5), False)
    e.feed_keys("gv")
    assert e.selection == ((2, 4), (3, 5))


def test_reset_and_insert_text_adjust_marks():
    e = ViEngine(TEXT, cursor=(3, 0))
    e.feed_keys("ma")
    e.reset_text("top\n" + TEXT)
    assert e.marks.get("a") == (4, 0)
    e.set_cursor(0, 0)
    e.insert_text("x\ny\n")
    assert e.marks.get("a") == (6, 0)


def test_failed_key_restores_marks():
    kb = KeyBindings()

    @kb.add("Z")
    def _boom(event):
        buff = event.current_buffer
        buff.text = buff.text.split("\n", 1)[1]
        raise RuntimeError("boom")

    e = ViEngine(TEXT, cursor=(3, 0), _extra_bindings=kb)
    e.feed_keys("ma")
    e.feed_keys("Z")
    assert e.text == TEXT
    assert e.marks.get("a") == (3, 0)

"""TextEdit / diff_text: the prefix/suffix diff used to mirror engine edits."""

from __future__ import annotations

from tdb.vi.textedit import TextEdit, diff_text


def test_equal_texts_have_no_edit():
    assert diff_text("abc", "abc") is None


def test_middle_replacement():
    assert diff_text("abcd", "aXd") == TextEdit(1, 3, "X")


def test_line_delta_for_deleted_line():
    old = "a\nb\nc"
    assert diff_text(old, "a\nc").line_delta(old) == (1, 2, 1)


def test_pure_insertion_and_deletion():
    assert diff_text("ab", "aXb") == TextEdit(1, 1, "X")
    assert diff_text("aXb", "ab") == TextEdit(1, 2, "")
    assert diff_text("", "abc") == TextEdit(0, 0, "abc")


def test_repeated_chars_do_not_overlap_prefix_and_suffix():
    # Prefix "aa" and suffix "aa" would overlap in "aaa" -> "aa".
    edit = diff_text("aaa", "aa")
    assert edit is not None
    old = "aaa"
    assert old[: edit.start] + edit.new_text + old[edit.old_end :] == "aa"

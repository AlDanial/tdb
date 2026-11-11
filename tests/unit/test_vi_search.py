"""tdb.vi.search: Python-``re`` search with vim's cursor rules."""

from __future__ import annotations

import re

import pytest

from tdb.vi.search import (
    ViPatternError,
    compile_pattern,
    find,
    find_nth,
    match_spans,
    word_pattern,
)

TEXT = "foo bar\nbaz foo\nqux\n"


def test_forward_finds_next_match_after_offset():
    rx = compile_pattern("foo")
    assert find(TEXT, rx, 0, backward=False) == (12, 15, False)


def test_forward_skips_match_at_offset_and_wraps():
    rx = compile_pattern("foo")
    assert find(TEXT, rx, 12, backward=False) == (0, 3, True)


def test_backward_finds_previous_match():
    rx = compile_pattern("foo")
    assert find(TEXT, rx, 12, backward=True) == (0, 3, False)


def test_backward_wraps_to_last_match():
    rx = compile_pattern("foo")
    assert find(TEXT, rx, 0, backward=True) == (12, 15, True)


def test_only_match_at_offset_is_found_by_wrapping():
    rx = compile_pattern("qux")
    assert find(TEXT, rx, 16, backward=False) == (16, 19, True)
    assert find(TEXT, rx, 16, backward=True) == (16, 19, True)


def test_no_match_returns_none():
    assert find(TEXT, compile_pattern("zzz"), 0, backward=False) is None
    assert find(TEXT, compile_pattern("zzz"), 0, backward=True) is None
    assert find("", compile_pattern("x"), 0, backward=False) is None


def test_matches_do_not_overlap_from_line_start():
    # vim ('cpo' c): "aaaa" from column 0 finds "aa" at 2, not 1.
    rx = compile_pattern("aa")
    assert find("aaaa", rx, 0, backward=False) == (2, 4, False)
    assert find("xaaaa", rx, 0, backward=False) == (1, 3, False)
    assert find("aaaa", rx, 2, backward=True) == (0, 2, False)
    assert find("aaaa", rx, 3, backward=True) == (2, 4, False)


def test_zero_width_match_at_line_end_counts_as_last_character():
    rx = compile_pattern("$")
    # From column 0 the end of "ab" is ahead; from its last character it
    # is the cursor position itself, so the next line's end is found.
    assert find("ab\ncd", rx, 0, backward=False) == (2, 2, False)
    assert find("ab\ncd", rx, 1, backward=False) == (5, 5, False)
    assert find("ab\ncd", rx, 4, backward=True) == (2, 2, False)


def test_line_anchors_are_per_line():
    rx = compile_pattern("^")
    assert find("ab\ncd", rx, 0, backward=False) == (3, 3, False)
    assert find("a\n\nb", rx, 0, backward=False) == (2, 2, False)


def test_find_nth_counts_and_reports_wrap():
    rx = compile_pattern("o")
    text = "foo\nbar\nfoo"
    assert find_nth(text, rx, 0, backward=False, count=3) == (9, 10, False)
    assert find_nth(text, rx, 0, backward=False, count=5) == (1, 2, True)
    assert find_nth(text, compile_pattern("z"), 0, backward=False, count=2) is None


def test_invalid_pattern_raises_vi_pattern_error_with_re_message():
    with pytest.raises(ViPatternError) as info:
        compile_pattern("(")
    try:
        re.compile("(")
    except re.error as exc:
        assert str(info.value) == str(exc)


def test_star_escapes_word():
    rx = compile_pattern(word_pattern("a.b"))
    assert word_pattern("a.b") == r"\b" + re.escape("a.b") + r"\b"
    assert rx.search("axb") is None
    assert rx.search("x a.b y").span() == (2, 5)


def test_patterns_are_case_sensitive():
    assert find("Foo foo", compile_pattern("foo"), 0, backward=False) == (
        4,
        7,
        False,
    )


# ---- match_spans ('hlsearch') ----


def test_match_spans_per_row():
    rx = compile_pattern("foo")
    assert match_spans(TEXT, rx) == {0: [(0, 3)], 1: [(4, 7)]}


def test_match_spans_several_on_one_row():
    rx = compile_pattern("a")
    assert match_spans("aXa\nb\na", rx) == {0: [(0, 1), (2, 3)], 2: [(0, 1)]}


def test_match_spans_split_across_lines():
    rx = compile_pattern(r"bar\nbaz")
    assert match_spans(TEXT, rx) == {0: [(4, 7)], 1: [(0, 3)]}


def test_match_spans_starting_at_line_break():
    rx = compile_pattern(r"\nbaz")
    assert match_spans(TEXT, rx) == {1: [(0, 3)]}


def test_match_spans_skips_zero_width():
    assert match_spans(TEXT, compile_pattern("^")) == {}
    assert match_spans("ab", compile_pattern("x*")) == {}


def test_match_spans_limit():
    rx = compile_pattern(".")
    spans = match_spans("abcdef", rx, limit=3)
    assert spans == {0: [(0, 1), (1, 2), (2, 3)]}

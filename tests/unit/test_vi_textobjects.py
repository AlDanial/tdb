"""Direct tests of the pure vim text-object ports (no engine)."""

from __future__ import annotations

import pytest

from tdb.vi.textobjects import (
    NoObject,
    block_span,
    current_par,
    current_word,
    increment_number,
    join_lines,
    quote_span,
)


def test_inner_word_in_whitespace_run_takes_the_run():
    assert current_word(["one   two"], (0, 4), 1, False, False, None) == (
        (0, 3),
        (0, 5),
        True,
    )


def test_inner_word_at_indent_start_stays_on_its_line():
    start, end, _ = current_word(["foo:", "    bar"], (1, 0), 1, False, False, None)
    assert (start, end) == ((1, 0), (1, 3))


def test_word_failure_reports_cursor():
    with pytest.raises(NoObject) as fail:
        current_word(["one   "], (0, 4), 1, True, False, None)
    assert fail.value.pos[0] == 0


def test_paragraph_spans():
    lines = ["a1", "a2", "", "b1", "b2", "", "c1"]
    assert current_par(lines, 3, 1, False, None) == (3, 4)
    assert current_par(lines, 3, 1, True, None) == (3, 5)
    assert current_par(lines, 2, 1, False, None) == (2, 2)


def test_block_without_pair_is_none():
    assert block_span(["no pair"], (0, 2), "(", 1, True, None) is None


def test_block_inner_span():
    start, end, _sol = block_span(["f(a, b)"], (0, 3), "(", 1, True, None)
    assert (start, end) == ((0, 2), (0, 5))


def test_quote_span():
    assert quote_span('x = "hi"', 6, '"', False) == (5, 7)
    assert quote_span("no quotes", 2, '"', False) is None


def test_join_three_lines():
    assert join_lines(["one", "  two", "three", "four"], 0, 3, True) == (
        ["one two three", "four"],
        7,
    )
    assert join_lines(["a", " b"], 0, 2, False) == (["a b"], 1)


@pytest.mark.parametrize(
    ("line", "col", "delta", "want"),
    [
        ("y = 10", 0, 1, ("y = 11", 5)),
        ("x = -3", 0, 5, ("x = 2", 4)),
        ("x = -3", 0, -1, ("x = -4", 5)),
        ("a = 0x0f", 0, 1, ("a = 0x10", 7)),
        ("a = 0xAF", 0, 1, ("a = 0xB0", 7)),
        ("a = 0b11", 0, 1, ("a = 0b100", 8)),
        ("a = 007", 0, 1, ("a = 008", 6)),
        ("a = 099", 0, 1, ("a = 100", 6)),
        ("a = 000", 0, -1, ("a = -001", 7)),
        ("a1 b22", 3, 1, ("a1 b23", 5)),
        ("10 abc", 4, 1, None),
        ("hello", 0, 1, None),
        ("", 0, 1, None),
    ],
)
def test_increment_number(line, col, delta, want):
    assert increment_number(line, col, delta) == want

"""Find-in-page for the documentation viewer (`tdb --doc`,
Help -> Documentation)."""

from __future__ import annotations

from tdb.widgets.searchable_markdown import MIN_INCREMENTAL, find_matches


def test_min_incremental_is_four():
    assert MIN_INCREMENTAL == 4


def test_lowercase_query_is_case_insensitive():
    assert find_matches("GDB and gdb", "gdb") == [(0, 3), (8, 11)]


def test_uppercase_in_query_makes_it_case_sensitive():
    assert find_matches("GDB and gdb", "GDB") == [(0, 3)]


def test_matches_do_not_overlap():
    assert find_matches("aaaa", "aa") == [(0, 2), (2, 4)]


def test_empty_query_matches_nothing():
    assert find_matches("anything", "") == []

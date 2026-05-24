"""Unit tests for tdb.app_helpers (the pure-Python TUI-side utilities)."""

from __future__ import annotations

from tdb.app_helpers import find_readme, unquote_dap_string


# --- unquote_dap_string -------------------------------------------------


def test_unquote_simple_string():
    assert unquote_dap_string("'hello'") == "hello"


def test_unquote_with_escaped_newline():
    assert unquote_dap_string("'hello\\nworld'") == "hello\nworld"


def test_unquote_double_quoted():
    assert unquote_dap_string('"hi"') == "hi"


def test_unquote_non_string_returns_input():
    """If literal_eval gives back something that isn't a string (a list,
    int, bool), we keep the original — better to surface the unparsed
    repr than silently swallow type info."""
    assert unquote_dap_string("[1, 2, 3]") == "[1, 2, 3]"
    assert unquote_dap_string("42") == "42"


def test_unquote_invalid_python_returns_input():
    assert unquote_dap_string("<not valid python>") == "<not valid python>"


def test_unquote_empty_string_returns_empty():
    assert unquote_dap_string("") == ""


# --- find_readme --------------------------------------------------------


def test_find_readme_returns_string_or_none():
    """In a working install README is locatable; we accept either result
    rather than asserting on filesystem layout, but if it returns
    something it must be the actual README content."""
    result = find_readme()
    if result is not None:
        assert "tdb" in result.lower() or "textual" in result.lower()

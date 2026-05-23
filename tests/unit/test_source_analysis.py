"""Unit tests for tdb.source_analysis (statement-step unit computation)."""

from __future__ import annotations

from textwrap import dedent

from tdb.source_analysis import compute_step_units, find_step_unit


def _units(src: str):
    return compute_step_units(dedent(src))


def test_simple_statements_one_unit_each():
    units = _units("""\
        a = 1
        b = 2
        c = a + b
        """)
    assert units == [(1, 1), (2, 2), (3, 3)]


def test_multi_line_expression_is_one_unit():
    units = _units("""\
        results = foo(
            1,
            2,
            3,
        )
        next_line = True
        """)
    # The assignment spans lines 1-5; next_line is line 6.
    assert (1, 5) in units
    assert (6, 6) in units
    assert find_step_unit(2, units) == (1, 5)
    assert find_step_unit(5, units) == (1, 5)
    assert find_step_unit(6, units) == (6, 6)


def test_for_loop_header_separate_from_body():
    units = _units("""\
        for x in range(10):
            print(x)
            y = x * 2
        done = True
        """)
    # For-header is just line 1, body lines 2 and 3 are their own units,
    # then line 4 is its own unit.
    assert find_step_unit(1, units) == (1, 1)
    assert find_step_unit(2, units) == (2, 2)
    assert find_step_unit(3, units) == (3, 3)
    assert find_step_unit(4, units) == (4, 4)


def test_if_else_each_branch_separate():
    units = _units("""\
        if cond:
            a()
        else:
            b()
        after = True
        """)
    assert find_step_unit(1, units) == (1, 1)
    assert find_step_unit(2, units) == (2, 2)
    assert find_step_unit(4, units) == (4, 4)
    assert find_step_unit(5, units) == (5, 5)


def test_try_except_finally():
    units = _units("""\
        try:
            risky()
        except ValueError:
            handle()
        finally:
            cleanup()
        """)
    assert find_step_unit(1, units) == (1, 1)
    assert find_step_unit(2, units) == (2, 2)
    assert find_step_unit(3, units) == (3, 3)
    assert find_step_unit(4, units) == (4, 4)
    assert find_step_unit(6, units) == (6, 6)


def test_function_def_header_separate_from_body():
    units = _units("""\
        def foo():
            return 1
        x = foo()
        """)
    assert find_step_unit(1, units) == (1, 1)
    assert find_step_unit(2, units) == (2, 2)
    assert find_step_unit(3, units) == (3, 3)


def test_multi_line_call_with_keywords():
    """Mirrors the asyncio_gather.py example the user reported."""
    units = _units("""\
        async def main():
            results = await asyncio.gather(
                fetch(1, 2),
                fetch(2, 1),
                fetch(3, 3)
            )
            print(results)
        """)
    # Lines 2..6 are the assignment; line 7 is the next statement.
    assert find_step_unit(2, units) == (2, 6)
    assert find_step_unit(3, units) == (2, 6)
    assert find_step_unit(6, units) == (2, 6)
    assert find_step_unit(7, units) == (7, 7)


def test_syntax_error_returns_empty():
    assert compute_step_units("def broken(:\n    pass\n") == []


def test_find_step_unit_no_match():
    units = [(1, 1), (3, 3)]
    assert find_step_unit(2, units) is None
    assert find_step_unit(99, units) is None


def test_nested_compound_picks_innermost():
    units = _units("""\
        def outer():
            for i in range(3):
                x = i + 1
        """)
    # Line 2 should map to the for-header (2,2), not the def-header (1,1).
    assert find_step_unit(2, units) == (2, 2)
    assert find_step_unit(3, units) == (3, 3)

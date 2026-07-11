"""Unit tests for tdb.source_analysis (statement-step unit computation)."""

from __future__ import annotations

from textwrap import dedent

from tdb.source_analysis import (
    compute_step_units,
    find_step_unit,
    snap_breakpoint,
    snap_to_statement_start,
)


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


def test_match_statement_header_cases_and_bodies():
    # Regression: ast.Match has no `body` attribute (suites live in
    # `cases`); compute_step_units used to crash with AttributeError on
    # any real match statement.
    units = _units("""\
        match command:
            case "go":
                move()
                log()
            case _:
                stop()
        after = True
        """)
    assert find_step_unit(1, units) == (1, 1)  # match header
    assert find_step_unit(2, units) == (2, 2)  # case "go":
    assert find_step_unit(3, units) == (3, 3)  # move()
    assert find_step_unit(4, units) == (4, 4)  # log()
    assert find_step_unit(5, units) == (5, 5)  # case _:
    assert find_step_unit(6, units) == (6, 6)  # stop()
    assert find_step_unit(7, units) == (7, 7)  # after = True


def test_match_with_multiline_subject():
    units = _units("""\
        match build(
            arg,
        ):
            case _:
                pass
        """)
    # Header spans the multi-line subject up to the first case.
    assert find_step_unit(1, units) == (1, 3)
    assert find_step_unit(4, units) == (4, 4)
    assert find_step_unit(5, units) == (5, 5)


def test_match_inside_function():
    units = _units("""\
        def handler(arg_type):
            match arg_type:
                case int():
                    return 1
                case _:
                    return 0
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


# --- snap_to_statement_start --------------------------------------------------


def test_snap_inside_statement_returns_start():
    units = [(2, 6), (7, 7)]
    assert snap_to_statement_start(4, units) == 2
    assert snap_to_statement_start(6, units) == 2
    assert snap_to_statement_start(7, units) == 7


def test_snap_between_statements_goes_to_previous():
    # Blank line at 4 between unit (1,3) and unit (6,6).
    units = [(1, 3), (6, 6)]
    assert snap_to_statement_start(4, units) == 1
    assert snap_to_statement_start(5, units) == 1


def test_snap_before_first_statement_returns_none():
    units = [(5, 7), (10, 12)]
    assert snap_to_statement_start(1, units) is None
    assert snap_to_statement_start(4, units) is None


def test_snap_at_start_is_identity():
    units = [(2, 6)]
    assert snap_to_statement_start(2, units) == 2


# --- snap_breakpoint (uses real file) -----------------------------------------


def test_snap_breakpoint_on_multi_line_call(tmp_path):
    src = tmp_path / "x.py"
    src.write_text(
        dedent("""\
        a = 1
        results = func(
            1,
            2,
        )
        b = 2
        """)
    )
    # Sub-lines 3 and 4 belong to the assign on line 2 — snap to 2.
    assert snap_breakpoint(str(src), 3) == 2
    assert snap_breakpoint(str(src), 4) == 2
    # Line 2 is already a start.
    assert snap_breakpoint(str(src), 2) == 2
    # Line 6 is its own statement.
    assert snap_breakpoint(str(src), 6) == 6


def test_snap_breakpoint_unreadable_file_passes_through(tmp_path):
    # Pointing at a missing file: caller may want debugpy to validate
    # later, so we don't drop or snap.
    missing = tmp_path / "nope.py"
    assert snap_breakpoint(str(missing), 5) == 5


def test_snap_breakpoint_syntax_error_passes_through(tmp_path):
    src = tmp_path / "broken.py"
    src.write_text("def broken(:\n    pass\n")
    assert snap_breakpoint(str(src), 1) == 1
    assert snap_breakpoint(str(src), 2) == 2


def test_snap_breakpoint_past_end_of_file_passes_through(tmp_path):
    # Pre-existing CLI tests set BPs at lines > file length to exercise
    # the parser without caring about validity. We must not silently
    # snap those to line 1 — they're typos, debugpy should reject.
    src = tmp_path / "x.py"
    src.write_text("print('hi')\n")  # 1 line of code
    assert snap_breakpoint(str(src), 5) == 5
    assert snap_breakpoint(str(src), 99) == 99


def test_snap_breakpoint_before_first_statement_returns_none(tmp_path):
    src = tmp_path / "x.py"
    # Lines 1-3 are blank/comment-only; first statement at line 4.
    src.write_text(
        dedent("""\
        # header comment
        # more comment

        x = 1
        """)
    )
    assert snap_breakpoint(str(src), 1) is None
    assert snap_breakpoint(str(src), 3) is None
    assert snap_breakpoint(str(src), 4) == 4

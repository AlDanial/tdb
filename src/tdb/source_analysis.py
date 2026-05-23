"""AST-based analysis used by statement-granularity stepping.

A "step unit" is a contiguous line range the debugger's step-over should
treat as one logical step. For simple statements this is just the AST
node's (lineno, end_lineno). For compound statements (if/for/while/with/
try/def/class/match), the header line(s) form one unit and each body
statement is its own unit — so stepping over a `for` header doesn't
skip the loop body.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

log = logging.getLogger(__name__)


_COMPOUND_STMT_TYPES: tuple[type, ...] = (
    ast.If, ast.For, ast.AsyncFor, ast.While,
    ast.With, ast.AsyncWith, ast.Try,
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
)
if hasattr(ast, "Match"):  # 3.10+
    _COMPOUND_STMT_TYPES = _COMPOUND_STMT_TYPES + (ast.Match,)
if hasattr(ast, "TryStar"):  # 3.11+
    _COMPOUND_STMT_TYPES = _COMPOUND_STMT_TYPES + (ast.TryStar,)


def compute_step_units(
    source: str, filename: str = "<unknown>",
) -> list[tuple[int, int]]:
    """Return (start_line, end_line) ranges, one per atomic step unit.

    Empty list if the source can't be parsed. Ranges may share endpoints
    in edge cases like single-line `if x: foo()`; lookup picks the
    smallest match by span.
    """
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return []

    units: list[tuple[int, int]] = []
    for stmt in tree.body:
        _walk(stmt, units)
    return units


def find_step_unit(
    line: int, units: list[tuple[int, int]],
) -> tuple[int, int] | None:
    """Smallest unit containing `line`, or None if no unit covers it."""
    matches = [u for u in units if u[0] <= line <= u[1]]
    if not matches:
        return None
    return min(matches, key=lambda u: (u[1] - u[0], u[0]))


def snap_to_statement_start(
    line: int, units: list[tuple[int, int]],
) -> int | None:
    """Snap `line` to a step-unit start line.

    - If `line` is inside a statement, returns that statement's start.
    - If `line` is between statements (e.g., a blank or comment-only
      line), returns the start of the most recent statement before it.
    - Returns None when `line` is before the first statement, so the
      caller can drop the breakpoint with a warning.
    """
    containing = find_step_unit(line, units)
    if containing is not None:
        return containing[0]
    earlier = [u for u in units if u[0] < line]
    if not earlier:
        return None
    return max(earlier, key=lambda u: u[0])[0]


def snap_breakpoint(source_path: str, line: int) -> int | None:
    """Snap `line` to the start of a logical statement in `source_path`.

    Returns the (possibly unchanged) snapped line. Caller can compare
    against `line` to decide whether to warn.

    Edge cases — designed so the caller can stay unaware:
    - File unreadable: returns `line` unchanged (let debugpy validate).
    - Source has a SyntaxError: returns `line` unchanged (same reason).
    - `line` is past the last line of the file: returns `line` unchanged
      (snapping a typo'd line number back to the last real statement
      would be more confusing than letting debugpy reject it).
    - `line` is before the first statement: returns None (drop the bp).
    """
    try:
        source = Path(source_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return line
    units = compute_step_units(source, filename=source_path)
    if not units:
        return line
    line_count = source.count("\n") + (0 if source.endswith("\n") else 1)
    if line > line_count:
        return line
    return snap_to_statement_start(line, units)


def _walk(node: ast.AST, out: list[tuple[int, int]]) -> None:
    # Decorators trace as their own line events in 3.10+ PEP 626 bytecode.
    for dec in getattr(node, "decorator_list", []) or []:
        out.append((dec.lineno, dec.end_lineno or dec.lineno))

    if isinstance(node, _COMPOUND_STMT_TYPES):
        out.append(_header_range(node, node.body))
        for sublist in _all_suites(node):
            for child in sublist:
                _walk(child, out)
    elif isinstance(node, ast.ExceptHandler):
        out.append(_header_range(node, node.body))
        for child in node.body:
            _walk(child, out)
    elif hasattr(ast, "match_case") and isinstance(node, ast.match_case):
        out.append(_header_range(node, node.body))
        for child in node.body:
            _walk(child, out)
    elif isinstance(node, ast.stmt):
        out.append((node.lineno, node.end_lineno or node.lineno))


def _header_range(
    node: ast.AST, body: list[ast.AST],
) -> tuple[int, int]:
    """Range covering only the header of a compound node.

    Clamped so the header never spans below `node.lineno`, which can
    happen for single-line forms like `if x: foo()` where the body
    starts on the same line.
    """
    start = node.lineno  # type: ignore[attr-defined]
    if body:
        end = body[0].lineno - 1  # type: ignore[attr-defined]
    else:
        end = node.end_lineno or start  # type: ignore[attr-defined]
    return (start, max(start, end))


def _all_suites(node: ast.AST):
    for attr in ("body", "orelse", "finalbody", "handlers", "cases"):
        sublist = getattr(node, attr, None)
        if isinstance(sublist, list) and sublist:
            yield sublist

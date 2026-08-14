from itertools import pairwise
from pathlib import Path

import pytest

from tdb.adapters.tcsh.models import LogicalUnit, SourceSpan, UnitKind
from tdb.adapters.tcsh.scanner import scan_script

SCRIPT_PATH = Path("/work/main.csh")
GOLDEN_DIR = Path(__file__).parent / "tcsh_golden"


def scan(text: str):
    return scan_script(SCRIPT_PATH, text)


def assert_unit_invariants(text: str, units: tuple[LogicalUnit, ...]) -> None:
    assert "".join(unit.text for unit in units) == text
    assert units
    assert units[0].span.start_line == 1
    for previous, current in pairwise(units):
        assert previous.span.end_line + 1 == current.span.start_line
        assert previous.span.start_line <= previous.span.end_line
    assert units[-1].span.end_line == len(text.splitlines())


def test_models_are_immutable_and_slotted() -> None:
    span = SourceSpan(SCRIPT_PATH, 1, 1)
    unit = LogicalUnit(span, "echo yes\n", UnitKind.PROBEABLE, None)

    with pytest.raises((AttributeError, TypeError)):
        span.start_line = 2  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        unit.extra = "not allowed"  # type: ignore[misc]


def test_continuation_is_one_probeable_unit() -> None:
    units = scan('set values = (one \\\n+two)\necho "$values"\n')

    assert [(u.span.start_line, u.span.end_line, u.kind) for u in units] == [
        (1, 2, UnitKind.PROBEABLE),
        (3, 3, UnitKind.PROBEABLE),
    ]
    assert_unit_invariants('set values = (one \\\n+two)\necho "$values"\n', units)


def test_heredoc_body_is_part_of_command() -> None:
    units = scan("cat << EOF\nhello $user\nEOF\necho done\n")

    assert [(u.span.start_line, u.span.end_line) for u in units] == [(1, 3), (4, 4)]
    assert_unit_invariants("cat << EOF\nhello $user\nEOF\necho done\n", units)


def test_dynamic_heredoc_keeps_the_remaining_source_opaque() -> None:
    text = "cat << $EOF\nsource lib.csh\n$EOF\necho done\n"
    units = scan(text)

    assert [(u.span.start_line, u.span.end_line, u.kind) for u in units] == [
        (1, 4, UnitKind.OPAQUE),
    ]
    assert_unit_invariants(text, units)


def test_comment_trailing_backslash_does_not_continue_the_next_command() -> None:
    units = scan("echo one # comment \\\necho two\n")

    assert [(u.span.start_line, u.span.end_line, u.kind) for u in units] == [
        (1, 1, UnitKind.PROBEABLE),
        (2, 2, UnitKind.PROBEABLE),
    ]


def test_quoted_heredoc_marker_is_not_a_heredoc() -> None:
    units = scan('echo "prefix << EOF"\necho after\nEOF\necho final\n')

    assert [(u.span.start_line, u.span.end_line, u.kind) for u in units] == [
        (1, 1, UnitKind.PROBEABLE),
        (2, 2, UnitKind.PROBEABLE),
        (3, 3, UnitKind.PROBEABLE),
        (4, 4, UnitKind.PROBEABLE),
    ]


def test_empty_source_has_no_logical_units() -> None:
    assert scan("") == ()


def test_labels_and_structural_lines_are_classified() -> None:
    units = scan("again:\nif (1) then\necho yes\nendif\n")

    assert [u.kind for u in units] == [
        UnitKind.LABEL,
        UnitKind.STRUCTURAL,
        UnitKind.PROBEABLE,
        UnitKind.STRUCTURAL,
    ]


def test_only_plain_literal_source_is_recognized() -> None:
    units = scan('source "lib/helper.csh"\nsource "$where/file.csh"\n')

    assert units[0].kind is UnitKind.LITERAL_SOURCE
    assert units[0].source_target == "lib/helper.csh"
    assert units[1].kind is UnitKind.OPAQUE
    assert units[1].source_target is None


@pytest.mark.parametrize(
    "text",
    [
        "source ~/lib.csh\n",
        "source {one,two}.csh\n",
        "source *.csh\n",
        "source ?.csh\n",
        "source [ab].csh\n",
    ],
)
def test_unquoted_filename_substitution_sources_are_opaque(text: str) -> None:
    unit = scan(text)[0]

    assert unit.kind is UnitKind.OPAQUE
    assert unit.source_target is None


def test_quoted_tilde_and_braces_remain_literal_source_targets() -> None:
    units = scan('source "~/lib/{one,two}.csh"\n')

    assert units[0].kind is UnitKind.LITERAL_SOURCE
    assert units[0].source_target == "~/lib/{one,two}.csh"


@pytest.mark.parametrize("text", ["source =0/lib.csh\n", "source =-/lib.csh\n"])
def test_unquoted_directory_stack_sources_are_opaque(text: str) -> None:
    unit = scan(text)[0]

    assert unit.kind is UnitKind.OPAQUE
    assert unit.source_target is None


def test_quoted_directory_stack_syntax_remains_a_literal_source_target() -> None:
    units = scan('source "=0/lib.csh"\n')

    assert units[0].kind is UnitKind.LITERAL_SOURCE
    assert units[0].source_target == "=0/lib.csh"


@pytest.mark.parametrize(
    ("fixture_name", "expected"),
    [
        (
            "control_flow.csh",
            [
                (1, 1, UnitKind.OPAQUE, None),
                (2, 2, UnitKind.PROBEABLE, None),
                (3, 3, UnitKind.STRUCTURAL, None),
                (4, 4, UnitKind.STRUCTURAL, None),
                (5, 5, UnitKind.STRUCTURAL, None),
                (6, 6, UnitKind.STRUCTURAL, None),
                (7, 7, UnitKind.OPAQUE, None),
                (8, 8, UnitKind.STRUCTURAL, None),
                (9, 9, UnitKind.STRUCTURAL, None),
                (10, 10, UnitKind.PROBEABLE, None),
                (11, 11, UnitKind.STRUCTURAL, None),
                (12, 12, UnitKind.STRUCTURAL, None),
                (13, 13, UnitKind.STRUCTURAL, None),
                (14, 14, UnitKind.LABEL, None),
                (15, 16, UnitKind.PROBEABLE, None),
                (17, 17, UnitKind.LITERAL_SOURCE, "lib/helper.csh"),
                (18, 18, UnitKind.OPAQUE, None),
            ],
        ),
        (
            "heredoc.csh",
            [
                (1, 3, UnitKind.PROBEABLE, None),
                (4, 4, UnitKind.PROBEABLE, None),
                (5, 7, UnitKind.PROBEABLE, None),
                (8, 8, UnitKind.OPAQUE, None),
                (9, 9, UnitKind.OPAQUE, None),
            ],
        ),
    ],
)
def test_golden_scans_round_trip_with_exact_units(
    fixture_name: str,
    expected: list[tuple[int, int, UnitKind, str | None]],
) -> None:
    text = (GOLDEN_DIR / fixture_name).read_text()
    units = scan(text)

    assert_unit_invariants(text, units)
    assert [
        (u.span.start_line, u.span.end_line, u.kind, u.source_target) for u in units
    ] == expected

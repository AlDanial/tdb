"""tdb.vi.substitute: ``:s`` parsing and replacement (pure, Python ``re``)."""

from __future__ import annotations

import pytest

from tdb.vi.search import ViPatternError
from tdb.vi.substitute import (
    SubstituteError,
    SubstituteSpec,
    apply_substitute,
    iter_matches,
    parse_substitute,
    rebuild,
)

LINES = ["ax bx", "cc", "xx x"]


def sub(lines, args, first=0, last=None, **kw):
    spec = parse_substitute(args, **kw)
    return apply_substitute(
        list(lines), first, len(lines) - 1 if last is None else last, spec
    )


def test_substitute_first_match_only():
    assert sub(LINES, "/x/y/") == (["ay bx", "cc", "yx x"], 2, 2, 2)


def test_substitute_g_flag():
    assert sub(LINES, "/x/y/g") == (["ay by", "cc", "yy y"], 5, 2, 2)


def test_substitute_range_only():
    assert sub(LINES, "/x/y/g", 0, 1) == (["ay by", "cc", "xx x"], 2, 1, 0)


def test_substitute_i_flag():
    assert sub(["aX bx"], "/x/y/gi")[0] == ["ay by"]
    assert sub(["aX bx"], "/x/y/g")[0] == ["aX by"]  # case-sensitive by default
    assert sub(["aX bx"], "/x/y/giI")[0] == ["aX by"]  # I: case-sensitive again


def test_substitute_delimiter_hash():
    assert sub(["a/b/c"], "#/#-#g")[0] == ["a-b-c"]
    assert parse_substitute(r"#a\#b#c\#d#") == SubstituteSpec(r"a\#b", "c#d", "")


def test_substitute_groups_swap_words():
    assert sub(["hello world"], r"/(\w+) (\w+)/\2 \1/")[0] == ["world hello"]
    assert sub(["k=v"], r"/(?P<k>\w)=(?P<v>\w)/\g<v>=\g<k>/")[0] == ["v=k"]


def test_substitute_backslash_and_ampersand():
    # Python re: & is literal (not vim's whole match); \\ is one backslash.
    assert sub(["a.b"], r"/\./&/")[0] == ["a&b"]
    assert sub(["a.b"], r"/\./\\/")[0] == ["a\\b"]
    assert sub(["a.b"], r"/\./[\\]/")[0] == ["a[\\]b"]


def test_substitute_newline_splits_the_line():
    new, n, nlines, last_row = sub(["a,b,c", "d"], r"/,/\n/g")
    assert (new, n, nlines, last_row) == (["a", "b", "c", "d"], 2, 1, 2)
    # vim's \r splits too; an escaped backslash before r/n does not.
    assert sub(["a,b"], r"/,/\r/")[0] == ["a", "b"]
    assert sub(["a,b"], r"/,/\\n/")[0] == ["a\\nb"]


def test_substitute_line_anchors_are_per_line():
    assert sub(["ab", "cd"], "/^/# /")[0] == ["# ab", "# cd"]
    assert sub(["ab", "cd"], "/$/;/")[0] == ["ab;", "cd;"]


def test_substitute_missing_trailing_delimiters():
    assert parse_substitute("/x/y") == SubstituteSpec("x", "y", "")
    assert parse_substitute("/x") == SubstituteSpec("x", "", "")
    assert parse_substitute("/x/y/ g ") == SubstituteSpec("x", "y", "g")
    assert parse_substitute("/x/y ") == SubstituteSpec("x", "y ", "")


def test_substitute_empty_pattern_uses_last_search():
    assert parse_substitute("//Z/", last_search="b") == SubstituteSpec("b", "Z", "")
    with pytest.raises(SubstituteError, match="No previous regular expression"):
        parse_substitute("//Z/")


def test_parse_substitute_empty_repeats():
    last = SubstituteSpec("x", "y", "gc")
    # As vim's :s / &: the last pattern and replacement, without the flags.
    assert parse_substitute("", last=last) == SubstituteSpec("x", "y", "")
    assert parse_substitute("&", last=last) == SubstituteSpec("x", "y", "gc")
    assert parse_substitute("g", last=last) == SubstituteSpec("x", "y", "g")
    assert parse_substitute("&i", last=last) == SubstituteSpec("x", "y", "gci")
    with pytest.raises(SubstituteError, match="No previous regular expression"):
        parse_substitute("")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ("/x/y/q", "Trailing characters: q"),
        ("/x/y/g3", "Trailing characters: 3"),
        (r"\x\y\\", "Regular expressions can't be delimited by letters"),
        ('"x"y"', "Regular expressions can't be delimited by letters"),
    ],
)
def test_parse_substitute_errors(args, message):
    with pytest.raises(SubstituteError, match=message):
        parse_substitute(args, last=SubstituteSpec("x", "y", ""))


def test_substitute_invalid_pattern():
    lines = ["a(b", "c"]
    spec = parse_substitute("/(/x/")
    with pytest.raises(ViPatternError):
        apply_substitute(lines, 0, 1, spec)
    assert lines == ["a(b", "c"]


def test_substitute_invalid_replacement_changes_nothing():
    lines = ["ab"]
    with pytest.raises(SubstituteError, match="Invalid replacement"):
        apply_substitute(lines, 0, 0, parse_substitute(r"/a/\3/"))
    with pytest.raises(SubstituteError, match="Invalid replacement"):
        # Checked even when nothing matches.
        apply_substitute(lines, 0, 0, parse_substitute(r"/q/\3/"))
    assert lines == ["ab"]


def test_substitute_no_match():
    assert sub(LINES, "/q/y/g") == (LINES, 0, 0, -1)


def test_substitute_g_flag_toggles():
    assert sub(["xx"], "/x/y/gg")[0] == ["yx"]


def test_iter_matches():
    spec = parse_substitute("/x/y/g")
    assert list(iter_matches(LINES, 0, 2, spec)) == [
        (0, 1, 2),
        (0, 4, 5),
        (2, 0, 1),
        (2, 1, 2),
        (2, 3, 4),
    ]
    spec = parse_substitute("/x/y/")
    assert list(iter_matches(LINES, 1, 2, spec)) == [(2, 0, 1)]


def test_rebuild_with_some_matches_accepted():
    spec = parse_substitute(r"/x/Q\nR/g")
    done = rebuild(LINES, 0, 2, spec, {(0, 4, 5), (2, 1, 2)})
    assert done.lines == ["ax bQ", "R", "cc", "xQ", "R x"]
    assert done.row_map == [0, 2, 3]
    assert (done.substitutions, done.lines_changed, done.last_row) == (2, 2, 4)
    # Where each match (accepted or not) now begins, and where an
    # accepted one's line now ends.
    assert done.positions[(2, 3, 4)] == (4, 2)
    assert done.positions[(2, 1, 2)] == (3, 1)
    assert done.positions[(0, 1, 2)] == (0, 1)
    assert done.end_row(2) == 4
    assert done.end_row(1) == 2


def test_amp_flag_in_delimited_form_keeps_previous_flags():
    last = SubstituteSpec("a", "b", "g")
    assert parse_substitute("/x/y/&", last=last) == SubstituteSpec("x", "y", "g")
    # vim: g after & toggles it off again; without a previous :s, no flags.
    assert parse_substitute("/x/y/&g", last=last) == SubstituteSpec("x", "y", "gg")
    assert parse_substitute("/x/y/&") == SubstituteSpec("x", "y", "")
    spec = parse_substitute("/x/y/&", last=last)
    assert apply_substitute(["xx"], 0, 0, spec)[0] == ["yy"]


@pytest.mark.parametrize("args", ["/x/y/&&", "/x/y/g&", "&&", "g&"])
def test_amp_flag_only_first_and_once(args):
    with pytest.raises(SubstituteError, match="Trailing characters: &"):
        parse_substitute(args, last=SubstituteSpec("a", "b", "g"))


def test_rebuild_keeps_matches_with_the_same_start_apart():
    # re.finditer(r"|a", "ab"): an empty match and "a", both at 0.
    spec = parse_substitute("/|a/-/g")
    matches = list(iter_matches(["ab"], 0, 0, spec))
    assert matches[:2] == [(0, 0, 0), (0, 0, 1)]
    done = rebuild(["ab"], 0, 0, spec, {(0, 0, 1)})
    assert done.lines == ["-b"]
    assert done.positions[(0, 0, 0)] == (0, 0)
    assert done.positions[(0, 0, 1)] == (0, 0)

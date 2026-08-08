"""Parsing `declare -p` / `local -p` output into variables."""

from tdb.adapters.bash.declares import BashVar, parse_declares


def test_scalar():
    assert parse_declares('declare -- name="World"') == [
        BashVar(name="name", value='"World"', children=None)
    ]


def test_integer_and_export_flags():
    out = parse_declares('declare -i count="42"\ndeclare -x PATH="/usr/bin"')
    assert out[0] == BashVar(name="count", value='"42"', children=None)
    assert out[1].name == "PATH"


def test_scalar_with_escaped_quotes():
    # bash renders: declare -- s="a \"b\" \$d" — value kept verbatim
    out = parse_declares('declare -- s="a \\"b\\" \\$d"')
    assert out[0].name == "s"
    assert out[0].value == '"a \\"b\\" \\$d"'


def test_unquoted_scalar():
    # `local -p` can render unassigned/plain values without quotes
    assert parse_declares("declare -- flag").pop() == BashVar(
        name="flag", value="", children=None
    )


def test_indexed_array():
    out = parse_declares('declare -a fruits=([0]="apple" [1]="banana" [2]="cherry")')
    v = out[0]
    assert v.name == "fruits"
    assert v.value == "array[3]"
    assert v.children == [("0", '"apple"'), ("1", '"banana"'), ("2", '"cherry"')]


def test_sparse_indexed_array_numeric_order():
    out = parse_declares('declare -a a=([10]="x" [2]="y")')
    assert out[0].children == [("2", '"y"'), ("10", '"x"')]


def test_assoc_array_sorted_keys():
    out = parse_declares('declare -A m=([zed]="1" [alpha]="2")')
    v = out[0]
    assert v.value == "assoc[2]"
    assert v.children == [("alpha", '"2"'), ("zed", '"1"')]


def test_array_value_containing_bracket_and_paren():
    out = parse_declares('declare -a a=([0]="fn(x)" [1]="[ok]")')
    assert out[0].children == [("0", '"fn(x)"'), ("1", '"[ok]"')]


def test_multiple_lines_and_garbage_skipped():
    text = 'declare -- a="1"\nnot a declare line\ndeclare -a b=([0]="2")'
    out = parse_declares(text)
    assert [v.name for v in out] == ["a", "b"]


def test_empty_input():
    assert parse_declares("") == []

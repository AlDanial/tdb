from __future__ import annotations

import pytest

from tdb.adapters.tcsh.inspect import (
    InspectionError,
    StaleReferenceError,
    UnknownFrameError,
    UnknownReferenceError,
    Variable,
    VariableStore,
    parse_alias,
    parse_env,
    parse_set,
)


def pairs(variables: tuple[Variable, ...]) -> list[tuple[str, str]]:
    return [(variable.name, variable.value) for variable in variables]


def test_parse_set_preserves_scalar_list_and_empty_text() -> None:
    parsed = parse_set("name\tvalue with spaces\nargv\t(one two)\nempty\t\n")

    assert pairs(parsed) == [
        ("argv", "(one two)"),
        ("empty", ""),
        ("name", "value with spaces"),
    ]
    assert all(variable.variables_reference == 0 for variable in parsed)


def test_parse_env_splits_only_the_first_equals_and_sorts_names() -> None:
    parsed = parse_env("Z=last\nA=one=two\nEMPTY=\n")

    assert pairs(parsed) == [("A", "one=two"), ("EMPTY", ""), ("Z", "last")]


def test_parse_alias_preserves_embedded_whitespace_and_sorts_names() -> None:
    parsed = parse_alias("where\tpwd\nll\techo one  two\tthree\n")

    assert pairs(parsed) == [("ll", "echo one  two\tthree"), ("where", "pwd")]


@pytest.mark.parametrize(
    ("parser", "output"),
    [
        (parse_set, "missing-tab\n"),
        (parse_env, "missing-equals\n"),
        (parse_alias, "missing-tab\n"),
        (parse_set, "name\tvalue\n\nother\tvalue\n"),
    ],
)
def test_parsers_reject_malformed_nonempty_records(parser, output: str) -> None:
    with pytest.raises(InspectionError, match="malformed"):
        parser(output)


def test_store_exposes_four_fixed_scopes_for_each_current_frame() -> None:
    store = VariableStore()
    store.begin_stop([100, 200])

    first = store.scopes(100)
    second = store.scopes(200)

    assert [scope.name for scope in first] == [
        "Shell Variables",
        "Environment",
        "Aliases",
        "Arguments",
    ]
    assert all(scope.expensive is False for scope in (*first, *second))
    assert len({scope.variables_reference for scope in (*first, *second)}) == 8


def test_store_reuses_current_live_values_for_every_source_frame() -> None:
    store = VariableStore()
    store.begin_stop([100, 200])
    store.cache_set(parse_set("argv\t(one two)\nshared\tcurrent\n"))

    first_scopes = store.scopes(100)
    second_scopes = store.scopes(200)

    assert store.variables(first_scopes[0].variables_reference) == store.variables(
        second_scopes[0].variables_reference
    )
    assert pairs(store.variables(first_scopes[3].variables_reference)) == [
        ("argv", "(one two)")
    ]
    assert pairs(store.variables(second_scopes[3].variables_reference)) == [
        ("argv", "(one two)")
    ]


def test_store_differentiates_unknown_frames_and_references() -> None:
    store = VariableStore()
    store.begin_stop([100])

    with pytest.raises(UnknownFrameError, match="999"):
        store.scopes(999)
    with pytest.raises(UnknownReferenceError, match="999"):
        store.variables(999)


def test_handles_expire_on_resume_and_new_stop_uses_new_handles() -> None:
    store = VariableStore()
    store.begin_stop([100])
    reference = store.scopes(100)[0].variables_reference
    store.invalidate()

    with pytest.raises(StaleReferenceError, match=str(reference)):
        store.variables(reference)

    store.begin_stop([101])
    new_reference = store.scopes(101)[0].variables_reference
    assert new_reference != reference
    with pytest.raises(StaleReferenceError, match=str(reference)):
        store.variables(reference)


def test_parsers_hide_adapter_internal_variables() -> None:
    assert pairs(parse_set("__tcsh_dap_original_0\t/orig/path.csh\nname\tvalue")) == [
        ("name", "value")
    ]
    assert pairs(parse_env("__tcsh_dap_original_0=/orig/path.csh\nHOME=/home/al")) == [
        ("HOME", "/home/al")
    ]

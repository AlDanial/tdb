"""Tests for the tdb --trace spec loader and validator."""

from __future__ import annotations

import pytest

from tdb.trace.spec import (
    SpecError,
    defined_functions,
    load_spec,
    norm_path,
    normalize_name,
    unmatched_entries,
)

MOD = """\
def handle_request(r):
    pass
class Cart:
    def add(self, x):
        def helper():
            pass
    class Inner:
        def x(self): pass
async def fetch():
    pass
f = lambda: 1
"""


def write(tmp_path, name, text):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_defined_functions_qualnames(tmp_path):
    p = write(tmp_path, "mod.py", MOD)
    assert defined_functions(str(p)) == [
        ("handle_request", 1),
        ("Cart.add", 4),
        ("Cart.add.helper", 5),
        ("Cart.Inner.x", 8),
        ("fetch", 9),
    ]


def test_normalize_name():
    assert normalize_name("Cart::add") == "Cart.add"
    assert normalize_name("outer.<locals>.inner") == "outer.inner"


def test_simple_form_loads(tmp_path):
    write(tmp_path, "mod.py", MOD)
    s = load_spec(
        write(
            tmp_path,
            "s.yaml",
            "mod.py:\n  - handle_request\n  - Cart::add\n  - Cart.Inner.x\n",
        )
    )
    (e,) = s.entries
    assert e.kind == "path" and e.files == (norm_path(tmp_path / "mod.py"),)
    assert [f.pattern for f in e.functions] == [
        "handle_request",
        "Cart.add",
        "Cart.Inner.x",
    ]


def test_paths_resolve_relative_to_spec_dir(tmp_path, monkeypatch):
    write(tmp_path / "src", "mod.py", MOD)
    spec = write(tmp_path / "conf", "s.yaml", "../src/mod.py:\n  - fetch\n")
    monkeypatch.chdir(tmp_path / "src")
    assert load_spec(spec).entries[0].files == (norm_path(tmp_path / "src/mod.py"),)


def test_glob_key_expands_recursively(tmp_path):
    write(tmp_path / "pkg/a", "one.py", "def f(): pass\n")
    write(tmp_path / "pkg", "two.py", "def g(): pass\n")
    s = load_spec(write(tmp_path, "s.yaml", "pkg/**/*.py:\n  - '*'\n"))
    assert len(s.entries[0].files) == 2


def test_module_key(tmp_path):
    s = load_spec(write(tmp_path, "s.yaml", "orders.workers:\n  - Worker::run\n"))
    e = s.entries[0]
    assert (e.kind, e.files, e.suffixes) == (
        "module",
        (),
        ("orders/workers.py", "orders/workers/__init__.py"),
    )


def test_unknown_function_reports_did_you_mean(tmp_path):
    write(tmp_path, "mod.py", MOD)
    with pytest.raises(SpecError) as ei:
        load_spec(write(tmp_path, "s.yaml", "mod.py:\n  - handel_request\n"))
    assert ei.value.problems == [
        "mod.py: 'handel_request' matches no function in mod.py (did you mean: handle_request?)"
    ]


def test_long_form_options(tmp_path):
    write(tmp_path, "mod.py", MOD)
    s = load_spec(
        write(
            tmp_path,
            "s.yaml",
            "mod.py:\n  - handle_request:\n      capture: [r.path, 'len(r)']\n      return: true\n"
            "      if: \"r.method == 'POST'\"\n      max_hits: 500\n",
        )
    )
    f = s.entries[0].functions[0]
    assert (f.capture, f.record_return, f.condition, f.max_hits) == (
        ("r.path", "len(r)"),
        True,
        "r.method == 'POST'",
        500,
    )


def test_all_problems_reported_together(tmp_path):
    write(tmp_path, "mod.py", MOD)
    with pytest.raises(SpecError) as ei:
        load_spec(
            write(
                tmp_path,
                "s.yaml",
                "mod.py:\n  - fetch: {bogus: 1}\n  - fetch: {capture: x}\n  - fetch: {max_hits: 0}\n"
                "  - fetch: {if: 'a ==='}\n  - fetch: {return: maybe}\n  - 42\n"
                "missing.py:\n  - f\n"
                "nope/*.py:\n  - f\n"
                "bad key!:\n  - f\n"
                "mod2.py:\n",
            )
        )
    p = ei.value.problems
    assert len(p) == 10
    assert "mod.py: 'fetch': unknown option 'bogus'" in p
    assert "mod.py: entry 6 must be a name or a one-key mapping" in p
    assert "missing.py: no such file" in p and "nope/*.py: matches no files" in p
    assert (
        "bad key!: not a path or module name" in p
        and "mod2.py: no functions listed" in p
    )
    assert str(ei.value) == "\n".join(p)


@pytest.mark.parametrize(
    "text,problem",
    [
        ("", "spec is empty"),
        ("- a\n", "spec must map file names to lists of functions"),
        ("a: [\n", None),
    ],
)
def test_shape_errors(tmp_path, text, problem):
    with pytest.raises(SpecError) as ei:
        load_spec(write(tmp_path, "s.yaml", text))
    assert (
        (problem in ei.value.problems)
        if problem
        else ei.value.problems[0].startswith("invalid YAML: ")
    )


def test_agent_targets_shape(tmp_path):
    write(tmp_path, "mod.py", MOD)
    s = load_spec(
        write(
            tmp_path,
            "s.yaml",
            "mod.py:\n  - Cart::*\n  - handle_request: {capture: [r]}\n",
        )
    )
    assert s.agent_targets() == [
        {
            "files": [norm_path(tmp_path / "mod.py")],
            "suffixes": [],
            "functions": [
                {
                    "pattern": "Cart.*",
                    "capture": [],
                    "return": False,
                    "if": None,
                    "max_hits": None,
                },
                {
                    "pattern": "handle_request",
                    "capture": ["r"],
                    "return": False,
                    "if": None,
                    "max_hits": None,
                },
            ],
        }
    ]


def test_unmatched_entries(tmp_path):
    write(tmp_path, "mod.py", MOD)
    s = load_spec(
        write(
            tmp_path,
            "s.yaml",
            "mod.py:\n  - fetch\n  - Cart::*\norders.workers:\n  - Worker.run\n",
        )
    )
    called = {(norm_path(tmp_path / "mod.py"), "Cart.add")}
    assert unmatched_entries(s, called) == [
        "mod.py: fetch",
        "orders.workers: Worker.run",
    ]


def test_module_key_matches_called_file_suffix(tmp_path):
    s = load_spec(write(tmp_path, "s.yaml", "orders.workers:\n  - Worker.run\n"))
    called = {("/x/orders/workers.py", "Worker.run")}
    assert unmatched_entries(s, called) == []

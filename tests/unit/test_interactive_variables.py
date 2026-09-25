"""Per-language detection of variables created from the Evaluate console.

`ProfileCapabilities.interactive_variable` maps a typed console
expression to the variable it creates (display name + the expression
that reads it back), or None. Languages whose evaluate cannot create
variables (go, OCaml under earlybird) leave the capability None.
"""

from __future__ import annotations

import pytest

from tdb.languages import registry
from tdb.languages.base import InteractiveVariable, ProfileCapabilities
from tdb.languages.cpp import build_cpp_profile
from tdb.languages.ocaml import build_ocaml_profile
from tdb.languages.rust import build_rust_profile


def _matcher(lang: str, adapter: str | None = None):
    profile = registry.resolve(lang, adapter=adapter)
    fn = profile.capabilities.interactive_variable
    assert fn is not None, f"{lang}/{adapter} should support interactive variables"
    return fn


def test_capability_defaults_to_none():
    assert ProfileCapabilities().interactive_variable is None


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("newvar = 42", InteractiveVariable("newvar", "newvar")),
        ("  n: int = 3", InteractiveVariable("n", "n")),
        ("x == 1", None),
        ("x += 1", None),
        ("print(x)", None),
        ("d['k'] = 1", None),
    ],
)
def test_python_assignment(expr, expected):
    assert _matcher("python")(expr) == expected


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("$newvar = 42", InteractiveVariable("$newvar", "$newvar")),
        ("our @list = (1, 2)", InteractiveVariable("@list", "@list")),
        ("%h = (a => 1)", InteractiveVariable("%h", "%h")),
        # `my` lexicals live only inside the eval and vanish at once.
        ("my $x = 1", None),
        ("$x == 1", None),
        ("$x =~ /a/", None),
        ("print $x", None),
    ],
)
def test_perl_assignment(expr, expected):
    assert _matcher("perl")(expr) == expected


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("newvar=42", InteractiveVariable("newvar", 'printf %s "$newvar"')),
        ("local lnew=43", InteractiveVariable("lnew", 'printf %s "$lnew"')),
        ("declare -i n=1", InteractiveVariable("n", 'printf %s "$n"')),
        ("export P=1", InteractiveVariable("P", 'printf %s "$P"')),
        ("echo hi", None),
        ("[ x=1 ]", None),
    ],
)
def test_bash_assignment(expr, expected):
    assert _matcher("bash")(expr) == expected


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("set newvar = 42", InteractiveVariable("newvar", "echo $newvar")),
        ("set n=1", InteractiveVariable("n", "echo $n")),
        ("setenv FOO bar", InteractiveVariable("FOO", "echo $FOO")),
        ("echo $x", None),
        ("set", None),
    ],
)
def test_tcsh_assignment(expr, expected):
    assert _matcher("tcsh")(expr) == expected


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("newvar = 42", InteractiveVariable("newvar", "newvar")),
        ("$g = 1", InteractiveVariable("$g", "$g")),
        ("@i = 2", InteractiveVariable("@i", "@i")),
        ("x == 1", None),
        ("puts x", None),
    ],
)
def test_ruby_assignment(expr, expected):
    assert _matcher("ruby")(expr) == expected


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("$newvar = 42", InteractiveVariable("$newvar", "$newvar")),
        ("$global:gnew = 43", InteractiveVariable("$global:gnew", "$global:gnew")),
        ("$x -eq 1", None),
        ("Write-Output $x", None),
    ],
)
def test_powershell_assignment(expr, expected):
    assert _matcher("powershell")(expr) == expected


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("set $newvar = 42", InteractiveVariable("$newvar", "$newvar")),
        ("set var $x=1", InteractiveVariable("$x", "$x")),
        # Assigning a program variable creates nothing new.
        ("set x = 3", None),
        ("print x", None),
    ],
)
def test_gdb_convenience_variable(expr, expected):
    assert _matcher("cpp", "gdb")(expr) == expected


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("int $newvar = 42", InteractiveVariable("$newvar", "$newvar")),
        ("`expr int $o = 7", InteractiveVariable("$o", "$o")),
        ("$newvar", None),
        ("x = 3", None),
        ("$x == 3", None),
    ],
)
def test_lldb_persistent_variable(expr, expected):
    assert _matcher("cpp", "lldb-dap")(expr) == expected


def test_native_adapters_share_matchers_across_languages():
    gdb = build_cpp_profile(adapter="gdb").capabilities.interactive_variable
    lldb = build_cpp_profile(adapter="lldb-dap").capabilities.interactive_variable
    assert build_rust_profile(adapter="gdb").capabilities.interactive_variable is gdb
    assert (
        build_rust_profile(adapter="lldb-dap").capabilities.interactive_variable is lldb
    )
    assert (
        build_ocaml_profile(adapter="lldb-dap").capabilities.interactive_variable
        is lldb
    )
    assert build_ocaml_profile(adapter="gdb").capabilities.interactive_variable is gdb


def test_languages_without_creatable_variables_have_no_matcher():
    assert registry.resolve("go").capabilities.interactive_variable is None
    assert (
        build_ocaml_profile(adapter="ocamlearlybird").capabilities.interactive_variable
        is None
    )

from .conftest import pytestmark_skip

pytestmark = pytestmark_skip


def test_preview_and_expand_hash(run_helper):
    code = (
        'my $h = { name => "al", nums => [1, 2, 3] };\n'
        "Devel::TdbHelper::_test_preview($h);\n"
    )
    (payload,) = run_helper(code)
    assert payload["value"].startswith("HASH")
    assert payload["id"] > 0


def test_expand_returns_one_level(run_helper):
    code = (
        'my $h = { name => "al", nums => [1, 2, 3] };\n'
        "my ($v, $id) = Devel::TdbHelper::_preview($h);\n"
        "Devel::TdbHelper::expand($id);\n"
    )
    (payload,) = run_helper(code)
    by_name = {v["name"]: v for v in payload["vars"]}
    assert by_name["name"]["value"] == "'al'"
    assert by_name["name"]["id"] == 0
    assert by_name["nums"]["value"].startswith("ARRAY")
    assert by_name["nums"]["id"] > 0


def test_blessed_overloaded_tied_and_circular(run_helper):
    code = """
package Point;
use overload '""' => sub { die "overload must not be triggered" };
sub new { my $s = bless { x => 1 }, shift; return $s }
package main;
my $p = Point->new;
my $circ = {};
$circ->{self} = $circ;
my @tied;
{ package NoisyTie; require Tie::Array; our @ISA = ('Tie::StdArray'); }
tie @tied, 'NoisyTie';
Devel::TdbHelper::_test_preview($p);
Devel::TdbHelper::_test_preview($circ);
Devel::TdbHelper::_test_preview(\\@tied);
"""
    p, circ, tied = run_helper(code)
    assert p["value"].startswith("Point=")
    assert circ["id"] > 0  # circular is just expandable, never a crash
    assert "tied" in tied["value"]


def test_undef_distinct_from_empty_string(run_helper):
    code = (
        "Devel::TdbHelper::_test_preview(undef);\n"
        "Devel::TdbHelper::_test_preview('');\n"
    )
    u, e = run_helper(code)
    assert u["value"] == "undef"
    assert e["value"] == "''"


def test_lexicals_via_pad_walk_or_degraded(run_helper):
    code = (
        "sub target { my $inside = 42; my @list = (1,2); "
        "Devel::TdbHelper::vars(0, 'lexicals') }\n"
        "target();"
    )
    (payload,) = run_helper(code)
    if "degraded" in payload:
        assert "PadWalker" in payload["degraded"]
    else:
        names = {v["name"] for v in payload["vars"]}
        assert {"$inside", "@list"} <= names

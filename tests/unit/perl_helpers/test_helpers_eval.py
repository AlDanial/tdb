from .conftest import pytestmark_skip

pytestmark = pytestmark_skip

WRAP = "{ local $@; my $r = [ eval { %s } ]; Devel::TdbHelper::emit_eval($r, $@) }"


def test_eval_scalar_result(run_helper):
    (payload,) = run_helper(WRAP % "1 + 2")
    assert payload["value"] == "3"


def test_eval_ref_result_is_expandable(run_helper):
    (payload,) = run_helper(WRAP % "{ a => 1 }")
    assert payload["value"].startswith("HASH")
    assert payload["id"] > 0


def test_eval_list_result(run_helper):
    (payload,) = run_helper(WRAP % "(1, 2, 3)")
    assert payload["value"] == "(1, 2, 3)"


def test_eval_error_captured(run_helper):
    (payload,) = run_helper(WRAP % 'die "boom"')
    assert "boom" in payload["error"]


def test_eval_sees_lexicals_in_wrapping_scope(run_helper):
    code = "my $secret = 41;\n" + (WRAP % "$secret + 1")
    (payload,) = run_helper(code)
    assert payload["value"] == "42"

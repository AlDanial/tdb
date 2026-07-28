import pytest

from .conftest import pytestmark_skip

pytestmark = pytestmark_skip


def test_location_reports_caller_and_protocol_version(run_helper, tmp_path):
    payloads = run_helper("Devel::TdbHelper::location();")
    (loc,) = payloads
    assert loc["version"] == 1
    assert loc["file"].endswith("-e") or loc["file"] == "-e"
    assert isinstance(loc["line"], int)


def test_stack_skips_helper_frames(run_helper):
    code = "sub inner { Devel::TdbHelper::stack() }\nsub outer { inner() }\nouter();"
    (payload,) = run_helper(code)
    subs = [f["sub"] for f in payload["frames"]]
    assert not any("TdbHelper" in (s or "") for s in subs)
    assert any("inner" in (s or "") for s in subs)
    assert any("outer" in (s or "") for s in subs)


def test_breakable_and_source_read_perl_line_tables(tmp_path):
    # %{"_<$file"} / @{"_<$file"} line tables only exist under -d.
    # HARNESS ADAPTATION (perl 5.40.1): the array's breakability marking
    # (elements compare != 0 only when the line is breakable) is only
    # populated for the file compiled as the *top-level* program under
    # -d; a file pulled in via `do FILE()` at runtime never gets it
    # (verified with Devel::Peek -- the do'd file's array elements keep
    # IV=0 even for statement lines, while the top-level file's get a
    # real nonzero IV). So the toy file is itself the top-level script
    # driven by `perl -d`, and it calls the helpers on its own $0 at
    # the end. Assertion content (lines 1 and 3 breakable, 2 not; the
    # source text) is unchanged from the brief.
    import json
    import os
    import re
    import subprocess

    from .conftest import HELPERS

    target = tmp_path / "toy.pl"
    target.write_text(
        "my $a = 1;\n"
        "\n"
        "my $b = 2;\n"
        "print $a + $b;\n"
        f"do {str(HELPERS)!r} or die $@ || $!;\n"
        "Devel::TdbHelper::breakable($0);\n"
        "Devel::TdbHelper::source($0);\n"
    )
    proc = subprocess.run(
        ["perl", "-d", str(target)],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "PERL5DB": "sub DB::DB {}"},
    )
    assert proc.returncode == 0, proc.stderr
    payloads = [
        json.loads(m) for m in re.findall(r"TDB>>>(.*?)<<<TDB", proc.stdout, re.S)
    ]
    lines_payload, source_payload = payloads
    assert 1 in lines_payload["lines"] and 3 in lines_payload["lines"]
    assert 2 not in lines_payload["lines"]  # blank line is not breakable
    assert "my $a = 1;" in source_payload["text"]

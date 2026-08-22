"""Language-specific fatal-error parsers behind Presentation.parse_error."""

from tdb.languages.base import ErrorFrame
from tdb.languages.errors import parse_perl_error, parse_python_error

SIMPLE = """Traceback (most recent call last):
  File "/app/main.py", line 12, in <module>
    boom()
  File "/app/lib.py", line 5, in boom
    return 1 / 0
ZeroDivisionError: division by zero
"""

CHAINED = """Traceback (most recent call last):
  File "/app/a.py", line 2, in <module>
    inner()
ValueError: first

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "/app/b.py", line 9, in <module>
    outer()
RuntimeError: second
"""


def test_returns_none_without_traceback():
    assert parse_python_error("just some stderr noise\n") is None


def test_parses_message_and_frames_in_source_order():
    p = parse_python_error(SIMPLE)
    assert p is not None
    assert p.header == "Traceback (most recent call last):"
    assert p.message == "ZeroDivisionError: division by zero"
    assert [(f.path, f.line, f.func) for f in p.frames] == [
        ("/app/main.py", 12, "<module>"),
        ("/app/lib.py", 5, "boom"),
    ]


def test_chained_traceback_uses_last_block():
    p = parse_python_error(CHAINED)
    assert p is not None
    assert p.message == "RuntimeError: second"
    assert [f.path for f in p.frames] == ["/app/b.py"]


def test_python_error_ignores_exit_code():
    # A caught traceback (traceback.print_exc()) can print alongside a
    # clean exit(0) -- gating on exit code would be a regression, so the
    # python parser must return the identical result regardless of it.
    assert parse_python_error(SIMPLE, exit_code=0) == parse_python_error(
        SIMPLE, exit_code=None
    )
    assert parse_python_error(SIMPLE, exit_code=0) is not None


def test_presentation_exposes_parser_for_python():
    from tdb.languages import registry

    profile = registry.resolve("python")
    assert profile.presentation.parse_error is not None
    assert profile.presentation.parse_error(SIMPLE) is not None


def test_presentation_parse_error_defaults_to_none():
    from tdb.languages.base import Presentation

    assert Presentation().parse_error is None


PERL_COMPILE = """Illegal division by zero at /w/has_begin.pl line 10.
 at /w/has_begin.pl line 10.
\tmain::BEGIN() called at /w/has_begin.pl line 11
\teval {...} called at /w/has_begin.pl line 11
BEGIN failed--compilation aborted at /w/has_begin.pl line 11.
"""

PERL_RUNTIME = "Illegal division by zero at /tmp/x.pl line 4.\n"

PERL_DIE_IN_SUB = """boom at /tmp/x.pl line 3.
\tmain::inner() called at /tmp/x.pl line 7
\tmain::outer() called at /tmp/x.pl line 10
"""


def test_perl_returns_none_for_plain_output():
    assert parse_perl_error("hello world\n") is None


def test_perl_runtime_die_message_and_frame():
    p = parse_perl_error(PERL_RUNTIME)
    assert p is not None
    assert p.header == "Perl error:"
    assert p.message == "Illegal division by zero"
    assert [(f.path, f.line) for f in p.frames] == [("/tmp/x.pl", 4)]


def test_perl_compile_abort_frames_skip_eval_scaffolding():
    p = parse_perl_error(PERL_COMPILE)
    assert p is not None
    assert p.message == "Illegal division by zero"
    assert [(f.path, f.line, f.func) for f in p.frames] == [
        ("/w/has_begin.pl", 11, "main::BEGIN"),
        ("/w/has_begin.pl", 10, ""),
    ]


def test_perl_nested_call_frames_outermost_first():
    p = parse_perl_error(PERL_DIE_IN_SUB)
    assert p is not None
    assert [(f.line, f.func) for f in p.frames] == [
        (10, "main::outer"),
        (7, "main::inner"),
        (3, ""),
    ]


def test_perl_warning_alone_is_not_fatal():
    warn = "Use of uninitialized value in division (/) at /w/x.pl line 10.\n"
    assert parse_perl_error(warn) is None


# --- exit-code gate (Task 4) ---------------------------------------------
# Replaces the fragile prefix denylist: a warning opener NOT on
# _PERL_WARNING_PREFIXES (e.g. "Deep recursion on subroutine") used to be
# misclassified as fatal. With a real exit code available, fatality is
# exactly `exit_code != 0` regardless of the message's wording.

UNLISTED_WARNING = 'Deep recursion on subroutine "main::f" at /w/x.pl line 3.\n'


def test_perl_gate_unlisted_warning_with_clean_exit_is_not_fatal():
    # Not on the old denylist -- would have been misclassified as fatal
    # before the exit-code gate.
    assert parse_perl_error(UNLISTED_WARNING, exit_code=0) is None


def test_perl_gate_unlisted_warning_with_nonzero_exit_is_fatal():
    p = parse_perl_error(UNLISTED_WARNING, exit_code=255)
    assert p is not None
    assert p.message == 'Deep recursion on subroutine "main::f"'


def test_perl_gate_real_die_with_none_exit_code_uses_shape_fallback():
    # exit_code=None (attach mode, or the `exited` DAP event hasn't arrived
    # yet): falls back to the old shape-based heuristic, which still
    # correctly classifies a genuine die as fatal.
    p = parse_perl_error(PERL_RUNTIME, exit_code=None)
    assert p is not None
    assert p.message == "Illegal division by zero"


def test_perl_gate_listed_warning_with_none_exit_code_uses_shape_fallback():
    warn = "Use of uninitialized value in division (/) at /w/x.pl line 10.\n"
    assert parse_perl_error(warn, exit_code=None) is None


def test_presentation_exposes_parser_for_perl():
    from tdb.languages import registry

    profile = registry.resolve("perl")
    assert profile.presentation.parse_error is not None
    assert profile.presentation.parse_error(PERL_RUNTIME) is not None


# ---- ruby ----

from tdb.languages.errors import parse_ruby_error  # noqa: E402

RUBY_CLASSIC = """\
/w/boom.rb:2:in `inner': divided by 0 (ZeroDivisionError)
\tfrom /w/boom.rb:6:in `outer'
\tfrom /w/boom.rb:9:in `<main>'
"""

RUBY_34_QUOTING = """\
/w/boom.rb:2:in 'Object#inner': divided by 0 (ZeroDivisionError)
\tfrom /w/boom.rb:6:in 'Object#outer'
\tfrom /w/boom.rb:9:in '<main>'
"""


def test_ruby_classic_traceback():
    parsed = parse_ruby_error(RUBY_CLASSIC, 1)
    assert parsed is not None
    assert parsed.header == "Ruby error:"
    assert parsed.message == "divided by 0 (ZeroDivisionError)"
    # OUTERMOST-first, failing frame last
    assert [(f.path, f.line, f.func) for f in parsed.frames] == [
        ("/w/boom.rb", 9, ""),  # <main> -> "" so frame_placeholder applies
        ("/w/boom.rb", 6, "outer"),
        ("/w/boom.rb", 2, "inner"),
    ]
    assert "divided by 0" in parsed.detail
    assert "from /w/boom.rb:6" in parsed.detail


def test_ruby_34_quoting_variant():
    parsed = parse_ruby_error(RUBY_34_QUOTING, 1)
    assert parsed is not None
    assert [f.func for f in parsed.frames] == ["", "Object#outer", "Object#inner"]


def test_ruby_error_amid_earlier_stderr_noise():
    parsed = parse_ruby_error("some warning\n" + RUBY_CLASSIC, 1)
    assert parsed is not None
    assert parsed.message == "divided by 0 (ZeroDivisionError)"


def test_ruby_single_frame_error():
    parsed = parse_ruby_error("/w/x.rb:3:in `<main>': boom (RuntimeError)\n", 1)
    assert parsed is not None
    assert parsed.frames == [ErrorFrame(path="/w/x.rb", line=3, func="")]


def test_ruby_syntax_error_old_shape():
    parsed = parse_ruby_error("/w/bad.rb:3: syntax error, unexpected end-of-input\n", 1)
    assert parsed is not None
    assert parsed.frames == [ErrorFrame(path="/w/bad.rb", line=3, func="")]
    assert "syntax error" in parsed.message


def test_ruby_syntax_error_34_shape():
    text = "/w/bad.rb:2: syntax error found (SyntaxError)\n  1 | x = 1\n> 2 | if\n"
    parsed = parse_ruby_error(text, 1)
    assert parsed is not None
    assert parsed.frames[0].line == 2
    assert "> 2 | if" in parsed.detail


def test_ruby_garbage_returns_none():
    assert parse_ruby_error("plain stderr chatter\n", 1) is None
    assert parse_ruby_error("", None) is None


# ---- ocaml ----

from tdb.languages.errors import parse_ocaml_error  # noqa: E402

OCAML_WITH_BACKTRACE = """\
Fatal error: exception Failure("boom")
Raised at Stdlib.failwith in file "stdlib.ml", line 29, characters 17-33
Called from Fatal.boom in file "ocaml_fatal.ml", line 1, characters 15-31
Called from Fatal.middle in file "ocaml_fatal.ml", line 2, characters 18-25
Called from Fatal in file "ocaml_fatal.ml", line 3, characters 9-18
"""

OCAML_NO_BACKTRACE = 'Fatal error: exception Failure("boom")\n'


def test_ocaml_error_with_backtrace():
    err = parse_ocaml_error(OCAML_WITH_BACKTRACE, 2)
    assert err is not None
    assert err.header == 'Fatal error: exception Failure("boom")'
    assert err.message == 'Failure("boom")'
    # OUTERMOST-first (source order), like python's parser
    assert [f.func for f in err.frames] == [
        "Fatal",
        "Fatal.middle",
        "Fatal.boom",
        "Stdlib.failwith",
    ]
    assert err.frames[0].path == "ocaml_fatal.ml"
    assert err.frames[0].line == 3
    assert "Raised at Stdlib.failwith" in err.detail


def test_ocaml_error_without_backtrace_has_hint():
    err = parse_ocaml_error(OCAML_NO_BACKTRACE, 2)
    assert err is not None
    assert err.frames == []
    assert "compile with -g" in err.detail


def test_ocaml_error_none_on_clean_output():
    assert parse_ocaml_error("all good\n", 0) is None
    assert parse_ocaml_error("", None) is None


def test_ocaml_reraised_and_inlined_frames():
    text = (
        "Fatal error: exception Not_found\n"
        'Raised by primitive operation at M.find in file "m.ml" (inlined),'
        " line 7, characters 1-9\n"
        'Re-raised at M.wrap in file "m.ml", line 12, characters 4-11\n'
    )
    err = parse_ocaml_error(text, 2)
    assert err is not None
    assert [f.func for f in err.frames] == ["M.wrap", "M.find"]
    assert err.frames[0].line == 12


# Test with real native output from Task 1 probe (Bonus finding):
# native compiles have (inlined) markers and bare module names
OCAML_NATIVE_WITH_INLINED = """\
Fatal error: exception Failure("boom")
Raised at Stdlib.failwith in file "stdlib.ml", line 29, characters 17-33
Called from Ocaml_fatal.boom in file "ocaml_fatal.ml" (inlined), line 1, characters 14-29
Called from Ocaml_fatal.middle in file "ocaml_fatal.ml" (inlined), line 2, characters 16-23
Called from Ocaml_fatal in file "ocaml_fatal.ml", line 3, characters 9-18
"""


def test_ocaml_native_with_inlined_and_bare_module_name():
    err = parse_ocaml_error(OCAML_NATIVE_WITH_INLINED, 2)
    assert err is not None
    assert err.header == 'Fatal error: exception Failure("boom")'
    assert err.message == 'Failure("boom")'
    # OUTERMOST-first: bare module name first, then inlined functions, then innermost
    assert [f.func for f in err.frames] == [
        "Ocaml_fatal",
        "Ocaml_fatal.middle",
        "Ocaml_fatal.boom",
        "Stdlib.failwith",
    ]
    assert err.frames[0].line == 3
    assert err.frames[1].line == 2
    assert err.frames[2].line == 1


# --- Regression tests for detail contract (excludes header line) ---


def test_ocaml_error_detail_excludes_header_with_backtrace():
    # Regression: detail should NOT contain the header line ("Fatal error:")
    # it starts after the header line's newline. The modal displays header
    # and detail as separate fields, so including header in detail would
    # show it twice.
    err = parse_ocaml_error(OCAML_WITH_BACKTRACE, 2)
    assert err is not None
    # Header is shown separately in the modal, so detail must not include it
    assert "Fatal error:" not in err.detail
    # Detail's first line should be the first backtrace line
    detail_lines = err.detail.split("\n")
    assert detail_lines[0].startswith("Raised at")


def test_ocaml_error_detail_excludes_header_without_backtrace():
    # Regression: for no-backtrace case, detail is ONLY the hint,
    # no header line, no leading blank lines.
    err = parse_ocaml_error(OCAML_NO_BACKTRACE, 2)
    assert err is not None
    assert "Fatal error:" not in err.detail
    # Detail should be exactly the hint text
    assert err.detail == "(no backtrace — compile with -g, e.g. dune's dev profile)"

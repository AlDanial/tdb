"""Language-specific fatal-error parsers behind Presentation.parse_error."""

from tdb.languages.errors import parse_python_error

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


def test_presentation_exposes_parser_for_python():
    from tdb.languages import registry

    profile = registry.resolve("python")
    assert profile.presentation.parse_error is not None
    assert profile.presentation.parse_error(SIMPLE) is not None


def test_presentation_parse_error_defaults_to_none():
    from tdb.languages.base import Presentation

    assert Presentation().parse_error is None


from tdb.languages.errors import parse_perl_error

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


def test_presentation_exposes_parser_for_perl():
    from tdb.languages import registry

    profile = registry.resolve("perl")
    assert profile.presentation.parse_error is not None
    assert profile.presentation.parse_error(PERL_RUNTIME) is not None

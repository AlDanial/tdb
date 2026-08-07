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

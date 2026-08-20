"""Ruby error parser tests."""

from tdb.languages.errors import parse_ruby_error


def test_parse_ruby_error_simple():
    """Simple Ruby error with backtrace."""
    stderr = """/tmp/test.rb:5:in `<main>': undefined method `foo' for main:Object (NoMethodError)
\tfrom /tmp/test.rb:5:in `<main>'"""
    result = parse_ruby_error(stderr, exit_code=1)
    assert result is not None
    assert "NoMethodError" in result.message
    assert len(result.frames) > 0
    assert result.frames[0].path == "/tmp/test.rb"
    assert result.frames[0].line == 5


def test_parse_ruby_error_no_backtrace():
    """No error if exit_code is 0."""
    stderr = "some output"
    result = parse_ruby_error(stderr, exit_code=0)
    assert result is None


def test_parse_ruby_error_no_stderr():
    """No error if stderr is empty."""
    result = parse_ruby_error("", exit_code=1)
    assert result is None


def test_parse_ruby_error_with_method():
    """Error with method name in backtrace."""
    stderr = """/app/lib/user.rb:42:in `initialize': wrong number of arguments (given 0, expected 1) (ArgumentError)
\tfrom /app/app.rb:10:in `new'"""
    result = parse_ruby_error(stderr, exit_code=1)
    assert result is not None
    assert len(result.frames) > 0
    # Frame should have the method name
    assert "initialize" in result.frames[0].func or "new" in result.frames[1].func


def test_parse_ruby_error_rails_style():
    """Rails-style error traceback."""
    stderr = """/app/app/controllers/users_controller.rb:15:in `index': User not found (ActiveRecord::RecordNotFound)
\tfrom /app/config/routes.rb:1:in `block (2 levels) in <main>'"""
    result = parse_ruby_error(stderr, exit_code=1)
    assert result is not None
    assert "ActiveRecord::RecordNotFound" in result.message or "RecordNotFound" in result.detail


def test_parse_ruby_error_header_and_message():
    """Error header and message are correctly set."""
    stderr = """/tmp/script.rb:3:in `<main>': divided by 0 (ZeroDivisionError)"""
    result = parse_ruby_error(stderr, exit_code=1)
    assert result is not None
    assert result.header == "Ruby exception:"
    assert "ZeroDivisionError" in result.message or "divided by 0" in result.message

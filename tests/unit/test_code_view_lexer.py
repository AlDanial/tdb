from rich.text import Text

from tdb.widgets.code_view import CodeView


def test_default_lexer_is_python():
    assert CodeView.lexer_name == "python"


def test_highlight_uses_instance_lexer():
    view = CodeView()
    view.lexer_name = "cpp"
    lines = view._highlight_source("int main() { return 0; }")
    assert isinstance(lines[0], Text)


def test_highlight_python_still_works():
    view = CodeView()
    lines = view._highlight_source("def f():\n    return 1\n")
    assert len(lines) >= 2

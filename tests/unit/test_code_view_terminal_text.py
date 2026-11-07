"""A source file is the debuggee user's to write. Under `sudo tdb -a PID`
its escape sequences must reach the screen as text, not as commands to the
admin's terminal; the file itself must stay as it is for Edit mode."""

from tdb.widgets.code_view import CodeView

OSC_52 = "\x1b]52;c;ZXZpbA==\x07"


def _view(source: str) -> CodeView:
    view = CodeView()
    view._install_source(source, "x.py")
    return view


def test_escapes_in_source_are_shown_not_sent():
    view = _view(f'a = "{OSC_52}"\nb = "\x9b2J"\n')
    for index in range(2):
        shown = view._line_text(index).plain
        assert not any(ch in shown for ch in "\x1b\x07\x9b"), repr(shown)
    assert "␛]52;c;ZXZpbA==␇" in view._line_text(0).plain


def test_the_source_itself_is_kept_for_editing():
    source = f'a = "{OSC_52}"\n'
    view = _view(source)
    assert view._source_text == source
    assert view._lines == [source.rstrip("\n")]


def test_tabs_are_left_alone():
    assert "␉" not in _view("a\t= 1\n")._line_text(0).plain


def test_placeholders_are_shown_safely_too():
    view = CodeView()
    view.load_placeholder(f"<Could not read {OSC_52}>", "x.py")
    assert "\x1b" not in view._line_text(0).plain

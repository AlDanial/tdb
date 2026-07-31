"""CodeView renders per-line on demand (Textual line API), not by
rebuilding the whole file's Text on every state change — stepping
through a 19k-line file must not pay for 19k lines per stop."""

from textual.app import App

from tdb.widgets.code_view import CodeView


def make_view(source: str = "a = 1\nb = 2\nc = 3\n") -> CodeView:
    view = CodeView()
    view._install_source(source, "x.py")
    return view


def test_line_text_renders_gutter_number_and_source():
    view = make_view()
    assert view._line_text(0).plain == "     1 a = 1"
    assert view._line_text(2).plain == "     3 c = 3"


def test_line_text_marks_breakpoint_lines():
    view = make_view()
    view._breakpoint_lines = {2}
    assert view._line_text(1).plain.startswith("●    2 ")
    assert view._line_text(0).plain.startswith("     1 ")


def test_line_text_styles_current_line():
    view = make_view()
    view.set_reactive(CodeView.current_line, 2)
    styled = view._line_text(1)
    assert any(
        span.style and "rgb(120,100,30)" in str(span.style) for span in styled.spans
    )
    unstyled = view._line_text(0)
    assert not any(
        span.style and "rgb(120,100,30)" in str(span.style) for span in unstyled.spans
    )


class _CVApp(App):
    def compose(self):
        yield CodeView(id="cv")


async def test_only_visible_window_is_rendered():
    app = _CVApp()
    async with app.run_test(size=(100, 24)) as pilot:
        cv = app.query_one("#cv", CodeView)
        calls: list[int] = []
        orig = CodeView._line_text

        cv._line_text = lambda index: calls.append(index) or orig(cv, index)
        cv.load_content("\n".join(f"x{i} = {i}" for i in range(5000)), "big.py")
        cv.current_line = 42
        await pilot.pause()

        assert calls, "nothing was rendered"
        # A 24-row viewport over a 5000-line file: only the visible
        # window (plus repaints from the reactive watchers) may be
        # materialized — never the whole file.
        assert len(calls) < 500, f"rendered {len(calls)} lines for a 24-row viewport"

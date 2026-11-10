"""CodeEditor(standalone=True): the vim scheme as a standalone editor
(`tdb --vi FILE`). Esc never leaves, Ctrl+B/Ctrl+F page, and only
Ctrl+Q / Ctrl+S stay with tdb."""

from __future__ import annotations

from typing import ClassVar

from textual.app import App
from textual.binding import Binding, BindingType

from tdb.vi import ViSession
from tdb.widgets.code_editor import CodeEditor, ViLayer

TEXT = "".join(f"line {i}\n" for i in range(200))


class _EdApp(App):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+b", "pane", show=False),
    ]

    def __init__(self, *, standalone: bool, text: str = TEXT):
        super().__init__()
        self._standalone = standalone
        self._text = text
        self.leave: list[bool] = []
        self.saves = 0
        self.panes = 0

    def compose(self):
        yield CodeEditor(self._text, scheme="vim", standalone=self._standalone, id="ed")

    def on_mount(self):
        self.ed = ed = self.query_one("#ed", CodeEditor)
        ed._set_layer(ViLayer(ed, session=ViSession()))
        ed.focus()

    def action_pane(self) -> None:
        self.panes += 1

    def on_code_editor_leave_requested(self, msg):
        self.leave.append(msg.discard)

    def on_code_editor_save_requested(self, msg):
        self.saves += 1


async def test_standalone_escape_does_not_leave():
    app = _EdApp(standalone=True)
    async with app.run_test() as pilot:
        await pilot.press("escape", "escape")
        await pilot.pause()
        assert app.leave == []
        assert app.ed.submode == "normal"
        assert app.focused is app.ed


async def test_standalone_ctrl_f_ctrl_b_page():
    app = _EdApp(standalone=True)
    async with app.run_test(size=(80, 40)) as pilot:
        ed = app.ed
        await pilot.press("ctrl+f")
        await pilot.pause()
        assert ed.cursor_location[0] > 20
        await pilot.press("ctrl+f")
        await pilot.pause()
        down = ed.cursor_location[0]
        top = ed.scroll_offset.y
        assert ed._layer.engine.cursor[0] == down
        await pilot.press("ctrl+b", "ctrl+b")
        await pilot.pause()
        # As in vim, ^B leaves the cursor on the new screen's last line.
        assert ed.cursor_location[0] < down
        assert ed.scroll_offset.y < top
        assert ed.visible_rows()[0] == 0


async def test_standalone_ctrl_b_not_app_binding():
    app = _EdApp(standalone=True)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+b", "ctrl+b")
        await pilot.pause()
        assert app.panes == 0


async def test_standalone_ctrl_s_still_saves():
    app = _EdApp(standalone=True)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.saves == 1


async def test_standalone_claims_ctrl_p_and_ctrl_c():
    app = _EdApp(standalone=True)
    async with app.run_test():
        assert app.ed.check_consume_key("ctrl+p", None)
        assert app.ed.check_consume_key("ctrl+c", None)


async def test_non_standalone_unchanged():
    app = _EdApp(standalone=False)
    async with app.run_test() as pilot:
        await pilot.press("escape")
        await pilot.pause()
        assert app.leave == [False]
        await pilot.press("ctrl+b")
        await pilot.pause()
        assert app.panes == 1
        assert not app.ed.check_consume_key("ctrl+p", None)
        assert not app.ed.check_consume_key("ctrl+c", None)

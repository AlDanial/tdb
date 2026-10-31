"""Vim scheme: the engine and the TextArea agree on what a line break is.

prompt_toolkit's Document breaks lines only at ``\\n``; Textual's (and
``str.splitlines``) also at ``\\r \\v \\f \\x1c \\x1d \\x1e \\x85 U+2028
U+2029``.  In the vim scheme those are ordinary characters of a line, as in
vim: the engine keeps them and the TextArea shows a one-character stand-in,
so rows and columns match and the file is saved byte for byte.  CRLF files
keep CRLF.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual import events

from tdb.widgets.code_editor import CodeEditor
from tdb.widgets.code_view import CodeView
from tests.unit.test_code_editor_vim import _EdApp, _send
from tests.unit.test_code_view_edit_mode import _CVApp

SEPARATORS = [" ", " ", "\x0c", "\x0b", "\x1c", "\x1d", "\x1e", "\x85"]


def _in_step(ed: CodeEditor) -> None:
    """The TextArea shows the engine's lines, one for one."""
    engine = ed._layer.engine  # type: ignore[union-attr]
    shown = ed.document.lines
    lines = engine.buffer.document.lines
    assert len(shown) == len(lines) + (1 if engine.buffer.eol else 0)
    assert [len(x) for x in shown[: len(lines)]] == [len(x) for x in lines]
    assert ed.file_text == engine.text


@pytest.mark.parametrize("sep", SEPARATORS, ids=[f"U+{ord(s):04X}" for s in SEPARATORS])
async def test_pasted_separator_is_a_character(sep):
    app = _EdApp("ab\ncd\nef\n")
    async with app.run_test() as pilot:
        ed = app.ed
        ed.post_message(events.Paste(f"X{sep}Y"))
        await pilot.pause()
        _in_step(ed)
        await _send(pilot, "G", "G")
        await _send(pilot, "x", "x")
        _in_step(ed)
        assert ed.file_text == f"X{sep}Yab\ncd\ne\n"


async def test_substitute_form_feed_stays_on_its_line():
    app = _EdApp("ab\ncd\nef\n")
    async with app.run_test() as pilot:
        ed = app.ed
        names = {"\\": "backslash", "/": "slash", ":": "colon"}
        for ch in ":s/a/\\f/":
            await _send(pilot, names.get(ch, ch), ch)
        await _send(pilot, "enter", "\r")
        _in_step(ed)
        for ch in "jdd":
            await _send(pilot, ch, ch)
        _in_step(ed)
        assert ed.file_text == "\x0cb\nef\n"


async def test_mirror_resyncs_a_widget_that_lost_step():
    app = _EdApp("ab\ncd\n")
    async with app.run_test() as pilot:
        ed = app.ed
        notes: list[str] = []
        app.notify = lambda msg, **kw: notes.append(msg)  # type: ignore[method-assign]
        ed.insert("Z\n", (0, 0))  # behind the engine's back
        await _send(pilot, "x", "x")
        assert ed.text == "b\ncd\n"
        assert notes and "resynced" in notes[-1]


def _vim_app() -> _CVApp:
    return _CVApp(scheme="vim")


def _write_bytes(tmp_path, data: bytes) -> str:
    p = tmp_path / "prog.txt"
    p.write_bytes(data)
    return str(p)


async def _edit_and_save(tmp_path, data: bytes, keys: str) -> bytes:
    app = _vim_app()
    async with app.run_test() as pilot:
        cv = app.query_one("#cv", CodeView)
        path = _write_bytes(tmp_path, data)
        cv.load_file(path)
        await pilot.pause()
        assert await cv.enter_edit_mode()
        await pilot.pause()
        for ch in keys:
            await _send(pilot, ch, ch)
        assert cv.save_file()
        await pilot.pause()
        return Path(path).read_bytes()


async def test_separators_in_the_file_round_trip(tmp_path):
    data = "a\x0cb\nc d\ne\x85f\x0bg\x1c\x1d\x1eh \nlast\n".encode()
    got = await _edit_and_save(tmp_path, data, "Gx")
    assert got == data.replace(b"last", b"ast")


async def test_crlf_file_keeps_crlf(tmp_path):
    got = await _edit_and_save(tmp_path, b"one\r\ntwo\r\nthree\r\n", "Gxggx")
    assert got == b"ne\r\ntwo\r\nhree\r\n"


async def test_crlf_file_shows_no_stray_carriage_return(tmp_path):
    app = _vim_app()
    async with app.run_test() as pilot:
        cv = app.query_one("#cv", CodeView)
        cv.load_file(_write_bytes(tmp_path, b"one\r\ntwo\r\n"))
        assert await cv.enter_edit_mode()
        await pilot.pause()
        ed = app.query_one(CodeEditor)
        assert ed.document.lines[:2] == ["one", "two"]
        assert "\r" not in ed._layer.engine.text  # type: ignore[union-attr]
        for ch in "A!":
            await _send(pilot, ch, ch)
        await _send(pilot, "escape", None)
        assert ed.file_text == "one!\r\ntwo\r\n"


async def test_mixed_line_endings_round_trip(tmp_path):
    # Not all CRLF: as vim (fileformat=unix), the \r is a character (shown).
    data = b"a\r\nb\nc\r\n"
    got = await _edit_and_save(tmp_path, data, "jx")
    assert got == b"a\r\n\nc\r\n"


async def test_revert_to_disk_keeps_separators(tmp_path):
    app = _vim_app()
    async with app.run_test() as pilot:
        cv = app.query_one("#cv", CodeView)
        path = _write_bytes(tmp_path, b"a\x0cb\nc\r\n")
        cv.load_file(path)
        assert await cv.enter_edit_mode()
        await pilot.pause()
        ed = app.query_one(CodeEditor)
        await _send(pilot, "x", "x")
        assert ed.is_dirty
        assert cv.revert_to_disk()
        await pilot.pause()
        assert ed.file_text == "a\x0cb\nc\r\n" and not ed.is_dirty
        _in_step(ed)
        await _send(pilot, "u", "u")  # the revert is undoable
        assert ed.file_text == "\x0cb\nc\r\n"

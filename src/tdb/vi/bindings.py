"""tdb's prompt_toolkit key bindings layered over the built-in vi bindings.

Bindings returned here are passed as the engine Application's
``key_bindings`` and so take priority over prompt_toolkit's own vi
bindings for the same key sequence.

The Ctrl+R redo binding is adapted from pyvim (``pyvim/key_bindings.py``):

    Copyright (c) 2015, Jonathan Slenders
    All rights reserved.

    Redistribution and use in source and binary forms, with or without
    modification, are permitted provided that the following conditions are
    met:

    * Redistributions of source code must retain the above copyright
      notice, this list of conditions and the following disclaimer.

    * Redistributions in binary form must reproduce the above copyright
      notice, this list of conditions and the following disclaimer in the
      documentation and/or other materials provided with the distribution.

    * Neither the name of the {organization} nor the names of its
      contributors may be used to endorse or promote products derived from
      this software without specific prior written permission.

    THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS
    IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED
    TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
    PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
    HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
    SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED
    TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
    PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
    LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
    NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
    SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from prompt_toolkit.application.current import get_app
from prompt_toolkit.filters import (
    Condition,
    is_read_only,
    vi_navigation_mode,
    vi_selection_mode,
)
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.vi import (
    TextObject,
    TextObjectType,
    create_operator_decorator,
    create_text_object_decorator,
)
from prompt_toolkit.key_binding.vi_state import InputMode

if TYPE_CHECKING:
    from prompt_toolkit.key_binding import KeyPressEvent

    from tdb.vi.engine import ViEngine


def load_tdb_vi_bindings(engine: ViEngine) -> KeyBindings:
    """Key bindings that fix or extend prompt_toolkit's vi mode for *engine*."""
    kb = KeyBindings()

    @kb.add("c-r", filter=vi_navigation_mode, save_before=lambda e: False)
    def _redo(event: KeyPressEvent) -> None:
        """Redo (pyvim)."""
        for _ in range(event.arg):
            event.app.current_buffer.redo()

    @kb.add("c-v", filter=vi_navigation_mode | vi_selection_mode)
    def _no_block_visual(event: KeyPressEvent) -> None:
        """Block visual mode is unsupported: Ctrl+V does nothing."""

    _add_change_word(kb)

    return kb


def _add_change_word(kb: KeyBindings) -> None:
    """vim's ``cw``/``cW`` special case, missing from prompt_toolkit.

    With the cursor on a non-blank, ``cw`` changes to the end of the word
    (like ``ce``) instead of also eating the following white space.  This
    needs a tdb-owned ``c`` operator so the ``w``/``W`` text objects can
    tell a change apart from ``d``/``y``.
    """
    operator = create_operator_decorator(kb)
    text_object = create_text_object_decorator(kb)

    @operator("c", filter=~is_read_only)
    def change(event: KeyPressEvent, text_obj: TextObject) -> None:
        """``c{motion}`` (same as prompt_toolkit's, minus registers)."""
        buff = event.current_buffer
        new_document, clipboard_data = text_obj.cut(buff)
        buff.document = new_document
        if clipboard_data.text:
            event.app.clipboard.set_data(clipboard_data)
        event.app.vi_state.input_mode = InputMode.INSERT

    changing = Condition(lambda: get_app().vi_state.operator_func is change)

    def change_word(event: KeyPressEvent, *, WORD: bool) -> TextObject:
        doc = event.current_buffer.document
        if doc.current_char and not doc.current_char.isspace():
            end = doc.find_next_word_ending(
                count=event.arg, include_current_position=True, WORD=WORD
            )
            if end:
                return TextObject(end - 1, type=TextObjectType.INCLUSIVE)
        return TextObject(
            doc.find_next_word_beginning(count=event.arg, WORD=WORD)
            or doc.get_end_of_document_position()
        )

    @text_object("w", filter=changing, no_move_handler=True, no_selection_handler=True)
    def _change_word(event: KeyPressEvent) -> TextObject:
        return change_word(event, WORD=False)

    @text_object("W", filter=changing, no_move_handler=True, no_selection_handler=True)
    def _change_WORD(event: KeyPressEvent) -> TextObject:
        return change_word(event, WORD=True)

"""The engine's speed-up of prompt_toolkit's ``Document`` line tables.

``tdb.vi.engine`` replaces three ``Document`` methods and fills
``Document`` caches itself, through private prompt_toolkit names.  On the
pinned versions the patch must be active; when the names are missing
(a prompt_toolkit that renamed them) the engine must leave ``Document``
alone, and still edit correctly on prompt_toolkit's own (slow) path.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import prompt_toolkit
import prompt_toolkit.document as ptk_document
import pytest
from prompt_toolkit.document import Document

from tdb.vi import ViEngine, engine

PATCHED = (
    "_line_start_indexes",
    "_find_line_start_index",
    "translate_row_col_to_index",
)


def test_patch_is_active_on_the_pinned_prompt_toolkit():
    assert prompt_toolkit.__version__.startswith("3.0.")
    assert engine._PTK is not None
    assert Document.__dict__["_line_start_indexes"].fget is engine._line_start_indexes
    assert Document._find_line_start_index is engine._find_line_start_index
    assert Document.translate_row_col_to_index is engine._translate_row_col_to_index


async def test_edits_use_the_derived_line_table():
    e = ViEngine("one\ntwo\nthree\n")
    e.feed_keys("jxx")  # (the first edit computes the old text's table)
    cache = ptk_document._text_to_document_cache[e.buffer.text]
    assert type(cache.line_indexes) is engine._LineStarts


@pytest.mark.parametrize(
    "name", ["_DocumentCache", "_ImmutableLineList", "_text_to_document_cache"]
)
def test_missing_module_internals_disable_the_patch(monkeypatch, name):
    monkeypatch.delattr(ptk_document, name)
    assert engine._document_internals() is None


@pytest.mark.parametrize("name", PATCHED)
def test_missing_document_method_disables_the_patch(monkeypatch, name):
    monkeypatch.delattr(Document, name)
    assert engine._document_internals() is None


def test_document_without_a_cache_slot_disables_the_patch(monkeypatch):
    monkeypatch.setattr(Document, "__slots__", ("_text", "_cursor_position"))
    assert engine._document_internals() is None


# One session of edits that go through every user of the derived table:
# typing, linewise and charwise changes, undo/redo, marks, :g with
# :normal, :%normal, a macro.
SCRIPT = [
    "jmaggOnew<Esc>",
    "'aAx<Esc>",
    "5Gdd",
    ":%normal A;<CR>",
    ":g/two/normal ddp<CR>",
    "u",
    "<C-r>",
    "qaI#<Esc>jq2@a",
    "'a",
    "gg5x",
    "u",
]


async def run_script() -> tuple[ViEngine, list[tuple[str, tuple[int, int]]]]:
    e = ViEngine("one\ntwo\nthree\nfour\nfive\nsix\n")
    states = []
    for keys in SCRIPT:
        e.feed_keys(keys)
        states.append((e.text, e.cursor))
    return e, states


async def test_fallback_edits_like_the_patched_engine(monkeypatch):
    with monkeypatch.context() as m:  # first: no table the patch made is cached
        m.setattr(engine, "_PTK", None)
        for name, original in engine._DOCUMENT_ORIGINALS.items():
            m.setattr(Document, name, original)
        assert Document.translate_row_col_to_index is not (
            engine._translate_row_col_to_index
        )
        e, slow = await run_script()
        doc = e.buffer.document
        assert type(doc._line_start_indexes) is list  # prompt_toolkit's own table
    _, fast = await run_script()
    assert slow == fast
    # (checked against nvim)
    assert slow[-1] == ("new;\none;\n#three;\n#twox;\n#five;\nsix;\n", (0, 0))


def test_tdb_starts_with_the_internals_renamed():
    """A prompt_toolkit whose private names were renamed (its
    ``document.py`` loaded with them renamed throughout, so prompt_toolkit
    itself still works): importing tdb's UI, which imports tdb.vi, works,
    Document is not patched and the engine edits."""
    code = textwrap.dedent(
        """
        import asyncio, importlib.abc, importlib.util, os, sys

        RENAMED = ("_DocumentCache", "_ImmutableLineList",
                   "_text_to_document_cache", "_line_start_indexes")
        base = importlib.util.find_spec("prompt_toolkit").submodule_search_locations
        PATH = os.path.join(base[0], "document.py")

        class Renamer(importlib.abc.MetaPathFinder, importlib.abc.Loader):
            def find_spec(self, name, path, target=None):
                if name == "prompt_toolkit.document":
                    return importlib.util.spec_from_file_location(
                        name, PATH, loader=self)
                return None

            def create_module(self, spec):
                return None

            def exec_module(self, module):
                with open(PATH, encoding="utf-8") as f:
                    src = f.read()
                for old in RENAMED:
                    src = src.replace(old, old + "_v2")
                exec(compile(src, PATH, "exec"), module.__dict__)

        sys.meta_path.insert(0, Renamer())
        import prompt_toolkit.document as d
        assert not hasattr(d, "_DocumentCache")
        original = d.Document.translate_row_col_to_index
        import tdb.widgets.code_view  # tdb's UI imports tdb.vi eagerly
        from tdb.vi import ViEngine, engine
        assert engine._PTK is None
        assert d.Document.translate_row_col_to_index is original

        async def main():
            e = ViEngine("a\\nb\\nc\\n")
            e.feed_keys("jddp:%normal A!<CR>u")
            print(repr(e.text))

        asyncio.run(main())
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == repr("a\nc\nb\n")

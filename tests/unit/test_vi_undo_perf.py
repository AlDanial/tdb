"""Undo history inside undo groups stays small (no per-change full-text copies).

While a group is open (a `.` change or insert session, a macro run, an ex
command such as ``:normal`` or ``:g``) only the state before the group is
kept, as vim does: ``u`` inside a macro or ``:normal`` undoes the whole
block so far (checked against nvim with typed input).
"""

from __future__ import annotations

import time
import tracemalloc

import pytest

from tdb.vi import ViEngine, ViKey


def _numbered(n: int) -> str:
    return "".join(f"line {i} = value_{i}\n" for i in range(n))


def _seconds(keys: str, text: str) -> tuple[float, ViEngine]:
    e = ViEngine(text)
    start = time.perf_counter()
    e.feed_keys(keys)
    return time.perf_counter() - start, e


def _growth(keys: str, limit: float) -> ViEngine:
    """How much longer *keys* takes on 8,000 lines than on 1,000, which is
    the same on any machine (seconds are not: musl's allocator and a CI
    runner made a 1.9 s run 10.9 s).  Linear is 8x and quadratic 64x; the
    per-change text copies these tests guard against measured 29-56x, and
    the fix 12-21x (glibc and Alpine).  Prompt_toolkit's own per-edit text
    copy and hash keep it above 8x.  One retry, for a busy runner."""
    for _ in range(2):
        small = min(_seconds(keys, _numbered(1_000))[0] for _ in range(3))
        took, e = _seconds(keys, _numbered(8_000))
        if took / small < limit:
            break
    assert took / small < limit, (
        f"{keys!r}: 8,000 lines took {took / small:.1f}x as long as 1,000"
        f" ({took:.2f}s / {small:.3f}s)"
    )
    return e


def test_percent_normal_time_grows_linearly():
    # (2026-10-31, 10,000 lines: 6.4 s before the fix, 1.85 s after)
    e = _growth(":%normal Ax<CR>", 22.0)
    assert e.text == _numbered(8_000).replace("\n", "x\n")


def test_global_normal_time_grows_linearly():
    # (2026-10-31, 20,000 lines: 13.6 s before the fix, 2.26 s after)
    e = _growth(":g/7/normal Ax<CR>", 32.0)
    text = _numbered(8_000)
    want = "".join(
        line + ("x" if "7" in line else "") + "\n" for line in text.splitlines()
    )
    assert e.text == want
    e.feed_keys("u")
    assert e.text == text


def test_percent_normal_on_10k_lines_memory():
    text = _numbered(10_000)
    e = ViEngine(text)
    tracemalloc.start()
    try:
        e.feed_keys(":%normal Ax<CR>")
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 300 * 2**20, f"peaked at {peak / 2**20:.0f} MB"
    assert e.text == text.replace("\n", "x\n")
    e.feed_keys("u")
    assert e.text == text


def _check_table(text: str, rng) -> None:
    """The shared Document cache of *text* equals a fresh one."""
    import bisect

    from prompt_toolkit.document import Document, _text_to_document_cache

    cache = _text_to_document_cache[text]
    assert cache.lines is not None
    assert list(cache.lines) == text.split("\n")
    want, pos = [], 0
    for line in text.split("\n"):
        want.append(pos)
        pos += len(line) + 1
    doc = Document(text)
    starts = doc._line_start_indexes
    assert [starts[i] for i in range(len(starts))] == want
    assert starts[-1] == want[-1]
    for index in {0, len(text), rng.randint(0, len(text))}:
        row = bisect.bisect_right(want, index) - 1
        assert doc.translate_index_to_position(index) == (row, index - want[row])
    fresh = Document(text)
    fresh._cache = type(cache)()
    for row in (-1, 0, len(want) - 1, len(want), rng.randint(0, len(want))):
        assert doc.translate_row_col_to_index(row, 2) == (
            fresh.translate_row_col_to_index(row, 2)
        )


def test_derived_document_cache_matches_a_fresh_one():
    # The engine's buffer derives each new text's lines and line starts
    # from the previous text's; they must equal prompt_toolkit's own,
    # also for an older text gone back to (as undo does), whose table a
    # later one shares.
    import random

    from prompt_toolkit.document import Document

    rng = random.Random(7)
    e = ViEngine("ab\n\ncd\nef\n")
    buff = e.buffer
    buff.text = buff.text + "z"  # the first change has nothing to derive from
    history: list[str] = []
    for _ in range(3000):
        if history and rng.random() < 0.2:
            buff.text = rng.choice(history)
            _check_table(buff.text, rng)
        text = buff.text
        history.append(text)
        a = rng.randint(0, len(text))
        b = rng.randint(a, min(len(text), a + rng.choice((0, 1, 3, 12))))
        piece = "".join(rng.choice("x\n\n y") for _ in range(rng.randint(0, 4)))
        # The old text's cache is filled, as when the engine runs.
        _ = buff.document, Document(text).cursor_position_row
        buff.text = text[:a] + piece + text[b:]
        _check_table(buff.text, rng)


def test_global_normal_is_one_step():
    text = _numbered(3_000)
    e = ViEngine(text)
    e.feed_keys(":g/7/normal Ax<CR>")
    assert len(e.buffer._undo_stack) == 1
    want = "".join(
        line + ("x" if "7" in line else "") + "\n" for line in text.splitlines()
    )
    assert e.text == want
    e.feed_keys("u")
    assert e.text == text


def test_macro_run_keeps_one_undo_entry():
    text = _numbered(2_000)
    e = ViEngine(text)
    e.feed_keys("qaA;<Esc>jq")
    before = len(e.buffer._undo_stack)
    e.feed_keys("1500@a")
    assert len(e.buffer._undo_stack) <= before + 1
    e.feed_keys("u")
    assert e.text == text.replace("line 0 = value_0", "line 0 = value_0;")


def test_insert_session_keeps_one_undo_entry():
    text = _numbered(1_000)
    e = ViEngine(text, cursor=(500, 0))
    e.feed(ViKey("i"))
    for i in range(500):
        e.feed(ViKey("abcdefgh"[i % 8]))
        assert len(e.buffer._undo_stack) <= 1
    e.feed_keys("<Esc>u")
    assert e.text == text


def _with_register(text: str, reg: str) -> ViEngine:
    from prompt_toolkit.clipboard import ClipboardData

    e = ViEngine(text)
    e.session.vi_state.named_registers["m"] = ClipboardData(reg)  # type: ignore[attr-defined]
    return e


@pytest.mark.parametrize(
    ("reg", "typed", "expected"),
    [
        # nvim (typed input): u inside a macro or :normal undoes the whole
        # unsynced block so far; a typed change before it is its own step.
        ("xxu", "@m", "abcd\n"),
        ("xxuu", "@m", "abcd\n"),
        ("xxu\x12", "@m", "cd\n"),
        ("xiZ\x1bu", "@m", "abcd\n"),
        ("xu", "x@m", "bcd\n"),
        ("", ":normal xxu<CR>", "abcd\n"),
        ("", "x:normal xxuu<CR>", "abcd\n"),
    ],
)
async def test_undo_inside_a_replay_undoes_the_block(reg, typed, expected):
    e = _with_register("abcd\n", reg)
    e.feed_keys(typed)
    assert e.text == expected


async def test_counted_redo_stays_per_step():
    # Redo pushes a step even inside a group; motions and counts keep the
    # redo history (prompt_toolkit cleared it before every key).
    e = ViEngine("abcd\n")
    e.feed_keys("xxxuuu3<C-r>u")
    assert e.text == "cd\n"

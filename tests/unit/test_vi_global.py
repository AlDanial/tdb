"""``:g`` / ``:v``: an ex command on every matching (or non-matching) line.

Behaviour checked against nvim 0.11 (the ``global-*`` golden cases cover
the same commands through ``test_vi_compat``).
"""

from __future__ import annotations

import time

import pytest

from tdb.vi import Notify, ViEngine

TEXT = "  ax\nb\n  cx\ndx\n  e\n"


def run(keys: str, text: str = TEXT, cursor: tuple[int, int] = (4, 1)) -> ViEngine:
    e = ViEngine(text, cursor=cursor)
    e.feed_keys(keys)
    return e


def test_global_delete():
    e = run(":g/x/d<CR>")
    assert (e.text, e.cursor) == ("b\n  e\n", (1, 2))
    assert e.drain_events() == []


def test_vglobal_delete():
    e = run(":v/x/d<CR>")
    assert (e.text, e.cursor) == ("  ax\n  cx\ndx\n", (2, 0))


def test_global_bang_is_vglobal():
    assert run(":g!/x/d<CR>").text == run(":v/x/d<CR>").text == "  ax\n  cx\ndx\n"


def test_global_substitute_reports_once():
    e = run(":g/x/s/x/y/<CR>")
    assert (e.text, e.cursor) == ("  ay\nb\n  cy\ndy\n  e\n", (3, 0))
    assert e.drain_events() == [Notify("3 substitutions on 3 lines")]


def test_global_substitute_g_flag():
    e = run(":g/x/s/a/b/g<CR>", text="ax aa\nb a\nxa\n", cursor=(0, 0))
    assert e.text == "bx bb\nb a\nxb\n"


def test_global_substitute_not_found_on_some_lines_is_no_error():
    # nvim: the cursor ends on the last marked line, at its first non-blank
    # (some :s did substitute), and nothing is reported.
    e = run(":g/x/s/a/b/<CR>", text="  ax\nb\n  cx\n  dx\n  e\n")
    assert (e.text, e.cursor) == ("  bx\nb\n  cx\n  dx\n  e\n", (3, 2))
    assert e.drain_events() == []


def test_global_substitute_never_found_is_silent():
    e = run(":g/x/s/zz/y/<CR>")
    assert (e.text, e.cursor) == (TEXT, (3, 0))
    assert e.drain_events() == []
    assert e.session.last_search[0] == "zz"


def test_global_normal_append():
    e = run(":g/def/normal A:<CR>", text="def f():\n  pass\ndef g()\n", cursor=(0, 0))
    assert (e.text, e.cursor) == ("def f()::\n  pass\ndef g():\n", (2, 7))


def test_global_move_to_top_reverses_matching_lines():
    e = run(":g/x/m0<CR>")
    assert (e.text, e.cursor) == ("dx\n  cx\n  ax\nb\n  e\n", (0, 1))


def test_global_move_to_end():
    e = run(":g/x/m$<CR>")
    assert (e.text, e.cursor) == ("b\n  e\n  ax\n  cx\ndx\n", (4, 1))


def test_global_copy_to_end():
    e = run(":g/x/t$<CR>")
    assert e.text == TEXT + "  ax\n  cx\ndx\n"
    assert e.cursor == (7, 1)


def test_global_yank_lowercase_register_keeps_the_last_line():
    e = run(":g/x/y a<CR>")
    assert (e.text, e.cursor) == (TEXT, (3, 0))
    assert e.session.vi_state.named_registers["a"].text == "dx"


def test_global_yank_appends_to_cleared_register():
    # As nvim: ``qaq`` leaves an empty characterwise register, so the
    # appended lines start with an empty one.
    e = run("qaq:g/x/y A<CR>")
    assert e.session.vi_state.named_registers["a"].type.name == "LINES"
    e.feed_keys('G"ap')
    assert e.text == TEXT + "\n  ax\n  cx\ndx\n"


def test_global_default_range_and_explicit_range():
    e = run(":g/x/d<CR>")
    assert e.text == "b\n  e\n"
    e = run(":1,3g/x/d<CR>")
    assert e.text == "b\ndx\n  e\n"
    e = run(":2,3g/x/d<CR>")
    assert (e.text, e.cursor) == ("  ax\nb\ndx\n  e\n", (2, 0))


def test_global_empty_command_is_an_error():
    e = run(":g/x/<CR>")
    assert e.text == TEXT
    assert e.drain_events() == [
        Notify(":global without a command is not supported", "error")
    ]


@pytest.mark.parametrize(
    ("keys", "message"),
    [
        (":g/zz/d<CR>", "Pattern not found: zz"),
        (":v/./d<CR>", "Pattern found in every line: ."),
    ],
)
def test_global_no_matching_line(keys, message):
    e = run(keys)
    assert e.text == TEXT
    assert e.drain_events() == [Notify(message, "warning")]


@pytest.mark.parametrize(
    ("keys", "message"),
    [
        (
            ":g/(/d<CR>",
            "Invalid pattern: missing ), unterminated subpattern at position 0",
        ),
        (":g//d<CR>", "No previous regular expression"),
        (":g xaxd<CR>", "Regular expressions can't be delimited by letters"),
        (":g<CR>", "No previous regular expression"),
        (":g/x/bogus<CR>", "Not an editor command: bogus"),
        (":v!/x/d<CR>", "No ! allowed"),
        (":g/x/s/x/y/c<CR>", "The c flag of :s is not supported inside :global"),
        (":g/x/1,2g/a/d<CR>", "Cannot do :global recursive with a range"),
    ],
)
def test_global_errors_change_nothing(keys, message):
    e = run(keys)
    assert e.text == TEXT
    assert e.drain_events() == [Notify(message, "error")]
    assert e.mode == "normal"


def test_global_empty_pattern_is_the_last_search():
    e = run("/c<CR>:g//d<CR>")
    assert e.text == "  ax\nb\ndx\n  e\n"


def test_global_sets_the_last_search():
    e = run(":g/c/d<CR>nx", text="ac\nb\nc\nbc\n", cursor=(1, 0))
    assert e.session.last_search[0] == "c"


def test_global_is_one_undo_step():
    e = run(":g/x/s/x/y/<CR>")
    e.feed_keys("u")
    assert (e.text, e.cursor) == (TEXT, (0, 0))
    e.feed_keys("<C-r>")
    assert e.text == "  ay\nb\n  cy\ndy\n  e\n"
    e = run(":g/x/d<CR>u")
    assert (e.text, e.cursor) == (TEXT, (0, 1))
    e = run(":g/def/normal A:<CR>u", text="def f()\nx\ndef g()\n", cursor=(1, 0))
    assert e.text == "def f()\nx\ndef g()\n"


def test_global_no_change_leaves_no_undo_step():
    e = run("x:g/x/y<CR>u", text="abc\nx\n", cursor=(0, 0))
    assert e.text == "abc\nx\n"


def test_global_on_empty_buffer():
    e = run(":g/x/d<CR>", text="", cursor=(0, 0))
    assert e.text == ""
    assert e.drain_events() == [Notify("Pattern not found: x", "warning")]
    for keys in (":v/x/d<CR>", ":g/^/d<CR>", ":g/^/s/^/a/<CR>u", ":v/x/normal dd<CR>"):
        e = run(keys, text="", cursor=(0, 0))
        assert e.text == ""
        assert not [n for n in e.drain_events() if n.severity == "error"]


def test_global_marks_lines_first():
    # Adjacent matching lines: deleting one moves the next up onto the
    # deleted line's row, and it is still marked.
    e = run(":g/x/d<CR>", text="x\nx\nx\ny\nx\n", cursor=(0, 0))
    assert e.text == "y\n"
    # A line deleted by an earlier run is not visited.
    e = run(":g/x/.,+1d<CR>")
    assert (e.text, e.cursor) == ("  e\n", (0, 2))
    # Lines that match only after the command ran are not visited.
    e = run(":g/x/normal oxx<CR>", text="x\ny\n", cursor=(0, 0))
    assert e.text == "x\nxx\ny\n"


@pytest.mark.parametrize(
    ("text", "keys", "want"),
    [
        # Deleting identical lines other than the run's own (nvim 0.11.6).
        ("x\nx\n", ":g/x/+1d<CR>", "x\n"),
        ("x\nx\nx\nx\n", ":g/x/+1d<CR>", "x\nx\n"),
        ("x\nx\nx\nx\n", ":g/x/-1d<CR>", "\n"),
        ("x\nx\n", ":g/x/normal jdd<CR>", "x\n"),
        ("x\nx\nx\nx\n", ":g/x/normal jdd<CR>", "x\nx\n"),
        ("x\nx\nx\nx\n", ":g/x/normal dk<CR>", "\n"),
        ("x\nx\nx\nx\n", ":g/x/normal dd<CR>", "\n"),
        ("a\n\n\n\nb\n\n", ":g/^$/normal dd<CR>", "a\nb\n"),
        ("x\nx\ny\n", ":g/x/normal yyp<CR>", "x\nx\nx\nx\ny\n"),
    ],
)
def test_global_follows_identical_lines_exactly(text, keys, want):
    e = run(keys, text=text, cursor=(0, 0))
    assert e.text == want
    assert not [n for n in e.drain_events() if n.severity == "error"]


def test_global_copy_here_ends_on_the_last_copy():
    assert run(":g/x/t.<CR>", text="x\nx\n", cursor=(0, 0)).cursor == (3, 0)
    e = run(":g/x/t.<CR>", text="x\nx\nx\nx\n", cursor=(0, 0))
    assert (e.text, e.cursor) == ("x\n" * 8, (7, 0))


def test_global_marks_follow_moved_lines():
    # Each moved line keeps its mark: every line is moved exactly once.
    e = run(":g/x/m$<CR>", text="x1\nx2\nx3\ny\n", cursor=(0, 0))
    assert e.text == "y\nx1\nx2\nx3\n"


def test_global_marks_a_to_z_follow_their_lines():
    # As nvim: a mark on a deleted line is gone; the others move with
    # their lines, also when :m reorders them.
    e = run("ggjmbjjmajmc:g/x/d<CR>")
    assert e.marks.get("a") is None
    assert (e.marks.get("b")[0], e.marks.get("c")[0]) == (0, 1)
    e = run("jmbjmajmc:g/x/m0<CR>", text="x1\nb\nx2\nc\n", cursor=(0, 0))
    assert e.text == "x2\nx1\nb\nc\n"
    assert [e.marks.get(m) for m in "abc"] == [(0, 0), (2, 0), (3, 0)]


def test_global_sets_the_jump_mark_once():
    e = run(":g/x/s/x/y/<CR>")
    assert e.marks.get("'") == (4, 1)
    e = run(":g/x/d<CR>``")
    assert e.cursor == (1, 1)


def test_global_nested_without_range_runs_on_the_current_line():
    # nvim: a :g inside :g checks only the line it runs on.
    e = run(":g/x/g/a/d<CR>")
    assert e.text == "b\n  cx\ndx\n  e\n"
    e = run(":g/x/normal :g/a/d<lt>CR><CR>")
    assert e.text == "b\n  cx\ndx\n  e\n"


def test_global_split_lines_keep_the_marks_of_later_lines():
    e = run(r":g/x/s/x/\r/<CR>", text="ax\nbx\n", cursor=(0, 0))
    assert e.text == "a\n\nb\n\n"


def test_global_performance():
    # The brief's limit; one retry so a busy CI runner's hiccup is not a
    # failure (a real slowdown fails both tries).
    text = "".join(f"line {i} value {i * 13}\n" for i in range(5000))
    times = []
    for _ in range(2):
        e = ViEngine(text)
        start = time.perf_counter()
        e.feed_keys(":g/7/s/1/2/<CR>")
        times.append(time.perf_counter() - start)
        assert e.text != text
        e.feed_keys("u")
        assert e.text == text
        if times[-1] < 1.0:
            break
    assert times[-1] < 1.0, f":g took {', '.join(f'{t:.2f}s' for t in times)}"

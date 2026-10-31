"""tdb.vi.ex: ex command parsing, ranges and the basic commands (fake host)."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from tdb.vi.ex import ExCommand, ExError, ExRange, parse_ex, resolve_range, run_ex


class FakeHost:
    """An ``ExHost`` over a list of lines; records what the commands do."""

    def __init__(self, text: str = "l1\nl2\nl3\nl4\nl5", row: int = 0, col: int = 0):
        self._lines = text.split("\n")
        self.row, self.col = row, col
        self.marks: dict[str, tuple[int, int]] = {}
        self.registers: dict[str | None, tuple[str, bool, bool]] = {}
        self.events: list[object] = []
        self.normal: list[tuple[int | None, str]] = []
        self.row_maps: list[object] = []
        self.groups = 0
        self.search: str | None = None

    def lines(self) -> list[str]:
        return list(self._lines)

    def cursor_row(self) -> int:
        return self.row

    def cursor_col(self) -> int:
        return self.col

    def mark(self, name: str) -> tuple[int, int] | None:
        return self.marks.get(name)

    def replace_lines(self, lines, cursor, *, row_map=None, undo_cursor=None):
        self._lines = list(lines) or [""]
        self.row, self.col = cursor
        self.row_maps.append(row_map)

    def set_register(self, name, text, linewise, *, yank=False):
        self.registers[name] = (text, linewise, yank)

    def run_normal(self, row, keys):
        self.normal.append((row, keys))
        if keys == "dd":
            del self._lines[row if row is not None else self.row]
        elif keys == "A;":
            r = row if row is not None else self.row
            self._lines[r] += ";"

    def last_search(self):
        return self.search

    def set_last_search(self, pattern):
        self.search = pattern

    def save(self):
        self.events.append("save")

    def leave(self, discard):
        self.events.append(("leave", discard))

    def notify(self, text, severity="information"):
        self.events.append((text, severity))

    def goto_line(self, row):
        self.events.append(("goto", row))
        self.row = row

    @contextmanager
    def undo_group(self):
        self.groups += 1
        yield


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("10,20d", ExCommand(ExRange("10", "20"), "delete", False, "")),
        ("'a,'bm0", ExCommand(ExRange("'a", "'b"), "move", False, "0")),
        ("%normal Ax", ExCommand(ExRange("%", None), "normal", False, "Ax")),
        (".,$-1y a", ExCommand(ExRange(".", "$-1"), "yank", False, "a")),
        ("q!", ExCommand(None, "quit", True, "")),
        ("  wq  ", ExCommand(None, "wq", False, "")),
        ("5", ExCommand(ExRange("5", None), "", False, "")),
        ("1t0", ExCommand(ExRange("1", None), "copy", False, "0")),
        ("co$", ExCommand(None, "copy", False, "$")),
        ("2,3copy 4", ExCommand(ExRange("2", "3"), "copy", False, "4")),
        (",4d", ExCommand(ExRange(".", "4"), "delete", False, "")),
        ("::3", ExCommand(ExRange("3", None), "", False, "")),
        ("norm! x ", ExCommand(None, "normal", True, "x ")),
        ("g!/x/d", ExCommand(None, "global", True, "/x/d")),
        ("v/x/d", ExCommand(None, "vglobal", False, "/x/d")),
        ("s/a/b/", ExCommand(None, "substitute", False, "/a/b/")),
        ("&&", ExCommand(None, "&", False, "&")),
        ("ma", ExCommand(None, "ma", False, "")),
        ("x", ExCommand(None, "xit", False, "")),
    ],
)
def test_parse_ex(line, expected):
    assert parse_ex(line) == expected


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        ("3", None, (2, 2)),
        (".", None, (1, 1)),
        ("$", None, (4, 4)),
        ("%", None, (0, 4)),
        (".+2", None, (3, 3)),
        (".-1", None, (0, 0)),
        ("$-1", None, (3, 3)),
        ("+", None, (2, 2)),
        ("'a", None, (3, 3)),
        ("'<", "'>", (1, 2)),
        ("2", "4", (1, 3)),
        ("4", "2", (1, 3)),  # backwards: swapped silently
        ("0", None, (0, 0)),
        (".", "$", (1, 4)),
    ],
)
def test_resolve_range_addresses(start, end, expected):
    host = FakeHost(row=1)
    host.marks = {"a": (3, 1), "<": (1, 0), ">": (2, 2**31 - 1)}
    assert resolve_range(ExRange(start, end), host, "current") == expected


def test_resolve_range_defaults():
    host = FakeHost(row=2)
    assert resolve_range(None, host, "current") == (2, 2)
    assert resolve_range(None, host, "all") == (0, 4)


@pytest.mark.parametrize(
    ("rng", "message"),
    [
        (ExRange("9", None), "Invalid range"),
        (ExRange("1", "9"), "Invalid range"),
        (ExRange(".-3", None), "Invalid range"),
        (ExRange("'a", None), "Mark not set: a"),
        (ExRange("1", "'z"), "Mark not set: z"),
    ],
)
def test_resolve_range_errors(rng, message):
    with pytest.raises(ExError, match=f"^{message}$"):
        resolve_range(rng, FakeHost(row=1), "current")


def test_delete_range_goes_to_unnamed_register():
    host = FakeHost("  l1\n  l2\n  l3\n  l4\n  l5")
    run_ex("3,4d", host)
    assert host.lines() == ["  l1", "  l2", "  l5"]
    assert host.registers == {None: ("  l3\n  l4", True, False)}
    assert (host.row, host.col) == (2, 2)  # first non-blank
    assert host.groups == 1
    assert host.row_maps == [None]  # marks follow the diff (whole lines)


def test_delete_register_and_count():
    host = FakeHost()
    run_ex("2d a 2", host)
    assert host.lines() == ["l1", "l4", "l5"]
    assert host.registers == {"a": ("l2\nl3", True, False)}


def test_delete_everything_leaves_one_empty_line():
    host = FakeHost("a\nb")
    run_ex("%d", host)
    assert host.lines() == [""]


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("d +", "Clipboard registers are not supported"),
        ("y *", "Clipboard registers are not supported"),
        ("d %", "Register % is not supported"),
        ("d a b", "Trailing characters: b"),
    ],
)
def test_delete_and_yank_reject_bad_arguments(line, message):
    host = FakeHost()
    with pytest.raises(ExError, match=f"^{message}$"):
        run_ex(line, host)
    assert host.lines() == FakeHost().lines() and host.registers == {}


def test_yank_named_register_keeps_text_and_cursor():
    host = FakeHost(row=3, col=1)
    run_ex("2y a", host)
    assert host.registers == {"a": ("l2", True, True)}
    assert host.lines() == FakeHost().lines()
    assert (host.row, host.col) == (3, 1) and host.groups == 0


def test_move_to_end():
    host = FakeHost(col=1)
    run_ex("1,2m$", host)
    assert host.lines() == ["l3", "l4", "l5", "l1", "l2"]
    assert (host.row, host.col) == (4, 1)
    assert host.row_maps == [[3, 4, 0, 1, 2]]


def test_move_up():
    host = FakeHost()
    run_ex("4,5m1", host)
    assert host.lines() == ["l1", "l4", "l5", "l2", "l3"]
    assert host.row == 2
    assert host.row_maps == [[0, 3, 4, 1, 2]]


def test_move_into_itself_fails():
    host = FakeHost()
    with pytest.raises(ExError, match="Cannot move a range of lines into itself"):
        run_ex("2,4m3", host)
    with pytest.raises(ExError, match="Invalid address"):
        run_ex("2m", host)
    assert host.lines() == FakeHost().lines()


def test_copy_above_first_line():
    host = FakeHost(row=2, col=1)
    run_ex("1t0", host)
    assert host.lines() == ["l1", "l1", "l2", "l3", "l4", "l5"]
    assert (host.row, host.col) == (0, 1)
    assert host.row_maps == [[1, 2, 3, 4, 5]]


def test_co_dollar_copies_current_line_to_end():
    host = FakeHost(row=1)
    run_ex("co$", host)
    assert host.lines() == ["l1", "l2", "l3", "l4", "l5", "l2"]
    assert host.row == 5


def test_normal_on_every_line():
    host = FakeHost()
    run_ex("%normal A;", host)
    assert host.lines() == ["l1;", "l2;", "l3;", "l4;", "l5;"]
    assert host.normal == [(r, "A;") for r in range(5)]
    assert host.groups == 1


def test_normal_counts_lines_like_vim():
    host = FakeHost()
    run_ex("%norm dd", host)
    # vim runs on lines 1, 2, 3, then the last line twice (clamped).
    assert host.normal == [(0, "dd"), (1, "dd"), (2, "dd"), (1, "dd"), (0, "dd")]


def test_normal_without_range_runs_at_the_cursor():
    host = FakeHost(row=2)
    run_ex("normal A;", host)
    assert host.normal == [(None, "A;")]
    with pytest.raises(ExError, match="Argument required"):
        run_ex("normal", host)


def test_goto_line():
    host = FakeHost(row=0)
    run_ex("5", host)
    run_ex("99", host)
    run_ex("0", host)
    run_ex("$-1", host)
    assert [e for e in host.events] == [
        ("goto", 4),
        ("goto", 4),
        ("goto", 0),
        ("goto", 3),
    ]


@pytest.mark.parametrize(
    ("line", "events"),
    [
        ("w", ["save"]),
        ("q", [("leave", False)]),
        ("q!", [("leave", True)]),
        ("wq", ["save", ("leave", False)]),
        ("x", ["save", ("leave", False)]),
        ("", []),
    ],
)
def test_write_and_quit(line, events):
    host = FakeHost()
    run_ex(line, host)
    assert host.events == events


@pytest.mark.parametrize("line", ["s/a/b/", "&", "&&", "g/x/d", "g!/x/d", "v/x/d"])
def test_later_commands_are_not_yet_editor_commands(line):
    host = FakeHost()
    with pytest.raises(ExError, match=f"^Not an editor command: {line}$"):
        run_ex(line, host)
    assert host.groups == 0


def test_unknown_command():
    with pytest.raises(ExError, match="^Not an editor command: 2bogus$"):
        run_ex(":2bogus", FakeHost())
    with pytest.raises(ExError, match="^Not an editor command: 3;4$"):
        run_ex("3;4", FakeHost())


@pytest.mark.parametrize("line", ["1,3w", "%wq", "2x", "'a,'bw"])
def test_write_with_a_range_is_refused(line):
    host = FakeHost()
    host.marks = {"a": (0, 0), "b": (1, 0)}
    with pytest.raises(ExError, match="^Use ! to write partial buffer$"):
        run_ex(line, host)
    with pytest.raises(ExError, match="^Writing part of the buffer is not supported$"):
        run_ex(line + "!", host)
    assert host.events == []

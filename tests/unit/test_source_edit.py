"""Pure helpers behind Code View Edit mode: atomic save, breakpoint
line remap across an edit, and $EDITOR resolution."""

from __future__ import annotations

import os

import pytest

from tdb.dap.types import SourceBreakpoint
from tdb.source_edit import (
    atomic_write_text,
    remap_breakpoints,
    remap_line_numbers,
    resolve_external_editor,
)

# ---- atomic_write_text ----


def test_atomic_write_creates_file_and_preserves_text(tmp_path):
    target = tmp_path / "prog.py"
    atomic_write_text(str(target), "x = 1\ny = 2\n")
    assert target.read_text(encoding="utf-8") == "x = 1\ny = 2\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["prog.py"]


def test_atomic_write_replaces_existing_file(tmp_path):
    target = tmp_path / "prog.py"
    target.write_text("old\n", encoding="utf-8")
    atomic_write_text(str(target), "new\n")
    assert target.read_text(encoding="utf-8") == "new\n"


def test_atomic_write_keeps_text_without_trailing_newline(tmp_path):
    target = tmp_path / "prog.py"
    atomic_write_text(str(target), "no newline at end")
    assert target.read_bytes() == b"no newline at end"


def test_atomic_write_raises_oserror_and_leaves_no_temp(tmp_path, monkeypatch):
    target = tmp_path / "prog.py"
    target.write_text("keep me\n", encoding="utf-8")

    def boom(src, dst):
        raise PermissionError("read-only")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_text(str(target), "new\n")
    assert target.read_text(encoding="utf-8") == "keep me\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["prog.py"]


# ---- remap_line_numbers ----


def test_remap_insert_above_shifts_down():
    old = ["a", "b", "c"]
    new = ["new", "a", "b", "c"]
    assert remap_line_numbers(old, new, [1, 3]) == {1: 2, 3: 4}


def test_remap_insert_below_leaves_lines_alone():
    old = ["a", "b", "c"]
    new = ["a", "b", "c", "new"]
    assert remap_line_numbers(old, new, [1, 3]) == {1: 1, 3: 3}


def test_remap_deleted_line_maps_to_none():
    old = ["a", "b", "c"]
    new = ["a", "c"]
    assert remap_line_numbers(old, new, [2, 3]) == {2: None, 3: 2}


def test_remap_equal_size_replace_block_maps_by_offset():
    old = ["a", "b1", "b2", "c"]
    new = ["a", "B1", "B2", "c"]
    assert remap_line_numbers(old, new, [2, 3]) == {2: 2, 3: 3}


def test_remap_unequal_replace_block_maps_to_block_start():
    old = ["a", "b1", "b2", "b3", "c"]
    new = ["a", "B", "c"]
    assert remap_line_numbers(old, new, [2, 3, 4]) == {2: 2, 3: 2, 4: 2}


def test_remap_out_of_range_line_is_none():
    assert remap_line_numbers(["a"], ["a"], [0, 5]) == {0: None, 5: None}


# ---- remap_breakpoints ----


def test_remap_breakpoints_preserves_flags_and_drops_deleted():
    old = ["a", "b", "c", "d"]
    new = ["x", "a", "c", "d"]  # inserted above, deleted 'b'
    bps = [
        SourceBreakpoint(line=1, condition="i > 1", enabled=False),
        SourceBreakpoint(line=2),
        SourceBreakpoint(line=4, hit_condition="3"),
    ]
    out = remap_breakpoints(old, new, bps)
    assert [(bp.line, bp.condition, bp.hit_condition, bp.enabled) for bp in out] == [
        (2, "i > 1", None, False),
        (4, None, "3", True),
    ]


def test_remap_breakpoints_collapses_duplicates_keeping_first():
    old = ["a", "b1", "b2", "c"]
    new = ["a", "B", "c"]
    bps = [SourceBreakpoint(line=2, condition="first"), SourceBreakpoint(line=3)]
    out = remap_breakpoints(old, new, bps)
    assert [(bp.line, bp.condition) for bp in out] == [(2, "first")]


# ---- resolve_external_editor ----


def test_resolve_editor_prefers_visual_then_editor_posix():
    assert resolve_external_editor({"VISUAL": "code -w", "EDITOR": "vim"}, "linux") == [
        "code",
        "-w",
    ]
    assert resolve_external_editor({"EDITOR": "vim"}, "linux") == ["vim"]
    assert resolve_external_editor({}, "linux") == ["vi"]


def test_resolve_editor_windows_passes_value_verbatim():
    assert resolve_external_editor({"EDITOR": "C:\\Tools\\ed.exe -n"}, "win32") == [
        "C:\\Tools\\ed.exe -n"
    ]
    assert resolve_external_editor({}, "win32") == ["notepad"]

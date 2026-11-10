"""Pure helpers behind Code View Edit mode: atomic save, breakpoint
line remap across an edit, and $EDITOR resolution."""

from __future__ import annotations

import errno
import os
import sys

import pytest

from tdb.dap.types import SourceBreakpoint
from tdb.source_edit import (
    atomic_write_text,
    remap_breakpoints,
    remap_line_numbers,
    resolve_external_editor,
    save_text,
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


def test_atomic_write_updates_symlink_target_not_the_link(tmp_path):
    real = tmp_path / "real.py"
    real.write_text("old\n", encoding="utf-8")
    link = tmp_path / "prog.py"
    link.symlink_to(real)
    atomic_write_text(str(link), "new\n")
    assert link.is_symlink()
    assert os.path.realpath(str(link)) == str(real)
    assert real.read_text(encoding="utf-8") == "new\n"


def test_atomic_write_preserves_file_mode(tmp_path):
    target = tmp_path / "script.py"
    target.write_text("old\n", encoding="utf-8")
    os.chmod(target, 0o755)
    atomic_write_text(str(target), "new\n")
    assert (os.stat(target).st_mode & 0o777) == 0o755


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


posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks/umask")


@posix_only
def test_atomic_write_ignores_planted_temp_symlink(tmp_path):
    """Someone who can write beside the source plants the old predictable
    temp name as a symlink: the save must not follow it."""
    target = tmp_path / "demo.py"
    target.write_text("old\n", encoding="utf-8")
    victim = tmp_path / "victim.txt"
    victim.write_text("precious\n", encoding="utf-8")
    (tmp_path / ".demo.py.tdb-tmp").symlink_to(victim)
    atomic_write_text(str(target), "new\n")
    assert victim.read_text(encoding="utf-8") == "precious\n"
    assert not target.is_symlink()
    assert target.read_text(encoding="utf-8") == "new\n"


def test_atomic_write_temp_names_are_unique(tmp_path, monkeypatch):
    """Two saves of one file must not share a temp file."""
    target = tmp_path / "prog.py"
    seen: list[str] = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(os.fspath(src))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    atomic_write_text(str(target), "a\n")
    atomic_write_text(str(target), "b\n")
    assert len(set(seen)) == 2
    assert all(os.path.dirname(p) == str(tmp_path) for p in seen)


@posix_only
def test_atomic_write_new_file_gets_umask_mode(tmp_path):
    target = tmp_path / "fresh.py"
    old = os.umask(0o022)
    try:
        atomic_write_text(str(target), "x\n")
    finally:
        os.umask(old)
    assert (os.stat(target).st_mode & 0o777) == 0o644


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


def test_resolve_editor_windows_splits_with_posix_false():
    assert resolve_external_editor({"EDITOR": "C:\\Tools\\ed.exe -n"}, "win32") == [
        "C:\\Tools\\ed.exe",
        "-n",
    ]
    assert resolve_external_editor({"EDITOR": 'code -w "My Dir"'}, "win32") == [
        "code",
        "-w",
        '"My Dir"',
    ]
    assert resolve_external_editor({}, "win32") == ["notepad"]


# ---- save_text ----

_no_perm = pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs POSIX permission enforcement (non-root)",
)


@_no_perm
def test_fallback_when_dir_readonly(tmp_path):
    f = tmp_path / "d" / "f.txt"
    f.parent.mkdir()
    f.write_text("old")
    f.parent.chmod(0o500)
    try:
        save_text(str(f), "new")
        assert f.read_text() == "new"
        assert [p.name for p in f.parent.iterdir()] == ["f.txt"]
    finally:
        f.parent.chmod(0o700)


def test_fallback_on_ebusy(tmp_path, monkeypatch):
    f = tmp_path / "hosts"
    f.write_text("old")

    def busy(*a):
        raise OSError(errno.EBUSY, "busy")

    monkeypatch.setattr(os, "replace", busy)
    save_text(str(f), "new")
    assert f.read_text() == "new"
    assert [p.name for p in tmp_path.iterdir()] == ["hosts"]


@_no_perm
def test_fallback_writes_through_symlink(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("old")
    d = tmp_path / "ro"
    d.mkdir()
    (d / "link").symlink_to(real)
    d.chmod(0o500)
    try:
        save_text(str(d / "link"), "new")
        assert real.read_text() == "new"
        assert (d / "link").is_symlink()
    finally:
        d.chmod(0o700)


@_no_perm
def test_both_fail_raises(tmp_path):
    f = tmp_path / "f"
    f.write_text("x")
    f.chmod(0o400)
    tmp_path.chmod(0o500)
    try:
        with pytest.raises(PermissionError):
            save_text(str(f), "y")
        assert f.read_text() == "x"
    finally:
        tmp_path.chmod(0o700)
        f.chmod(0o600)


def test_other_errors_not_retried(tmp_path, monkeypatch):
    f = tmp_path / "f"
    f.write_text("old")

    def full(*a):
        raise OSError(errno.ENOSPC, "no space")

    monkeypatch.setattr(os, "replace", full)
    with pytest.raises(OSError) as ei:
        save_text(str(f), "new")
    assert ei.value.errno == errno.ENOSPC
    assert f.read_text() == "old"

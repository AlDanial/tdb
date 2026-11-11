"""`tdb --vi` footer helpers: vim's ruler column and the file name."""

from __future__ import annotations

import os

import pytest

from tdb.widgets.vi_status_bar import display_name, ruler


@pytest.mark.parametrize(
    ("line", "col", "inserting", "expected"),
    [
        ("", 0, False, "0-1"),
        ("abc", 0, False, "1"),
        ("abc", 2, False, "3"),
        ("abc", 3, True, "4"),  # insert mode, past the end
        ("\tx", 0, False, "1-4"),  # on a tab: its last cell
        ("\tx", 0, True, "1"),  # inserting before the tab
        ("\tx", 1, False, "2-5"),
        ("é", 0, False, "1"),
        ("éx", 1, False, "3-2"),  # two-byte character before the cursor
        ("中x", 1, False, "4-3"),  # wide, three-byte character
    ],
)
def test_ruler(line, col, inserting, expected):
    assert ruler(line, col, 4, inserting=inserting) == expected


def test_display_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert display_name(None) == "[No Name]"
    assert display_name("rel/f.py") == "rel/f.py"
    assert display_name(str(tmp_path / "sub" / "f.py")) == os.path.join("sub", "f.py")
    outside = str(tmp_path.parent / "elsewhere.py")
    assert display_name(outside) == outside

"""tests/unit/test_go_detection.py"""

import pytest

from tdb.languages import registry
from tdb.languages.base import LanguageNotSupportedError
from tdb.languages.go import is_go_binary

# 16-byte magic Go embeds in every built binary (read by `go version`).
GO_BUILDINFO_MAGIC = b"\xff Go buildinf:"


def _fake_binary(tmp_path, name, payload):
    p = tmp_path / name
    p.write_bytes(b"\x7fELF" + b"\x00" * 60 + payload)
    return str(p)


def test_go_binary_sniff_positive(tmp_path):
    prog = _fake_binary(tmp_path, "gohello", b"junk" + GO_BUILDINFO_MAGIC + b"more")
    assert is_go_binary(prog)
    assert registry.detect(prog) == "go"


def test_non_go_elf_still_detects_cpp(tmp_path):
    prog = _fake_binary(tmp_path, "chello", b"no go marker here")
    assert not is_go_binary(prog)
    assert registry.detect(prog) == "cpp"


def test_marker_straddling_chunk_boundary(tmp_path):
    from tdb.languages import go

    payload = b"A" * (go._CHUNK - 70) + GO_BUILDINFO_MAGIC
    prog = _fake_binary(tmp_path, "straddle", payload)
    assert is_go_binary(prog)


def test_marker_beyond_scan_limit_is_missed(tmp_path):
    from tdb.languages import go

    payload = b"A" * (go._SCAN_LIMIT + 10) + GO_BUILDINFO_MAGIC
    prog = _fake_binary(tmp_path, "huge", payload)
    assert not is_go_binary(prog)  # bounded scan, documented limitation


def test_is_go_binary_missing_file_is_false(tmp_path):
    assert not is_go_binary(str(tmp_path / "nope"))


def test_directory_with_go_files_detects_go(tmp_path):
    (tmp_path / "main.go").write_text("package main\n")
    assert registry.detect(str(tmp_path)) == "go"


def test_directory_without_go_files_errors(tmp_path):
    (tmp_path / "readme.txt").write_text("hi")
    with pytest.raises(LanguageNotSupportedError):
        registry.detect(str(tmp_path))


def test_go_source_extension_still_maps(tmp_path):
    src = tmp_path / "main.go"
    src.write_text("package main\n")
    assert registry.detect(str(src)) == "go"

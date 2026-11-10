"""`tdb --vi FILE` argument handling: exactly one file, refused targets,
other switches ignored, and no subprocess ever started."""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from tdb.cli import parse_args


def _parse_error(argv, capsys) -> str:
    with pytest.raises(SystemExit) as exc:
        parse_args(argv)
    assert exc.value.code == 2
    return capsys.readouterr().err


def test_vi_no_file_errors(capsys):
    assert "--vi expects a file name" in _parse_error(["--vi"], capsys)


def test_vi_two_files_errors(tmp_path, capsys):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("a\n")
    b.write_text("b\n")
    assert "exactly one" in _parse_error(["--vi", str(a), str(b)], capsys)


def test_vi_directory_errors(tmp_path, capsys):
    assert "is a directory" in _parse_error(["--vi", str(tmp_path)], capsys)


def test_missing_parent_dir_rejected(tmp_path, capsys):
    target = tmp_path / "nope" / "f"
    assert "no such directory" in _parse_error(["--vi", str(target)], capsys)


def test_vi_binary_rejected(tmp_path, capsys):
    f = tmp_path / "prog"
    f.write_bytes(b"\x00ELF\x02\x01")
    assert "binary" in _parse_error(["--vi", str(f)], capsys)


def test_vi_non_utf8_rejected(tmp_path, capsys):
    f = tmp_path / "latin1.txt"
    f.write_bytes("caf\xe9\n".encode("latin-1"))
    assert "not valid UTF-8" in _parse_error(["--vi", str(f)], capsys)


def test_vi_ignores_debug_switches(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("hello\n")
    args = parse_args(["--vi", str(f), "-k", "x:3", "--timing"])
    assert args.vi_file == str(f)


def test_vi_ignores_switch_before_file(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("hello\n")
    args = parse_args(["-k", "nosuch.py:3", "--vi", str(f)])
    assert args.vi_file == str(f)


def test_vi_keeps_config_dir(tmp_path):
    d = tmp_path / "cfg"
    f = tmp_path / "f.txt"
    f.write_text("hello\n")
    args = parse_args(["--config-dir", str(d), "--vi", str(f)])
    assert args.config_dir == str(d)
    assert args.vi_file == str(f)


def test_vi_new_file_ok(tmp_path):
    f = tmp_path / "new.txt"
    args = parse_args(["--vi", str(f)])
    assert args.vi_file == str(f)
    assert not f.exists()


def test_no_vi_parsing_unchanged(tmp_path):
    f = tmp_path / "prog.py"
    f.write_text("x = 1\n")
    args = parse_args([str(f), "a", "b"])
    assert not args.vi
    assert args.program == str(f)
    assert args.args == ["a", "b"]


def test_main_dispatches_to_vi_app(monkeypatch, tmp_path):
    import tdb.cli as cli
    import tdb.vi_app as vi_app

    f = tmp_path / "f.txt"
    f.write_text("hello\n")
    monkeypatch.setenv("TDB_LOG_DIR", str(tmp_path / "log"))
    ran: list[str] = []

    class _FakeViApp:
        def __init__(self, path: str) -> None:
            self.path = path

        def run(self):
            ran.append(self.path)

    monkeypatch.setattr(vi_app, "ViApp", _FakeViApp)
    with pytest.raises(SystemExit) as exc:
        cli.main(["--config-dir", str(tmp_path / "cfg"), "--vi", str(f)])
    assert exc.value.code == 0
    assert ran == [str(f)]


async def test_vi_starts_no_subprocess(monkeypatch, tmp_path):
    from tdb.vi_app import ViApp

    def _refuse(*_a, **_k):
        raise AssertionError("--vi must not start a subprocess")

    monkeypatch.setattr(subprocess, "Popen", _refuse)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _refuse)
    monkeypatch.setenv("TDB_CONFIG_DIR", str(tmp_path / "cfg"))
    f = tmp_path / "f.txt"
    f.write_text("hello\n")
    app = ViApp(str(f))
    async with app.run_test() as pilot:
        await pilot.pause()
        for key in ("colon", "q", "enter"):
            await pilot.press(key)
        await pilot.pause()
    assert app.return_code == 0

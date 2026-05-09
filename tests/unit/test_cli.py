"""Unit tests for tdb.cli argument parsing."""

from __future__ import annotations

import pytest

from tdb.cli import parse_args


def test_program_required_without_remote_attach():
    with pytest.raises(SystemExit):
        parse_args([])


def test_basic_program(tmp_path):
    prog = tmp_path / "x.py"
    prog.write_text("print('hi')\n")
    args = parse_args([str(prog)])
    assert args.program == str(prog.resolve())
    assert args.stop_on_entry is True  # default
    assert args.attach_host is None
    assert args.attach_port is None


def test_no_stop_on_entry(tmp_path):
    prog = tmp_path / "x.py"
    prog.write_text("\n")
    args = parse_args([str(prog), "--no-stop-on-entry"])
    assert args.stop_on_entry is False


def test_program_path_must_exist(tmp_path):
    with pytest.raises(SystemExit):
        parse_args([str(tmp_path / "missing.py")])


def test_remote_attach_port_only():
    args = parse_args(["--remote-attach", "5678"])
    assert args.attach_host == "127.0.0.1"
    assert args.attach_port == 5678
    # No program needed in remote-attach mode.


def test_remote_attach_host_and_port():
    args = parse_args(["-r", "192.168.1.10:5678"])
    assert args.attach_host == "192.168.1.10"
    assert args.attach_port == 5678


def test_remote_attach_invalid_port():
    with pytest.raises(SystemExit):
        parse_args(["-r", "not-a-port"])


def test_breakpoints_parsed(tmp_path):
    prog = tmp_path / "x.py"
    prog.write_text("print('hi')\n")
    args = parse_args([
        str(prog),
        "-k", f"{prog}:5",
        "-k", f"{prog}:9",
    ])
    assert args.breakpoint == [
        (str(prog.resolve()), 5),
        (str(prog.resolve()), 9),
    ]


def test_breakpoint_bare_line_targets_program(tmp_path):
    prog = tmp_path / "x.py"
    prog.write_text("print('hi')\n")
    args = parse_args([str(prog), "-k", "5", "-k", "12"])
    assert args.breakpoint == [
        (str(prog.resolve()), 5),
        (str(prog.resolve()), 12),
    ]


def test_breakpoint_bare_and_file_line_mix(tmp_path):
    prog = tmp_path / "x.py"
    prog.write_text("print('hi')\n")
    other = tmp_path / "y.py"
    other.write_text("print('y')\n")
    args = parse_args([str(prog), "-k", "7", "-k", f"{other}:3"])
    assert args.breakpoint == [
        (str(prog.resolve()), 7),
        (str(other.resolve()), 3),
    ]


def test_breakpoint_bare_line_requires_program():
    # Remote-attach has no program — bare-line breakpoint must error.
    with pytest.raises(SystemExit):
        parse_args(["--remote-attach", "5678", "-k", "10"])


def test_breakpoint_missing_file(tmp_path):
    prog = tmp_path / "x.py"
    prog.write_text("\n")
    with pytest.raises(SystemExit):
        parse_args([str(prog), "-k", str(tmp_path / "missing.py:5")])


def test_breakpoint_invalid_format(tmp_path):
    prog = tmp_path / "x.py"
    prog.write_text("\n")
    with pytest.raises(SystemExit):
        parse_args([str(prog), "-k", "nocolon"])


def test_terminal_missing_executable_errors(tmp_path, monkeypatch):
    prog = tmp_path / "x.py"
    prog.write_text("\n")
    monkeypatch.setattr("shutil.which", lambda _name: None)
    with pytest.raises(SystemExit):
        parse_args([str(prog), "--terminal", "xterm"])


def test_terminal_present_passes(tmp_path, monkeypatch):
    prog = tmp_path / "x.py"
    prog.write_text("\n")
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    args = parse_args([str(prog), "--terminal", "xterm"])
    assert args.terminal == "xterm"


def test_headless_implies_server(tmp_path):
    prog = tmp_path / "x.py"
    prog.write_text("\n")
    args = parse_args([str(prog), "--headless"])
    assert args.server is True


def test_keybindings_scheme(tmp_path):
    prog = tmp_path / "x.py"
    prog.write_text("\n")
    args = parse_args([str(prog), "--keybindings", "emacs"])
    assert args.keybindings == "emacs"


def test_post_mortem_short_circuits_program_check():
    # No program required, no file existence check.
    args = parse_args(["--post-mortem", "/nonexistent/snapshot.json"])
    assert args.post_mortem == "/nonexistent/snapshot.json"
    assert args.program is None


def test_doc_flag_short_circuits_program_check():
    """--doc viewer mode needs no program and no remote-attach."""
    args = parse_args(["--doc"])
    assert args.doc is True
    assert args.program is None


def test_doc_flag_short_form():
    args = parse_args(["-d"])
    assert args.doc is True


def test_doc_flag_default_false(tmp_path):
    prog = tmp_path / "x.py"
    prog.write_text("\n")
    args = parse_args([str(prog)])
    assert args.doc is False


def test_doc_text_flag_short_circuits_program_check():
    """--doc-text prints the README to stdout; no program needed."""
    args = parse_args(["--doc-text"])
    assert args.doc_text is True
    assert args.program is None


def test_doc_text_flag_default_false(tmp_path):
    prog = tmp_path / "x.py"
    prog.write_text("\n")
    args = parse_args([str(prog)])
    assert args.doc_text is False


def test_version_flag_prints_and_exits(capsys):
    with pytest.raises(SystemExit) as exc:
        parse_args(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("tdb ")


def test_version_short_form(capsys):
    with pytest.raises(SystemExit) as exc:
        parse_args(["-v"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("tdb ")


def test_doc_text_renders_to_stdout(capsys):
    """End-to-end: _run_doc_text writes the rendered README to stdout."""
    from tdb.cli import _run_doc_text
    _run_doc_text()
    out = capsys.readouterr().out
    # README starts with the project title — should be visible after Rich
    # renders the markdown (centred or otherwise, but the text is there).
    assert "textual-debugger" in out
    # And tables in the README produce box-drawing characters.
    assert "─" in out or "━" in out

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
    args = parse_args(
        [
            str(prog),
            "-k",
            f"{prog}:5",
            "-k",
            f"{prog}:9",
        ]
    )
    assert args.breakpoint == [
        (str(prog.resolve()), 5),
        (str(prog.resolve()), 9),
    ]


def test_breakpoint_implies_no_stop_on_entry(tmp_path):
    prog = tmp_path / "x.py"
    prog.write_text("print('hi')\n")
    # Default (no -k) → stop on entry True.
    args = parse_args([str(prog)])
    assert args.stop_on_entry is True
    # With -k → stop on entry is suppressed so the program runs to the breakpoint.
    args = parse_args([str(prog), "-k", "5"])
    assert args.stop_on_entry is False


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


def test_breakpoint_snaps_to_statement_start_with_warning(tmp_path, capsys):
    # A multi-line statement spans lines 2-5; line 4 is a sub-line.
    prog = tmp_path / "x.py"
    prog.write_text(
        "a = 1\n"
        "results = func(\n"  # line 2 (statement start)
        "    1,\n"  # line 3
        "    2,\n"  # line 4 — sub-line
        ")\n"  # line 5
        "b = 2\n"  # line 6
    )
    args = parse_args([str(prog), "-k", "4"])
    assert args.breakpoint == [(str(prog.resolve()), 2)]
    err = capsys.readouterr().err
    assert "not the start of a logical statement" in err
    assert "moved to line 2" in err


def test_breakpoint_at_statement_start_does_not_warn(tmp_path, capsys):
    prog = tmp_path / "x.py"
    prog.write_text("a = 1\nb = 2\n")  # both statement starts
    args = parse_args([str(prog), "-k", "2"])
    assert args.breakpoint == [(str(prog.resolve()), 2)]
    assert capsys.readouterr().err == ""


def test_breakpoint_before_first_statement_dropped(tmp_path, capsys):
    prog = tmp_path / "x.py"
    prog.write_text("# just a comment\n# another\n\nx = 1\n")
    args = parse_args([str(prog), "-k", "2"])
    # No statement before/at line 2 → dropped with a warning.
    assert args.breakpoint == []
    err = capsys.readouterr().err
    assert "no logical statement" in err
    assert "dropping breakpoint" in err


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


# --- --local-root / --remote-root path mappings -----------------------------


def test_path_mappings_default_empty():
    args = parse_args(["-r", "5678"])
    assert args.path_mappings == []


def test_path_mappings_single_pair(tmp_path):
    local = tmp_path / "code"
    local.mkdir()
    args = parse_args(
        ["-r", "5678", "--local-root", str(local), "--remote-root", "/srv/code"]
    )
    assert args.path_mappings == [(str(local.resolve()), "/srv/code")]


def test_path_mappings_multiple_pairs_zip_in_order(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    args = parse_args(
        [
            "-r",
            "5678",
            "--local-root",
            str(a),
            "--remote-root",
            "/srv/A",
            "--local-root",
            str(b),
            "--remote-root",
            "/srv/B",
        ]
    )
    assert args.path_mappings == [
        (str(a.resolve()), "/srv/A"),
        (str(b.resolve()), "/srv/B"),
    ]


def test_path_mappings_count_mismatch_errors(tmp_path):
    local = tmp_path / "code"
    local.mkdir()
    with pytest.raises(SystemExit):
        parse_args(
            [
                "-r",
                "5678",
                "--local-root",
                str(local),
                "--remote-root",
                "/srv/A",
                "--remote-root",
                "/srv/B",
            ]
        )


def test_path_mappings_require_remote_attach(tmp_path):
    prog = tmp_path / "x.py"
    prog.write_text("\n")
    local = tmp_path / "code"
    local.mkdir()
    with pytest.raises(SystemExit):
        parse_args(
            [
                str(prog),
                "--local-root",
                str(local),
                "--remote-root",
                "/srv/A",
            ]
        )


def test_path_mappings_local_root_must_be_directory(tmp_path):
    not_a_dir = tmp_path / "nope.py"
    not_a_dir.write_text("\n")
    with pytest.raises(SystemExit):
        parse_args(
            [
                "-r",
                "5678",
                "--local-root",
                str(not_a_dir),
                "--remote-root",
                "/srv/A",
            ]
        )


def test_path_mappings_remote_root_normalized(tmp_path):
    local = tmp_path / "code"
    local.mkdir()
    # Trailing slash + backslashes both normalize to clean forward-slash form.
    args = parse_args(
        [
            "-r",
            "5678",
            "--local-root",
            str(local),
            "--remote-root",
            r"C:\srv\code\\",
        ]
    )
    assert args.path_mappings == [(str(local.resolve()), "C:/srv/code")]


def test_breakpoint_relative_path_resolved_under_local_root(tmp_path):
    local = tmp_path / "code"
    local.mkdir()
    src = local / "program.py"
    src.write_text("\n" * 20)
    args = parse_args(
        [
            "-r",
            "5678",
            "--local-root",
            str(local),
            "--remote-root",
            "/srv/code",
            "-k",
            "program.py:5",
        ]
    )
    # Resolved to the local-root copy; snap_breakpoint may move the line
    # to the nearest statement (blank file → likely dropped, so only
    # assert path-resolution here by inspecting before-snap state).
    # Use a real statement so snap_breakpoint keeps it:
    src.write_text("x = 1\n" * 20)
    args = parse_args(
        [
            "-r",
            "5678",
            "--local-root",
            str(local),
            "--remote-root",
            "/srv/code",
            "-k",
            "program.py:5",
        ]
    )
    assert args.breakpoint == [(str(src.resolve()), 5)]


def test_breakpoint_relative_path_not_found_under_local_root_errors(tmp_path):
    local = tmp_path / "code"
    local.mkdir()
    with pytest.raises(SystemExit):
        parse_args(
            [
                "-r",
                "5678",
                "--local-root",
                str(local),
                "--remote-root",
                "/srv/code",
                "-k",
                "nope.py:5",
            ]
        )


def test_breakpoint_absolute_path_still_works_with_local_root(tmp_path):
    local = tmp_path / "code"
    local.mkdir()
    elsewhere = tmp_path / "elsewhere.py"
    elsewhere.write_text("x = 1\n" * 20)
    args = parse_args(
        [
            "-r",
            "5678",
            "--local-root",
            str(local),
            "--remote-root",
            "/srv/code",
            "-k",
            f"{elsewhere}:5",
        ]
    )
    assert args.breakpoint == [(str(elsewhere.resolve()), 5)]


def test_breakpoint_relative_searches_local_roots_in_order(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    # Only the second local-root has the file.
    src = b / "found.py"
    src.write_text("x = 1\n" * 20)
    args = parse_args(
        [
            "-r",
            "5678",
            "--local-root",
            str(a),
            "--remote-root",
            "/srv/A",
            "--local-root",
            str(b),
            "--remote-root",
            "/srv/B",
            "-k",
            "found.py:5",
        ]
    )
    assert args.breakpoint == [(str(src.resolve()), 5)]

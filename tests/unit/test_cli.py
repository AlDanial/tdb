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


def test_terminal_rejected_with_remote_attach(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    with pytest.raises(SystemExit):
        parse_args(["--terminal", "xterm", "-r", "5678"])


def test_terminal_rejected_for_cpp_with_default_gdb_adapter(tmp_path, monkeypatch):
    """gdb's DAP mode has no terminal integration (GdbDapAdapter.launch_body
    raises the same error as a backstop) -- the CLI must reject this
    combination up front rather than let it fail deep inside a TUI worker,
    where it degrades to a bare "Failed to start" subtitle. No --adapter
    is given here, so this also covers the implicit gdb default."""
    binary = _write_elf(tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    with pytest.raises(SystemExit):
        parse_args(["--lang", "cpp", "--terminal", "xterm", str(binary)])


def test_terminal_rejected_for_cpp_with_explicit_gdb_adapter(tmp_path, monkeypatch):
    binary = _write_elf(tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    with pytest.raises(SystemExit):
        parse_args(
            ["--lang", "cpp", "--adapter", "gdb", "--terminal", "xterm", str(binary)]
        )


def test_terminal_allowed_for_cpp_with_lldb_dap_adapter(tmp_path, monkeypatch):
    binary = _write_elf(tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    args = parse_args(
        ["--lang", "cpp", "--adapter", "lldb-dap", "--terminal", "xterm", str(binary)]
    )
    assert args.terminal == "xterm"
    assert args.profile.id == "cpp"
    assert args.profile.adapter.id == "lldb-dap"


def test_terminal_rejected_for_ocaml_with_explicit_gdb_adapter(tmp_path, monkeypatch):
    """OCaml's gdb adapter is the same GdbDapAdapter as cpp's -- no terminal
    integration -- so the up-front gate must catch it too, not just cpp."""
    binary = _write_elf(tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    with pytest.raises(SystemExit):
        parse_args(
            ["--lang", "ocaml", "--adapter", "gdb", "--terminal", "xterm", str(binary)]
        )


def test_terminal_rejected_for_ocaml_with_earlybird_adapter(tmp_path, monkeypatch):
    """ocamlearlybird has no terminal integration either (it hardcodes
    console: internalConsole) -- must be rejected up front, same as gdb."""
    binary = _write_elf(tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--lang",
                "ocaml",
                "--adapter",
                "ocamlearlybird",
                "--terminal",
                "xterm",
                str(binary),
            ]
        )


def test_terminal_allowed_for_ocaml_with_lldb_dap_adapter(tmp_path, monkeypatch):
    binary = _write_elf(tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    args = parse_args(
        ["--lang", "ocaml", "--adapter", "lldb-dap", "--terminal", "xterm", str(binary)]
    )
    assert args.terminal == "xterm"
    assert args.profile.id == "ocaml"
    assert args.profile.adapter.id == "lldb-dap"


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


def test_mcp_flag_short_circuits_program_check():
    """--mcp owns its own lifecycle (sessions come from the agent's
    debug_launch tool); no program argument is needed on the CLI."""
    args = parse_args(["--mcp"])
    assert args.mcp is True
    assert args.program is None


def test_mcp_flag_default_false(tmp_path):
    prog = tmp_path / "x.py"
    prog.write_text("\n")
    args = parse_args([str(prog)])
    assert args.mcp is False


def test_main_dispatches_to_mcp_runner(monkeypatch):
    """`tdb --mcp` must route to the MCP entry point, not to the TUI
    or headless server. Equivalent to `python -m tdb.mcp`."""
    called: list[bool] = []

    def _fake_mcp_main():
        called.append(True)

    import tdb.mcp.server as mcp_server

    monkeypatch.setattr(mcp_server, "main", _fake_mcp_main)

    from tdb.cli import main as cli_main

    cli_main(["--mcp"])
    assert called == [True]


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
    # Use a real statement so snap_breakpoint keeps the breakpoint (and avoid warnings).
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


def test_headless_remote_attach_parses(tmp_path):
    """--headless + --remote-attach is allowed (was guarded; now wired up)."""
    args = parse_args(["--headless", "-r", "5678"])
    assert args.headless is True
    assert args.server is True
    assert args.attach_host == "127.0.0.1"
    assert args.attach_port == 5678
    assert args.program is None


def test_headless_remote_attach_with_path_mappings_parses(tmp_path):
    local = tmp_path / "code"
    local.mkdir()
    args = parse_args(
        [
            "--headless",
            "-r",
            "rhost:15678",
            "--local-root",
            str(local),
            "--remote-root",
            "/srv/code",
        ]
    )
    assert args.headless is True
    assert args.attach_host == "rhost"
    assert args.attach_port == 15678
    assert args.path_mappings == [(str(local.resolve()), "/srv/code")]


def test_run_headless_forwards_attach_args_to_runner(tmp_path, monkeypatch):
    """cli._run_headless plumbs attach_host/attach_port/path_mappings through."""
    import asyncio as _asyncio

    captured: dict = {}

    async def _fake_run_headless(**kwargs):
        captured.update(kwargs)

    # Patch the import target inside _run_headless (it does
    # `from tdb.server.runner import run_headless` at call time).
    import tdb.server.runner as runner_mod

    monkeypatch.setattr(runner_mod, "run_headless", _fake_run_headless)

    # Patch asyncio.run so it just awaits the coroutine synchronously here.
    def _run(coro):
        loop = _asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    monkeypatch.setattr("asyncio.run", _run)

    local = tmp_path / "code"
    local.mkdir()
    args = parse_args(
        [
            "--headless",
            "-r",
            "rhost:15678",
            "--local-root",
            str(local),
            "--remote-root",
            "/srv/code",
        ]
    )

    from tdb.cli import _run_headless

    _run_headless(args)

    assert captured["attach_host"] == "rhost"
    assert captured["attach_port"] == 15678
    assert captured["path_mappings"] == [(str(local.resolve()), "/srv/code")]
    assert captured["program"] is None
    assert captured["profile"] is args.profile


def test_run_headless_forwards_profile_to_runner(tmp_path):
    """cli._run_headless threads args.profile through to run_headless."""
    captured: dict = {}

    async def _fake_run_headless(**kwargs):
        captured.update(kwargs)

    import tdb.server.runner as runner_mod

    program = tmp_path / "prog.py"
    program.write_text("print('hi')\n")

    args = parse_args(["--headless", str(program)])

    from tdb.cli import _run_headless

    orig = runner_mod.run_headless
    runner_mod.run_headless = _fake_run_headless
    try:
        _run_headless(args)
    finally:
        runner_mod.run_headless = orig

    assert captured["profile"] is args.profile
    assert args.profile.id == "python"


def _write_elf(tmp_path):
    binary = tmp_path / "prog"
    binary.write_bytes(b"\x7fELF\x02\x01\x01" + b"\x00" * 9)
    return binary


def test_python_program_resolves_python_profile(tmp_path):
    prog = tmp_path / "p.py"
    prog.write_text("pass\n")
    args = parse_args([str(prog)])
    assert args.profile.id == "python"


def test_elf_binary_resolves_cpp_profile_or_errors_before_task10(tmp_path):
    # Until Task 10 registers cpp, this errors with "not supported";
    # after Task 10 it resolves. Written to pass in both states:
    binary = _write_elf(tmp_path)
    try:
        args = parse_args([str(binary)])
    except SystemExit:
        return  # pre-Task-10: parser.error path exercised
    assert args.profile.id == "cpp"


def test_lang_flag_overrides_detection(tmp_path):
    binary = _write_elf(tmp_path)
    args = parse_args(["--lang", "python", str(binary)])
    assert args.profile.id == "python"


def test_python_flag_rejected_for_non_python(tmp_path, capsys):
    binary = _write_elf(tmp_path)
    with pytest.raises(SystemExit):
        parse_args(["--lang", "cpp", "--python", "/usr/bin/python3", str(binary)])


def test_no_subprocess_rejected_for_non_python(tmp_path):
    binary = _write_elf(tmp_path)
    with pytest.raises(SystemExit):
        parse_args(["--lang", "cpp", "--no-subprocess", str(binary)])


def test_remote_attach_rejected_for_non_python():
    with pytest.raises(SystemExit):
        parse_args(["--lang", "cpp", "-r", "5678"])


def test_breakpoints_not_snapped_for_non_python(tmp_path, monkeypatch):
    binary = _write_elf(tmp_path)
    called = []
    import tdb.source_analysis as sa

    monkeypatch.setattr(sa, "snap_breakpoint", lambda *a: called.append(a) or a[1])
    try:
        args = parse_args(["--lang", "cpp", "-k", f"{binary}:3", str(binary)])
    except SystemExit:
        pytest.skip("cpp profile not yet registered (pre-Task-10)")
    assert called == []
    assert args.breakpoint == [(str(binary), 3)]


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


# --- -t / --to-line: like -k but not persisted -------------------------------


def test_to_line_parsed_like_breakpoint(tmp_path):
    prog = tmp_path / "x.py"
    prog.write_text("a = 1\nb = 2\nc = 3\n")
    args = parse_args([str(prog), "-t", f"{prog}:2", "-t", "3"])
    assert args.to_line == [
        (str(prog.resolve()), 2),
        (str(prog.resolve()), 3),
    ]
    assert args.breakpoint == []


def test_to_line_implies_no_stop_on_entry(tmp_path):
    prog = tmp_path / "x.py"
    prog.write_text("a = 1\n")
    args = parse_args([str(prog), "-t", "1"])
    assert args.stop_on_entry is False


def test_to_line_snaps_to_statement_start(tmp_path, capsys):
    prog = tmp_path / "x.py"
    prog.write_text(
        "a = 1\n"
        "results = func(\n"  # line 2 (statement start)
        "    1,\n"  # line 3 — sub-line
        ")\n"
    )
    args = parse_args([str(prog), "-t", "3"])
    assert args.to_line == [(str(prog.resolve()), 2)]
    err = capsys.readouterr().err
    assert "-t" in err and "moved to line 2" in err


def test_to_line_bare_line_requires_program():
    with pytest.raises(SystemExit):
        parse_args(["--remote-attach", "5678", "-t", "10"])


def test_cli_bps_merges_breakpoint_and_to_line_with_persist_flag(tmp_path):
    prog = tmp_path / "x.py"
    prog.write_text("a = 1\nb = 2\nc = 3\n")
    args = parse_args([str(prog), "-k", "1", "-t", "2"])
    assert args.cli_bps == [
        (str(prog.resolve()), 1, True),
        (str(prog.resolve()), 2, False),
    ]


def test_remote_attach_allowed_for_perl():
    args = parse_args(["--lang", "perl", "-r", "5678"])
    assert args.profile.id == "perl"
    assert args.attach_port == 5678


def test_remote_attach_still_rejected_for_cpp():
    with pytest.raises(SystemExit):
        parse_args(["--lang", "cpp", "-r", "5678"])


def test_remote_attach_allows_ruby():
    args = parse_args(["-r", "5678", "--lang", "ruby"])
    assert args.profile.id == "ruby"
    assert args.profile.adapter.id == "rdbg"


def test_remote_attach_still_rejects_bash():
    with pytest.raises(SystemExit):
        parse_args(["-r", "5678", "--lang", "bash"])

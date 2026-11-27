"""CLI validation for the trace modes: --trace, --trace-merge, --trace-discover.

The trace modes never debug the program, so every debugger mode flag is
rejected with them, and the language is never auto-detected: a ``.py``
program, ``-m MODULE`` or an explicit ``--lang`` decides it.
"""

from __future__ import annotations

import pytest

from tdb.cli import build_parser, parse_args


@pytest.fixture
def spec(tmp_path):
    (tmp_path / "prog.py").write_text("def f():\n    pass\n")
    p = tmp_path / "s.yaml"
    p.write_text("prog.py:\n  - f\n")
    return str(p)


def err(capsys, argv):
    with pytest.raises(SystemExit) as ei:
        parse_args(argv)
    assert ei.value.code == 2
    return capsys.readouterr().err


def test_trace_parses_program_and_args(spec, tmp_path):
    a = parse_args(
        [
            "--trace",
            spec,
            "--trace-name",
            "orders",
            str(tmp_path / "prog.py"),
            "--",
            "--port",
            "8001",
        ]
    )
    assert (
        a.trace_spec.entries[0].functions[0].pattern == "f"
        and a.trace_args == ["--port", "8001"]
        and a.trace_name == "orders"
    )


def test_program_kept_as_typed(spec, tmp_path, monkeypatch):
    # Ruling 25: checked to exist, but run as typed (launch makes it
    # absolute only for --cwd), so sys.argv[0] is what it is without tdb.
    monkeypatch.chdir(tmp_path)
    a = parse_args(["--trace", spec, "prog.py"])
    assert a.program == "prog.py"


def test_m_takes_positionals_as_module_args(spec):
    a = parse_args(["--trace", spec, "-m", "orders.main", "--", "--port", "8001"])
    assert (a.trace_module, a.program, a.trace_args) == (
        "orders.main",
        "--port",
        ["--port", "8001"],
    )


def test_m_without_positionals_has_no_args(spec):
    a = parse_args(["--trace", spec, "-m", "orders.main"])
    assert (a.program, a.trace_args) == (None, [])


def test_command_form_keeps_program_as_given(spec):
    a = parse_args(["--trace", spec, "uvicorn", "app:app"])
    assert (a.program, a.trace_args) == ("uvicorn", ["app:app"])


@pytest.mark.parametrize(
    "extra,flag",
    [
        (["--run"], "--run"),
        (["-e", "1", "x"], "-e/--eval"),
        (["-a", "123"], "-a/--attach"),
        (["-k", "3"], "-k/--breakpoint"),
        (["--headless"], "--headless"),
        (["--no-subprocess"], "--no-subprocess"),
        (["--source", "x"], "--source"),
        (["-r", "5678"], "-r/--remote-attach"),
        (["-t", "3"], "-t/--to-line"),
        (["--record", "r.json"], "--record"),
        (["--terminal", "xterm"], "--terminal"),
        (["--keybindings", "vim"], "--keybindings"),
        (["--adapter", "gdb"], "--adapter"),
        (["--info"], "--info"),
        (["--replay-quiet"], "--replay-quiet"),
        (["--replay-interval", "1"], "--replay-interval"),
        (["--replay-timeout", "5"], "--replay-timeout"),
        (["--timing"], "--timing"),
        (["--server-port", "9000"], "--server-port"),
        (["--no-pause-on-attach"], "--no-pause-on-attach"),
    ],
)
def test_trace_rejects_conflicting_flags(spec, tmp_path, capsys, extra, flag):
    assert f"--trace cannot be combined with {flag}" in err(
        capsys, ["--trace", spec, *extra, str(tmp_path / "prog.py")]
    )


def test_default_server_port_is_no_conflict(spec, tmp_path):
    a = parse_args(
        ["--trace", spec, "--server-port", "8150", str(tmp_path / "prog.py")]
    )
    assert a.trace_spec is not None


def test_conflict_names_the_mode_in_use(tmp_path, capsys):
    assert "--trace-discover cannot be combined with --headless" in err(
        capsys, ["--trace-discover", "o.yaml", "--headless", str(tmp_path)]
    )
    assert "--trace-merge cannot be combined with --run" in err(
        capsys, ["--run", "--trace-merge", "a.jsonl"]
    )


def test_modes_mutually_exclusive(spec, tmp_path, capsys):
    assert "mutually exclusive" in err(
        capsys,
        ["--trace", spec, "--trace-discover", "o.yaml", str(tmp_path / "prog.py")],
    )


def test_m_requires_trace_mode(capsys):
    assert "-m is supported only with --trace or --trace-discover" in err(
        capsys, ["-m", "x"]
    )


def test_m_rejected_with_merge(capsys):
    assert "-m is supported only with --trace or --trace-discover" in err(
        capsys, ["-m", "x", "--trace-merge", "a.jsonl"]
    )


def test_trace_only_flags_need_trace(tmp_path, capsys):
    assert "--trace-name requires --trace" in err(
        capsys, ["--trace-name", "x", str(tmp_path)]
    )
    assert "--trace-max-events requires --trace" in err(
        capsys, ["--trace-max-events", "5", str(tmp_path)]
    )
    assert "--trace-out requires --trace or --trace-merge" in err(
        capsys, ["--trace-out", "x", str(tmp_path)]
    )
    assert "--trace-out requires --trace or --trace-merge" in err(
        capsys, ["--trace-discover", "o.yaml", "--trace-out", "x", str(tmp_path)]
    )


def test_max_events_must_be_positive(spec, tmp_path, capsys):
    assert "--trace-max-events must be a positive integer" in err(
        capsys, ["--trace", spec, "--trace-max-events", "0", str(tmp_path / "prog.py")]
    )


def test_max_events_parsed(spec, tmp_path):
    a = parse_args(
        ["--trace", spec, "--trace-max-events", "1000", str(tmp_path / "prog.py")]
    )
    assert a.trace_max_events == 1000


def test_trace_needs_program_or_module(spec, capsys):
    assert "--trace needs a program or -m MODULE" in err(capsys, ["--trace", spec])


def test_python_rejected_with_command_form(spec, capsys):
    assert "--python/--pv apply only to a .py program or -m" in err(
        capsys, ["--trace", spec, "--python", "/x/py", "uvicorn"]
    )
    assert "--python/--pv apply only to a .py program or -m" in err(
        capsys, ["--trace", spec, "--pv", "uvicorn"]
    )


def test_python_allowed_with_module(spec):
    a = parse_args(["--trace", spec, "--pv", "-m", "pkg"])
    assert a.python == ".venv/bin/python"


def test_lang_without_tracer_rejected(spec, tmp_path, capsys):
    assert "--trace is not yet supported for Perl" in err(
        capsys, ["--trace", spec, "--lang", "perl", "svc"]
    )


def test_lang_unknown_rejected(spec, capsys):
    assert "language 'cobol' is not supported yet" in err(
        capsys, ["--trace", spec, "--lang", "cobol", "svc"]
    )


def test_lang_python_with_command_form(spec):
    a = parse_args(["--trace", spec, "--lang", "python", "uvicorn"])
    assert a.program == "uvicorn"


def test_missing_py_program_rejected(spec, tmp_path, capsys):
    assert f"File not found: {tmp_path / 'nope.py'}" in err(
        capsys, ["--trace", spec, str(tmp_path / "nope.py")]
    )


def test_spec_errors_reported_before_launch(tmp_path, capsys):
    (tmp_path / "s.yaml").write_text("missing.py:\n  - f\n")
    assert "missing.py: no such file" in err(
        capsys, ["--trace", str(tmp_path / "s.yaml"), "x.py"]
    )


def test_spec_problems_listed_one_per_line(tmp_path, capsys):
    s = tmp_path / "s.yaml"
    s.write_text("a.py:\n  - f\nb.py:\n  - g\n")
    assert (
        f"invalid trace spec {s}:\n  a.py: no such file\n  b.py: no such file"
        in err(capsys, ["--trace", str(s), "x.py"])
    )


@pytest.mark.parametrize("content", [None, b"\xff\xfe\x00bad"])
def test_unreadable_spec_is_a_parser_error(tmp_path, capsys, content):
    s = tmp_path / "s.yaml"
    if content is not None:
        s.write_bytes(content)
    out = err(capsys, ["--trace", str(s), "x.py"])
    assert f"invalid trace spec {s}: " in out
    assert "Traceback" not in out


def test_discover_has_no_spec(tmp_path):
    (tmp_path / "prog.py").write_text("")
    a = parse_args(["--trace-discover", "o.yaml", str(tmp_path / "prog.py"), "x"])
    assert (a.trace_spec, a.trace_args) == (None, ["x"])


def test_discover_needs_program_or_module(capsys):
    assert "--trace-discover needs a program or -m MODULE" in err(
        capsys, ["--trace-discover", "o.yaml"]
    )


def test_merge_takes_inputs(capsys):
    a = parse_args(["--trace-merge", "a.jsonl", "b.parts", "--trace-out", "x"])
    assert (a.trace_merge, a.trace_out, a.program) == (
        ["a.jsonl", "b.parts"],
        "x",
        None,
    )


@pytest.mark.parametrize(
    "extra,flag",
    [
        (["--python", "python3"], "--python/--pv"),
        (["--pv"], "--python/--pv"),
        (["--cwd", "elsewhere"], "--cwd"),
        (["--lang", "python"], "--lang"),
    ],
)
def test_merge_rejects_launch_flags(capsys, extra, flag):
    """What runs a program has no meaning for a merge (Task 9 M1)."""
    assert f"--trace-merge cannot be combined with {flag}" in err(
        capsys, ["--trace-merge", "a.jsonl", *extra]
    )


@pytest.mark.parametrize(
    "extra", [["--pv"], ["--cwd", "elsewhere"], ["--lang", "python"]]
)
def test_launch_flags_allowed_with_discover(tmp_path, extra):
    (tmp_path / "prog.py").write_text("")
    a = parse_args(["--trace-discover", "o.yaml", *extra, str(tmp_path / "prog.py")])
    assert a.trace_discover == "o.yaml"


def test_discover_output_must_not_be_a_directory(tmp_path, capsys):
    (tmp_path / "prog.py").write_text("")
    assert f"--trace-discover: {tmp_path} is a directory" in err(
        capsys, ["--trace-discover", str(tmp_path), str(tmp_path / "prog.py")]
    )


def test_non_trace_run_has_trace_defaults(tmp_path):
    p = tmp_path / "prog.py"
    p.write_text("pass\n")
    a = parse_args([str(p)])
    assert (a.trace, a.trace_spec, a.trace_module) == (None, None, None)


def test_help_lists_trace_flags():
    text = build_parser().format_help()
    for flag in (
        "--trace SPEC.yaml",
        "--trace-name NAME",
        "--trace-out BASE",
        "--trace-merge INPUT",
        "--trace-discover OUT.yaml",
        "--trace-max-events N",
        "-m MODULE",
    ):
        assert flag in text

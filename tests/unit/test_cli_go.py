"""tests/unit/test_cli_go.py"""

import pytest

from tdb.cli import parse_args

GO_BUILDINFO_MAGIC = b"\xff Go buildinf:"


@pytest.fixture
def go_src(tmp_path):
    src = tmp_path / "main.go"
    src.write_text("package main\nfunc main() {}\n")
    return str(src)


@pytest.fixture
def go_pkg(tmp_path):
    (tmp_path / "main.go").write_text("package main\nfunc main() {}\n")
    return str(tmp_path)


def test_go_source_resolves_go_profile(go_src):
    args = parse_args([go_src])
    assert args.profile.id == "go"
    assert args.profile.adapter.id == "dlv"


def test_test_flag_selects_test_mode(go_pkg):
    args = parse_args(["--test", go_pkg])
    body = args.profile.adapter.launch_body(
        program=go_pkg,
        args=[],
        cwd=".",
        env=None,
        stop_on_entry=True,
        console="internalConsole",
        opts={},
    )
    assert body["mode"] == "test"


def test_test_flag_rejected_for_non_go(tmp_path):
    py = tmp_path / "x.py"
    py.write_text("print(1)\n")
    with pytest.raises(SystemExit):
        parse_args(["--test", str(py)])


def test_attach_pid_builds_local_attach_profile(tmp_path, monkeypatch):
    monkeypatch.setattr("tdb.languages.go.is_go_binary", lambda p: True)
    args = parse_args(["--lang", "go", "-a", "4242"])
    assert args.attach_pid == 4242
    assert args.attach_host == "127.0.0.1"
    assert args.attach_port == 0
    body = args.profile.adapter.attach_body(host="127.0.0.1", port=0, opts={})
    assert body == {"mode": "local", "processId": 4242, "stopOnEntry": True}


def test_attach_pid_rejected_for_non_go(tmp_path):
    py = tmp_path / "x.py"
    py.write_text("print(1)\n")
    with pytest.raises(SystemExit):
        parse_args(["-a", "4242", str(py)])


def test_attach_pid_conflicts_with_remote_attach():
    with pytest.raises(SystemExit):
        parse_args(["--lang", "go", "-a", "1", "-r", "5678"])


def test_remote_attach_allows_go():
    args = parse_args(["--lang", "go", "-r", "localhost:5678"])
    assert args.profile.id == "go"


def test_terminal_rejected_for_go(go_src):
    with pytest.raises(SystemExit):
        parse_args(["--terminal", "xterm", go_src])


def test_run_allowed_for_go(go_src):
    assert parse_args(["--run", go_src]).profile.id == "go"

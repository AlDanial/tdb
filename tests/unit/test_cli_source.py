import json

import pytest

from tdb.app import TdbApp
from tdb.cli import (
    _run_post_mortem,
    _run_run,
    _run_tui,
    build_replay_tui_app,
    parse_args,
)
from tdb.persist import TdbConfig
from tdb.replay import Recording
from tdb.source_discovery import discover_sources


@pytest.fixture
def source_args(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main = tmp_path / "main.py"
    main.write_text("import local\n")
    (tmp_path / "local.py").write_text("")
    external = tmp_path / "elsewhere" / "extra.py"
    external.parent.mkdir()
    external.write_text("")
    listing = tmp_path / "lists" / "files.txt"
    listing.parent.mkdir()
    listing.write_text("elsewhere/extra.py\nmissing.py\n")
    return main, external, listing


def test_source_list_is_additive_and_warns(source_args, capsys):
    main, external, listing = source_args
    args = parse_args([f"--source={listing}", str(main)])
    assert set(discover_sources(args.program, args.cwd, args.source_files)) == {
        main,
        main.parent / "local.py",
        external,
    }
    assert "missing.py" in capsys.readouterr().err


def test_source_paths_use_launch_directory_even_with_debuggee_cwd(source_args):
    main, external, listing = source_args
    args = parse_args(
        ["--source", str(listing), "--cwd", str(external.parent), str(main)]
    )
    assert args.source_files == [external]


@pytest.mark.parametrize("kind", ["missing", "invalid_utf8", "directory"])
def test_invalid_source_list_is_cli_error(tmp_path, kind, capsys):
    path = tmp_path / "list"
    if kind == "invalid_utf8":
        path.write_bytes(b"\xff")
    elif kind == "directory":
        path.mkdir()
    with pytest.raises(SystemExit) as error:
        parse_args([f"--source={path}", "-r", "5678"])
    assert error.value.code == 2
    assert "cannot read --source" in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["launch", "attach", "post_mortem"])
def test_cli_passes_sources_to_real_app(source_args, monkeypatch, mode):
    main, external, listing = source_args
    argv = [f"--source={listing}"]
    if mode == "launch":
        argv += [str(main)]
    elif mode == "attach":
        argv += ["-r", "5678"]
    else:
        snapshot = main.parent / "snapshot.json"
        snapshot.write_text(json.dumps({}))
        argv += ["--post-mortem", str(snapshot)]
    args = parse_args(argv)
    apps = []
    monkeypatch.setattr(TdbApp, "run", lambda self: apps.append(self))
    monkeypatch.setattr("tdb.persist.load_config", TdbConfig)
    (_run_post_mortem if mode == "post_mortem" else _run_tui)(args)
    app = apps[0]
    assert external in discover_sources(app._program, app._cwd, app._source_files)


def test_cli_run_mode_receives_sources(source_args, monkeypatch):
    main, external, listing = source_args
    args = parse_args(["--run", f"--source={listing}", str(main)])
    received = {}

    async def run(**kwargs):
        received.update(kwargs)
        return 0

    monkeypatch.setattr("tdb.run_mode.run", run)
    monkeypatch.setattr("tdb.persist.load_config", TdbConfig)
    with pytest.raises(SystemExit) as result:
        _run_run(args)
    assert result.value.code == 0
    assert received["source_files"] == [external]


async def test_default_run_episode_configures_real_app(source_args, monkeypatch):
    from tdb.run_mode import ConsoleRunHandler, _default_tui_episode
    from tdb.session.controller import DebugController
    from tdb.session.event_bus import SwappableEventHandler

    main, external, _ = source_args
    console = ConsoleRunHandler()
    handler = SwappableEventHandler(console)
    controller = DebugController(handler)
    apps = []

    async def run_async(self):
        apps.append(self)

    monkeypatch.setattr(TdbApp, "run_async", run_async)
    await _default_tui_episode(
        controller,
        handler,
        console,
        TdbConfig(),
        str(main),
        source_files=[external],
        cwd=str(main.parent),
    )
    assert apps[0]._source_files == [external]
    assert apps[0]._cwd == str(main.parent)


def test_replay_builder_retains_explicit_sources(source_args):
    main, external, listing = source_args
    args = parse_args(["--replay-tui", "session.jsonl", f"--source={listing}"])
    recording = Recording(
        header={
            "mode": "launch",
            "language": "python",
            "program": str(main),
            "args": [],
            "cwd": str(main.parent),
        },
        records=[],
    )
    app, _ = build_replay_tui_app(
        recording, label="session", config=TdbConfig(), source_files=args.source_files
    )
    assert external in discover_sources(app._program, app._cwd, app._source_files)

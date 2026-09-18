"""Unit tests for PerlSession.launch()'s compile-shim availability gate.

Final review (release-blocking finding, fixed WITHOUT touching
pyproject.toml): pyproject.toml's package-data ships only helpers.pl and
TdbRemote.pm, not Devel/TdbCompile.pm. Because launch() passed
-MDevel::TdbCompile unconditionally, a built wheel installed non-editable
would make perl abort at startup ("Can't locate Devel/TdbCompile.pm in
@INC ... BEGIN failed") -- launch mode entirely broken, not merely
missing BEGIN-block stepping. This covers the defensive fallback: when
the shim file isn't present on disk, omit the -I/-M arguments and log a
warning instead of crashing the debuggee's perl process.
"""

from __future__ import annotations

import asyncio
import os

from tdb.adapters.perl import session as session_mod
from tdb.adapters.perl.session import PerlSession


class _FakeWriter:
    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeStream:
    async def read(self, n: int) -> bytes:
        return b""


class _FakeProcess:
    pid = 12345
    returncode = 0
    stdout = _FakeStream()
    stderr = _FakeStream()

    async def wait(self) -> int:
        return 0


async def _fake_start_server(client_connected_cb, host, port):
    class _FakeSock:
        def getsockname(self):
            return ("127.0.0.1", 0)

    class _FakeServer:
        sockets = [_FakeSock()]

        def close(self) -> None:
            pass

    reader = asyncio.StreamReader()
    reader.feed_eof()  # so the background read loop finishes immediately
    await client_connected_cb(reader, _FakeWriter())
    return _FakeServer()


def _make_session() -> PerlSession:
    session = PerlSession(on_output=lambda *a: None, on_stop=lambda: None)

    async def _noop_await_prompt(timeout: float, terminal: bool = False) -> None:
        pass

    async def _noop_command(text: str, timeout: float = 20.0) -> list:
        return []

    session._await_prompt = _noop_await_prompt  # type: ignore[method-assign]
    session.command = _noop_command  # type: ignore[method-assign]
    return session


async def test_launch_omits_compile_shim_when_file_missing(tmp_path, monkeypatch):
    missing_shim = tmp_path / "nowhere" / "TdbCompile.pm"
    monkeypatch.setattr(session_mod, "compile_shim_path", lambda: str(missing_shim))
    monkeypatch.setattr(session_mod.asyncio, "start_server", _fake_start_server)

    captured: dict = {}

    async def fake_create_subprocess_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        return _FakeProcess()

    monkeypatch.setattr(
        session_mod.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    session = _make_session()
    program = str(tmp_path / "prog.pl")
    await session.launch(program=program, args=[], cwd=str(tmp_path), env=None)

    argv = captured["argv"]
    assert "-MDevel::TdbCompile" not in argv
    assert not any(isinstance(a, str) and a.startswith("-I") for a in argv)
    assert argv[0] == "perl"
    assert argv[1] == "-d"
    assert program in argv
    assert "TDB_COMPILE_FILE" not in captured["env"]


async def test_launch_includes_compile_shim_when_file_present(tmp_path, monkeypatch):
    """Sanity check for the opposite branch: when the shim IS present
    (the normal, non-broken-wheel case), -I/-M are still passed and
    TDB_COMPILE_FILE is still set -- the fallback must not regress the
    working path."""
    present_shim = tmp_path / "TdbCompile.pm"
    present_shim.parent.mkdir(parents=True, exist_ok=True)
    present_shim.write_text("1;\n")
    monkeypatch.setattr(session_mod, "compile_shim_path", lambda: str(present_shim))
    monkeypatch.setattr(session_mod.asyncio, "start_server", _fake_start_server)

    captured: dict = {}

    async def fake_create_subprocess_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        return _FakeProcess()

    monkeypatch.setattr(
        session_mod.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    session = _make_session()
    program = str(tmp_path / "prog.pl")
    await session.launch(program=program, args=[], cwd=str(tmp_path), env=None)

    argv = captured["argv"]
    assert "-MDevel::TdbCompile" in argv
    assert any(isinstance(a, str) and a.startswith("-I") for a in argv)
    assert captured["env"]["TDB_COMPILE_FILE"] == program


async def _fake_run_in_terminal(cmd, cwd, env) -> None:
    del cmd, cwd, env


async def test_launch_terminal_mode_parses_pid_from_last_line(tmp_path, monkeypatch):
    """--terminal launches scrape the debuggee pid from `p $$`'s reply.

    Regression coverage for the fail-closed parse: the pid must come from
    the LAST non-empty line of the reply, matched in full (whitespace
    aside) -- not by concatenating every digit seen across every text
    event, which let a single stray digit anywhere in the reply corrupt
    the pid (stop() later SIGKILLs whatever that pid names).
    """
    monkeypatch.setattr(session_mod.asyncio, "start_server", _fake_start_server)
    session = _make_session()

    async def fake_command(text: str, timeout: float = 20.0) -> list:
        if text == "p $$":
            # A realistic perl5db reply: a leading prompt-ish line, then
            # the actual pid alone on the last line.
            return [("text", "DB<1>\n54321\n")]
        return []

    session.command = fake_command  # type: ignore[method-assign]
    program = str(tmp_path / "prog.pl")
    await session.launch(
        program=program,
        args=[],
        cwd=str(tmp_path),
        env=None,
        run_in_terminal=_fake_run_in_terminal,
    )

    assert session.debuggee_pid == 54321


async def test_launch_terminal_mode_pid_parse_fails_closed_on_stray_digit(
    tmp_path, monkeypatch, caplog
):
    """A malformed/unexpected reply must leave debuggee_pid None, not a
    corrupted value assembled from stray digits."""
    monkeypatch.setattr(session_mod.asyncio, "start_server", _fake_start_server)
    session = _make_session()

    async def fake_command(text: str, timeout: float = 20.0) -> list:
        if text == "p $$":
            # Last non-empty line is not a bare integer -- must not parse.
            return [("text", "some digit 7 appeared\n54321 extra junk\n")]
        return []

    session.command = fake_command  # type: ignore[method-assign]
    program = str(tmp_path / "prog.pl")
    with caplog.at_level("WARNING", logger="tdb.adapters.perl.session"):
        await session.launch(
            program=program,
            args=[],
            cwd=str(tmp_path),
            env=None,
            run_in_terminal=_fake_run_in_terminal,
        )

    assert session.debuggee_pid is None
    assert "could not parse debuggee pid" in caplog.text


async def test_launch_quotes_helpers_path_for_perl(tmp_path, monkeypatch):
    """helpers_path() is spliced into a Perl single-quoted string; a path
    containing an apostrophe (or backslashes, e.g. a Windows UNC share)
    must be escaped or the `do` statement misparses and helpers.pl never
    loads (same bug class as the OCaml `command script import` quoting)."""
    monkeypatch.setattr(session_mod, "helpers_path", lambda: "/home/o'brien/helpers.pl")
    monkeypatch.setattr(session_mod.asyncio, "start_server", _fake_start_server)

    async def fake_create_subprocess_exec(*argv, **kwargs):
        return _FakeProcess()

    monkeypatch.setattr(
        session_mod.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    session = _make_session()
    sent: list[str] = []

    async def capture_command(text: str, timeout: float = 20.0) -> list:
        sent.append(text)
        return []

    session.command = capture_command  # type: ignore[method-assign]
    program = str(tmp_path / "prog.pl")
    await session.launch(program=program, args=[], cwd=str(tmp_path), env=None)

    assert r"do '/home/o\'brien/helpers.pl'" in sent


def test_perl_single_quote_escapes_backslashes():
    # Windows UNC path: perl single-quote strings collapse \\ to \, so
    # every backslash must be doubled to round-trip.
    assert (
        session_mod._perl_single_quote("\\\\server\\share\\h.pl")
        == "'\\\\\\\\server\\\\share\\\\h.pl'"
    )


# --- PERL5LIB / bundled PadWalker ------------------------------------------


def test_padwalker_dir_contains_sources():
    d = session_mod.padwalker_dir()
    for name in ("PadWalker.pm", "PadWalker.xs", "Makefile.PL"):
        assert os.path.isfile(os.path.join(d, name)), name


def test_with_perl5lib_sets_when_unset():
    env = {"HOME": "/x"}
    out = session_mod.with_perl5lib(env, "/adapters")
    assert out["PERL5LIB"] == "/adapters"
    assert env == {"HOME": "/x"}  # input not mutated


def test_with_perl5lib_prepends_existing():
    env = {"PERL5LIB": "/a" + os.pathsep + "/b"}
    out = session_mod.with_perl5lib(env, "/adapters")
    assert out["PERL5LIB"] == os.pathsep.join(["/adapters", "/a", "/b"])


def test_with_perl5lib_does_not_duplicate():
    env = {"PERL5LIB": os.pathsep.join(["/a", "/adapters"])}
    out = session_mod.with_perl5lib(env, "/adapters")
    assert out["PERL5LIB"] == os.pathsep.join(["/adapters", "/a"])


def test_with_perl5lib_ignores_empty_existing():
    env = {"PERL5LIB": ""}
    out = session_mod.with_perl5lib(env, "/adapters")
    assert out["PERL5LIB"] == "/adapters"


def _stub_padwalker(monkeypatch, result):
    monkeypatch.setattr(session_mod, "ensure_padwalker", lambda perl, env: result)


async def _capture_launch(tmp_path, monkeypatch, env):
    monkeypatch.setattr(session_mod.asyncio, "start_server", _fake_start_server)
    captured: dict = {}

    async def fake_create_subprocess_exec(*argv, **kwargs):
        captured["env"] = kwargs.get("env")
        return _FakeProcess()

    monkeypatch.setattr(
        session_mod.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    outputs: list[tuple[str, str]] = []
    session = PerlSession(
        on_output=lambda t, c: outputs.append((t, c)), on_stop=lambda: None
    )

    async def _noop_await_prompt(timeout: float, terminal: bool = False) -> None:
        pass

    async def _noop_command(text: str, timeout: float = 20.0) -> list:
        return []

    session._await_prompt = _noop_await_prompt  # type: ignore[method-assign]
    session.command = _noop_command  # type: ignore[method-assign]
    await session.launch(
        program=str(tmp_path / "prog.pl"), args=[], cwd=str(tmp_path), env=env
    )
    return captured["env"], outputs


async def test_launch_prepends_padwalker_build_dir_to_perl5lib(tmp_path, monkeypatch):
    """When ensure_padwalker resolves a build dir, it must lead the
    debuggee's PERL5LIB, ahead of whatever the user already had there,
    and its notice must reach the console once."""
    from tdb.adapters.perl.padwalker import PadWalkerResult

    _stub_padwalker(
        monkeypatch, PadWalkerResult("/cache/pw", "built", "tdb: built PadWalker")
    )
    env, outputs = await _capture_launch(
        tmp_path, monkeypatch, {"PERL5LIB": "/user/lib"}
    )
    assert env["PERL5LIB"].split(os.pathsep) == ["/cache/pw", "/user/lib"]
    assert outputs == [("tdb: built PadWalker\n", "console")]


async def test_launch_leaves_perl5lib_alone_for_native_padwalker(tmp_path, monkeypatch):
    from tdb.adapters.perl.padwalker import PadWalkerResult

    _stub_padwalker(monkeypatch, PadWalkerResult(None, "native"))
    env, outputs = await _capture_launch(
        tmp_path, monkeypatch, {"PERL5LIB": "/user/lib"}
    )
    assert env["PERL5LIB"] == "/user/lib"
    assert outputs == []


async def test_launch_reports_unavailable_padwalker_and_continues(
    tmp_path, monkeypatch
):
    from tdb.adapters.perl.padwalker import PadWalkerResult

    _stub_padwalker(
        monkeypatch,
        PadWalkerResult(None, "unavailable", "tdb: PadWalker unavailable: x"),
    )
    env, outputs = await _capture_launch(tmp_path, monkeypatch, {})
    assert "PERL5LIB" not in env
    assert outputs == [("tdb: PadWalker unavailable: x\n", "console")]
    assert env["PERLDB_OPTS"].startswith("RemotePort=")

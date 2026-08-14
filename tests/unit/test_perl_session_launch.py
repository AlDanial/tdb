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

"""Unit test for BashSession.launch()'s harness-file packaging gate.

Final review (finding 5): mirrors tests/unit/test_perl_session_launch.py's
class of bug. If `tdb_harness.sh` isn't shipped as package-data in a built
wheel (or the install is otherwise broken), BashSession.launch() must fail
loudly and name the packaging problem, rather than silently trying to spawn
bash with a BASH_ENV pointing at a file that doesn't exist -- which would
degrade to a bare, uninstrumented bash run instead of a clear error.
"""

from __future__ import annotations

import pytest

from tdb.adapters.bash import session as session_mod
from tdb.adapters.bash.session import BashProtocolError, BashSession


async def test_launch_raises_when_harness_missing(tmp_path, monkeypatch):
    missing_harness = tmp_path / "nowhere" / "tdb_harness.sh"
    monkeypatch.setattr(session_mod, "HARNESS", str(missing_harness))

    session = BashSession(
        on_output=lambda *a: None,
        on_stop=lambda *a: None,
        on_exit=lambda *a: None,
    )
    program = tmp_path / "prog.sh"
    program.write_text("echo hi\n")

    with pytest.raises(BashProtocolError) as exc_info:
        await session.launch(program=str(program), args=[], cwd=str(tmp_path), env=None)

    message = str(exc_info.value)
    assert "package" in message  # names the packaging problem
    assert "reinstall" in message
    assert str(missing_harness) in message


async def test_launch_succeeds_when_harness_present_sanity(tmp_path, monkeypatch):
    """Sanity check for the opposite branch: the real (installed) harness
    file must NOT trip the gate -- this fallback must not regress the
    working path."""
    import os

    from tests.integration.bash_adapter_harness import bash_ok

    if not bash_ok():
        pytest.skip("needs bash >= 4.4")

    assert os.path.isfile(session_mod.HARNESS)

    session = BashSession(
        on_output=lambda *a: None,
        on_stop=lambda *a: None,
        on_exit=lambda *a: None,
    )
    program = tmp_path / "prog.sh"
    program.write_text("echo hi\nexit 0\n")
    await session.launch(program=str(program), args=[], cwd=str(tmp_path), env=None)
    session.resume("continue")
    await session.stop()

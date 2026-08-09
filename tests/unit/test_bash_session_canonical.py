"""canonical() must resolve relative paths against the debuggee's launch
cwd (not the adapter process's cwd), matching how tdb_harness.sh resolves
relative BASH_SOURCE entries against its own (the debuggee's) cwd.
"""

import os

from tdb.adapters.bash.session import canonical


def test_absolute_path_ignores_base(tmp_path):
    target = tmp_path / "sub" / "prog.sh"
    target.parent.mkdir()
    target.write_text("echo hi\n")
    assert canonical(str(target), base="/somewhere/else") == str(target.resolve())


def test_relative_path_resolves_against_explicit_base(tmp_path):
    launch_dir = tmp_path / "launchdir"
    launch_dir.mkdir()
    (launch_dir / "prog.sh").write_text("echo hi\n")
    # A relative path must be joined against `base` (the debuggee's launch
    # cwd), not against os.getcwd() (the adapter process's cwd) — those can
    # differ, e.g. tdb launched from one directory debugging a script in
    # another.
    result = canonical("prog.sh", base=str(launch_dir))
    assert result == str((launch_dir / "prog.sh").resolve())
    assert os.getcwd() != str(launch_dir)


def test_relative_path_falls_back_to_process_cwd_when_base_omitted(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "prog.sh").write_text("echo hi\n")
    assert canonical("prog.sh") == str((tmp_path / "prog.sh").resolve())

"""tdb's own Python children must not import from the current directory.

`python -m` puts the cwd first on sys.path, so a directory holding a
`debugpy/`, `tdb/` or `runpy.py` (any cloned repo, or a user's directory
under `sudo tdb -a`) would run that code as whoever started tdb.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from .conftest import SRC_DIR
from .powershell_adapter_harness import pwsh_ok

PLANT = "open({proof!r}, 'a').write({name!r} + '\\n')\n"

# language -> (interpreter it needs, file name, source, module its adapter
# is started from)
PROGRAMS = {
    "python": (None, "hello.py", 'print("hello-tdb")\n', "debugpy"),
    "bash": ("bash", "hello.sh", "echo hello-tdb\n", "tdb"),
    "perl": ("perl", "hello.pl", 'print "hello-tdb\\n";\n', "tdb"),
    "ruby": ("rdbg", "hello.rb", 'puts "hello-tdb"\n', "tdb"),
    "tcsh": ("tcsh", "hello.tcsh", "echo hello-tdb\n", "tdb"),
    "powershell": ("pwsh", "hello.ps1", 'Write-Output "hello-tdb"\n', "tdb"),
}


def _perl_debugger_installed():
    # Minimal installs such as Debian's perl-base leave it out.
    find = 'for (@INC) { exit 0 if -e "$_/perl5db.pl" } exit 1'
    return subprocess.run(["perl", "-e", find]).returncode == 0


def plant(directory, module, proof):
    """Leave `module` in `directory`; importing it appends to `proof`."""
    code = PLANT.format(proof=str(proof), name=module)
    if module == "runpy":
        (directory / "runpy.py").write_text(code)
    else:
        (directory / module).mkdir()
        (directory / module / "__init__.py").write_text(code)


# runpy is frozen, which beats the cwd, except under the debugpy adapter's
# -Xfrozen_modules=off. So only python can fail on it, but all should pass.
@pytest.mark.parametrize("planted", ["adapter package", "runpy"])
@pytest.mark.parametrize("language", list(PROGRAMS))
def test_session_ignores_modules_in_cwd(language, planted, tmp_path):
    needs, name, source, package = PROGRAMS[language]
    if needs and shutil.which(needs) is None:
        pytest.skip(f"{needs} not found")
    if language == "powershell" and not pwsh_ok():
        pytest.skip("PowerShell Editor Services not found")
    if language == "perl" and not _perl_debugger_installed():
        pytest.skip("perl5db.pl not found")
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    proof = tmp_path / "proof"
    plant(hostile, "runpy" if planted == "runpy" else package, proof)
    program = tmp_path / name
    program.write_text(source)

    # -P stands in for the installed `tdb` script, which has no cwd on its
    # path either; plain `python -m tdb` would be the test's own hole.
    try:
        result = subprocess.run(
            [sys.executable, "-P", "-m", "tdb", "--run", str(program)],
            cwd=hostile,
            env={
                **os.environ,
                "PYTHONPATH": str(SRC_DIR),
                "TDB_LOG_DIR": str(tmp_path),
            },
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = result.stdout + result.stderr
    except subprocess.TimeoutExpired as timed_out:
        output = f"timed out: {timed_out.stdout!r} {timed_out.stderr!r}"

    assert not proof.exists(), f"planted code ran: {proof.read_text()}"
    # Without this, a session that never started would also pass.
    assert "hello-tdb" in output, output

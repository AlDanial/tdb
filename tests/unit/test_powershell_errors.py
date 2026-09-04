from tdb.languages.errors import PS_LAUNCHER_NAME, parse_powershell_error
from tdb.languages.powershell import build_powershell_profile

THROW = """\
before
Exception: /tmp/w/e1.ps1:2
Line |
   2 |  function Inner { throw "kaboom" }
     |                   ~~~~~~~~~~~~~~
     | kaboom
"""

DOTNET = """\
x
MethodInvocationException: /tmp/w/e2.ps1:2
Line |
   2 |  $n = [int]::Parse("abc")
     |  ~~~~~~~~~~~~~~~~~~~~~~~~
     | Exception calling "Parse" with "1" argument(s): "The input string 'abc'
     | was not in a correct format."
"""

CMDLET = """\
Get-Item: /tmp/w/e4.ps1:1
Line |
   1 |  Get-Item /nonexistent/zzz
     |  ~~~~~~~~~~~~~~~~~~~~~~~~~
     | Cannot find path '/nonexistent/zzz' because it does not exist.
continues
"""

# What a real `Write-Error "bad input"` renders under the tdb launcher:
# the cmdlet reports its CALLER's invocation site, which is the launcher's
# `& $Script @ScriptArgs` line, not the user's script (spec addendum 3.2).
WRITE_ERROR = """\
Write-Error: /home/u/src/tdb/adapters/powershell/tdb_launch.ps1:12
Line |
  12 |  & $Script @ScriptArgs
     |  ~~~~
     | bad input
"""

# The same launcher-attributed block, followed by a genuine terminating
# error in the user's own script.
LAUNCHER_THEN_USER = (
    WRITE_ERROR
    + """\
Exception: /tmp/w/user.ps1:1
Line |
   1 |  throw "real"
     |  ~~~~~~~~~~~~
     | real
"""
)

ANSI_THROW = (
    "\x1b[31;1mException: \x1b[0m/tmp/w/e1.ps1:2\x1b[0m\n"
    "\x1b[31;1m\x1b[0m\x1b[36;1mLine |\x1b[0m\n"
    "     | \x1b[31;1mkaboom\x1b[0m\n"
)


def test_throw_parses_to_one_frame():
    err = parse_powershell_error(THROW, exit_code=1)
    assert err is not None
    assert err.header == "Exception: /tmp/w/e1.ps1:2"
    assert err.message == "kaboom"
    assert [(f.path, f.line, f.func) for f in err.frames] == [("/tmp/w/e1.ps1", 2, "")]
    assert err.detail.startswith("Exception: /tmp/w/e1.ps1:2")
    assert "kaboom" in err.detail


def test_dotnet_exception_joins_multiline_message():
    err = parse_powershell_error(DOTNET, exit_code=1)
    assert err is not None
    assert err.header == "MethodInvocationException: /tmp/w/e2.ps1:2"
    assert err.message == (
        'Exception calling "Parse" with "1" argument(s): '
        "\"The input string 'abc' was not in a correct format.\""
    )


def test_cmdlet_error_kind():
    err = parse_powershell_error(CMDLET, exit_code=1)
    assert err is not None
    assert err.frames[0].line == 1
    assert err.message.startswith("Cannot find path")


def test_exit_code_zero_is_not_fatal():
    assert parse_powershell_error(CMDLET, exit_code=0) is None
    assert parse_powershell_error(THROW, exit_code=None) is None


def test_write_error_block_naming_the_launcher_is_ignored():
    """`Write-Error ...; exit 3`: non-zero exit, but the only block points
    into tdb's own launcher -- no modal, and certainly not one framed on
    tdb's source (spec addendum 3.2)."""
    assert parse_powershell_error(WRITE_ERROR, exit_code=3) is None


def test_launcher_block_does_not_shadow_a_real_user_error():
    """The launcher block is skipped, not just deprioritized: the user's
    block wins even though the launcher's came first."""
    err = parse_powershell_error(LAUNCHER_THEN_USER, exit_code=1)
    assert err is not None
    assert err.header == "Exception: /tmp/w/user.ps1:1"
    assert err.message == "real"
    assert [f.path for f in err.frames] == ["/tmp/w/user.ps1"]


def test_launcher_name_matches_the_shipped_launcher():
    """PS_LAUNCHER_NAME is a copy of the launcher's filename (errors.py
    stays free of adapter imports); this catches a rename."""
    from tdb.adapters.powershell.output import LAUNCHER

    assert PS_LAUNCHER_NAME == LAUNCHER.name


def test_windows_launcher_path_is_ignored_too():
    win = WRITE_ERROR.replace(
        "/home/u/src/tdb/adapters/powershell/tdb_launch.ps1",
        r"C:\Users\u\tdb\adapters\powershell\tdb_launch.ps1",
    )
    assert parse_powershell_error(win, exit_code=3) is None


def test_ansi_is_stripped():
    err = parse_powershell_error(ANSI_THROW, exit_code=1)
    assert err is not None
    assert err.header == "Exception: /tmp/w/e1.ps1:2"
    assert err.message == "kaboom"


def test_profile_wires_parser():
    assert build_powershell_profile().presentation.parse_error is parse_powershell_error

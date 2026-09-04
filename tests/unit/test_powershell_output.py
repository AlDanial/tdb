import shutil
import subprocess

import pytest

from tdb.adapters.powershell.output import (
    EXIT_SENTINEL_PREFIX,
    LAUNCHER,
    OutputClassifier,
    parse_exit_sentinel,
    quote_ps_arg,
)


def test_quote_plain():
    assert quote_ps_arg("abc") == "'abc'"


def test_quote_space_and_apostrophe():
    assert quote_ps_arg("it's here") == "'it''s here'"


def test_quote_empty():
    assert quote_ps_arg("") == "''"


def test_sentinel_parses():
    assert parse_exit_sentinel(f"{EXIT_SENTINEL_PREFIX}7\n") == 7
    assert parse_exit_sentinel(f"{EXIT_SENTINEL_PREFIX}0") == 0


def test_sentinel_rejects_other_lines():
    assert parse_exit_sentinel("tdb-exit:7") is None  # no \x1e
    assert parse_exit_sentinel("hello") is None
    assert parse_exit_sentinel(f"{EXIT_SENTINEL_PREFIX}x") is None


def test_classifier_drops_first_prompt_echo_only():
    c = OutputClassifier()
    assert c.classify("PS /tmp/w> . '/x/tdb_launch.ps1' '/x/s.ps1' 'a'\n") is None
    assert c.classify("PS /tmp/w> . '/x/tdb_launch.ps1' '/x/s.ps1'\n") == "stdout"


def test_classifier_plain_lines_are_stdout():
    c = OutputClassifier()
    assert c.classify("hello\n") == "stdout"
    assert c.classify("Write-Error: not fatal\n") == "stdout"


def test_classifier_tags_error_block_as_stderr_until_it_ends():
    c = OutputClassifier()
    assert c.classify("before\n") == "stdout"
    assert c.classify("Exception: /tmp/w/e1.ps1:2\n") == "stderr"
    assert c.classify("Line |\n") == "stderr"
    assert c.classify('   2 |  function Inner { throw "kaboom" }\n') == "stderr"
    assert c.classify("     |                   ~~~~~~~~~~~~~~\n") == "stderr"
    assert c.classify("     | kaboom\n") == "stderr"
    assert c.classify("after\n") == "stdout"


def test_classifier_cmdlet_header():
    c = OutputClassifier()
    assert c.classify("Get-Item: /tmp/w/e4.ps1:1\n") == "stderr"


def test_classifier_ansi_header_still_detected():
    c = OutputClassifier()
    assert (
        c.classify("\x1b[31;1mException: \x1b[0m/tmp/w/e1.ps1:2\x1b[0m\n") == "stderr"
    )


def test_launcher_exists():
    assert LAUNCHER.is_file()
    assert LAUNCHER.name == "tdb_launch.ps1"


pwsh = shutil.which("pwsh")


@pytest.mark.skipif(pwsh is None, reason="pwsh not installed")
def test_launcher_reports_exit_code_and_args(tmp_path):
    s = tmp_path / "s.ps1"
    s.write_text('Write-Host ($args -join "|")\nexit 7\n')
    cp = subprocess.run(
        [pwsh, "-NoProfile", "-File", str(LAUNCHER), str(s), "one two", "it's"],
        capture_output=True,
        text=True,
        env={"NO_COLOR": "1", "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    assert "one two|it's" in cp.stdout
    assert f"{EXIT_SENTINEL_PREFIX}7" in cp.stdout


@pytest.mark.skipif(pwsh is None, reason="pwsh not installed")
def test_launcher_no_sentinel_on_throw(tmp_path):
    s = tmp_path / "t.ps1"
    s.write_text('throw "kaboom"\n')
    cp = subprocess.run(
        [pwsh, "-NoProfile", "-File", str(LAUNCHER), str(s)],
        capture_output=True,
        text=True,
        env={"NO_COLOR": "1", "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    assert EXIT_SENTINEL_PREFIX not in cp.stdout
    assert f"Exception: {s}:1" in cp.stdout + cp.stderr

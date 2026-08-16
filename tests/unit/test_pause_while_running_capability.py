"""--run needs adapters that honor DAP `pause` while the debuggee is
running. python/perl/bash do today; tcsh and cpp are enabled by later
tasks (tcsh pause handler; cpp after verification)."""

from tdb.languages.base import ProfileCapabilities
from tdb.languages.bash import build_bash_profile
from tdb.languages.perl import build_perl_profile
from tdb.languages.python import build_python_profile
from tdb.languages.tcsh import build_tcsh_profile


def test_default_is_false():
    assert ProfileCapabilities().pause_while_running is False


def test_python_perl_bash_support_pause_while_running():
    assert build_python_profile().capabilities.pause_while_running is True
    assert build_perl_profile().capabilities.pause_while_running is True
    assert build_bash_profile().capabilities.pause_while_running is True


def test_tcsh_supports_pause_while_running():
    assert build_tcsh_profile().capabilities.pause_while_running is True

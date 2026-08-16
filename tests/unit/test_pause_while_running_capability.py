"""--run needs adapters that honor DAP `pause` while the debuggee is
running. python/perl/bash/tcsh/cpp all support it now (cpp verified in
Task 9 via tests/integration/test_cpp_pause.py against both gdb and
lldb-dap)."""

from tdb.languages.base import ProfileCapabilities
from tdb.languages.bash import build_bash_profile
from tdb.languages.cpp import build_cpp_profile
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


def test_cpp_supports_pause_while_running():
    assert build_cpp_profile().capabilities.pause_while_running is True

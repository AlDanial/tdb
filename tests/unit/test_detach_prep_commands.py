"""Native debuggers intercept SIGINT; at detach they are told to pass it."""

from __future__ import annotations

import pytest

from tdb.languages.cpp import GdbDapAdapter, LldbDapAdapter
from tdb.languages.go import DelveAdapter
from tdb.languages.ocaml import EarlybirdAdapter, OCamlGdbAdapter, OCamlLldbAdapter
from tdb.languages.rust import RustGdbAdapter, RustLldbAdapter

GDB = ("handle SIGINT nostop noprint pass",)
LLDB = ("process handle SIGINT --pass true --stop false --notify false",)


@pytest.mark.parametrize("cls", [GdbDapAdapter, OCamlGdbAdapter, RustGdbAdapter])
def test_gdb_family_passes_sigint(cls):
    assert cls().detach_prep_commands() == GDB
    assert cls().quirks.interrupt_route == "pid"


@pytest.mark.parametrize("cls", [LldbDapAdapter, OCamlLldbAdapter, RustLldbAdapter])
def test_lldb_family_passes_sigint(cls):
    assert cls().detach_prep_commands() == LLDB
    assert cls().quirks.interrupt_route == "pid"


def test_pid_attach_keeps_pid_route():
    assert GdbDapAdapter(attach_pid=123).quirks.interrupt_route == "pid"
    assert OCamlGdbAdapter(attach_pid=123).quirks.interrupt_route == "pid"
    assert OCamlLldbAdapter(attach_pid=123).quirks.interrupt_route == "pid"


@pytest.mark.parametrize("cls", [DelveAdapter, EarlybirdAdapter])
def test_dlv_and_earlybird_need_no_prep(cls):
    assert cls().detach_prep_commands() == ()


def test_gdb_and_lldb_declare_their_sigint_death_codes():
    # gdb DAP reports a signal death as exit 0, lldb-dap as the signal number.
    assert GdbDapAdapter().quirks.sigint_death_codes == (0,)
    assert LldbDapAdapter().quirks.sigint_death_codes == (2,)
    assert DelveAdapter().quirks.interrupt_route == "pid"
    assert DelveAdapter(attach_pid=5).quirks.interrupt_route == "pid"

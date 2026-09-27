"""`tdb -a PID` for native (gdb/lldb-dap) debuggees: C/C++, Rust, OCaml.

The live breakpoint hooks spawn `tdb --lang <x> -a <pid> --no-pause-on-attach`;
a user may also attach by hand, with or without --lang. Detection reads the
pid's executable through /proc (Linux only).
"""

from __future__ import annotations

import pytest

from tdb.cli import parse_args
from tdb.languages import registry
from tdb.languages.base import LanguageNotSupportedError
from tdb.languages.cpp import HOOK_STOP_FUNCTION

ELF = b"\x7fELF" + b"\0" * 60
GO_BUILDINFO_MAGIC = b"\xff Go buildinf:"


@pytest.fixture
def fake_proc(tmp_path, monkeypatch):
    """Make /proc/4242/exe resolve to a file we control."""
    exe = tmp_path / "prog"
    # Both the language sniff and the program default go through _pid_exe.
    monkeypatch.setattr("tdb.cli.sys.platform", "linux")
    monkeypatch.setattr("tdb.cli._pid_exe", lambda pid: str(exe))
    return exe


@pytest.mark.parametrize("lang", ["cpp", "rust", "ocaml"])
def test_attach_pid_accepted_for_native_languages(lang, fake_proc):
    fake_proc.write_bytes(ELF)
    args = parse_args(["--lang", lang, "-a", "4242"])
    assert args.attach_pid == 4242
    assert args.attach_host == "127.0.0.1" and args.attach_port == 0
    assert args.profile.id == lang
    assert args.profile.adapter.hook_function_breakpoints() == (HOOK_STOP_FUNCTION,)
    # gdb/lldb need a symbol-bearing program: defaulted from the pid's exe.
    assert args.program == str(fake_proc)


def test_attach_pid_explicit_program_wins(fake_proc, tmp_path):
    fake_proc.write_bytes(ELF)
    other = tmp_path / "prog-with-symbols"
    other.write_bytes(ELF)
    args = parse_args(["--lang", "cpp", "-a", "4242", str(other)])
    assert args.program == str(other)


def test_attach_pid_rejected_for_python(tmp_path):
    py = tmp_path / "x.py"
    py.write_text("print(1)\n")
    with pytest.raises(SystemExit):
        parse_args(["-a", "4242", str(py)])


def test_attach_pid_rejects_earlybird(fake_proc):
    fake_proc.write_bytes(ELF)
    with pytest.raises(SystemExit):
        parse_args(["--lang", "ocaml", "--adapter", "ocamlearlybird", "-a", "4242"])


def test_no_pause_on_attach_with_native_pid(fake_proc):
    fake_proc.write_bytes(ELF)
    args = parse_args(["--lang", "cpp", "-a", "4242", "--no-pause-on-attach"])
    assert args.no_pause_on_attach is True


def test_sniff_cpp_from_plain_elf(fake_proc):
    fake_proc.write_bytes(ELF)
    assert parse_args(["-a", "4242"]).profile.id == "cpp"


def test_sniff_rust_from_runtime_symbols(fake_proc):
    fake_proc.write_bytes(ELF + b"\0rust_eh_personality\0")
    assert parse_args(["-a", "4242"]).profile.id == "rust"


def test_sniff_ocaml_from_runtime_markers(fake_proc):
    fake_proc.write_bytes(ELF + b"\0caml_startup\0caml_program\0")
    assert parse_args(["-a", "4242"]).profile.id == "ocaml"
    # native, not earlybird, since a pid cannot be bytecode-debugged
    assert parse_args(["-a", "4242"]).profile.adapter.id in ("lldb-dap", "gdb")


def test_sniff_go_still_wins(fake_proc):
    fake_proc.write_bytes(ELF + GO_BUILDINFO_MAGIC + b"\0" * 32)
    assert parse_args(["-a", "4242"]).profile.id == "go"


def test_detect_executable_rejects_non_elf(tmp_path):
    script = tmp_path / "prog"
    script.write_bytes(b"#!/usr/bin/python3\nprint(1)\n")
    with pytest.raises(LanguageNotSupportedError, match="--lang"):
        registry.detect_executable(str(script))


def test_sniff_requires_proc_off_linux(monkeypatch):
    monkeypatch.setattr("tdb.cli.sys.platform", "darwin")
    with pytest.raises(SystemExit):
        parse_args(["-a", "4242"])


def test_native_pid_attach_off_linux_needs_program(monkeypatch):
    monkeypatch.setattr("tdb.cli.sys.platform", "darwin")
    with pytest.raises(SystemExit):
        parse_args(["--lang", "cpp", "-a", "4242"])

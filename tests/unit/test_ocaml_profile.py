import sys

import pytest

from tdb.languages.base import LanguageNotSupportedError
from tdb.languages.ocaml import (
    OCamlLldbAdapter,
    _with_runparam,
    build_ocaml_profile,
    formatter_script_path,
)


def _native_launch_body(adapter):
    return adapter.launch_body(
        program="/x/main.exe",
        args=["a"],
        cwd="/x",
        env=None,
        stop_on_entry=True,
        console="integratedTerminal",
        opts={},
    )


def test_lldb_launch_body_injects_formatters_and_runparam():
    body = _native_launch_body(OCamlLldbAdapter())
    assert body["program"] == "/x/main.exe"
    assert any(
        "command script import" in c and "lldb_formatters.py" in c
        for c in body["initCommands"]
    )
    assert any(
        "caml_fatal_uncaught_exception" in c for c in body.get("preRunCommands", [])
    )
    assert "OCAMLRUNPARAM=b" in body["env"]  # lldb-dap env is a list


def test_runparam_merge_preserves_user_flags():
    assert _with_runparam(None) == {"OCAMLRUNPARAM": "b"}
    assert _with_runparam({"OCAMLRUNPARAM": "v=61"}) == {"OCAMLRUNPARAM": "v=61,b"}
    assert _with_runparam({"OCAMLRUNPARAM": "b,v=61"}) == {"OCAMLRUNPARAM": "b,v=61"}
    assert _with_runparam({"PATH": "/x"})["PATH"] == "/x"


def test_formatter_script_path_exists():
    import os

    assert os.path.isfile(formatter_script_path())


def test_default_adapter_by_flavor(tmp_path):
    native = tmp_path / "prog"
    native.write_bytes(b"\x7fELF" + b"\x00" * 64 + b"caml_program")
    byte = tmp_path / "prog.byte"
    byte.write_bytes(b"#!/usr/bin/ocamlrun\n\x00" * 4 + b"Caml1999X033")

    assert build_ocaml_profile(program=str(native)).adapter.id == "lldb-dap"
    assert build_ocaml_profile(program=str(byte)).adapter.id == "ocamlearlybird"
    assert build_ocaml_profile(program=None).adapter.id == "lldb-dap"
    assert build_ocaml_profile(adapter="gdb", program=str(native)).adapter.id == "gdb"


def test_unknown_adapter_rejected():
    with pytest.raises(LanguageNotSupportedError, match="ocamlearlybird"):
        build_ocaml_profile(adapter="nope")


def test_presentation_and_capabilities():
    p = build_ocaml_profile(program=None)  # native default
    assert p.id == "ocaml" and p.presentation.lexer == "ocaml"
    assert p.presentation.frame_placeholder == "<top>"
    assert p.presentation.parse_error is not None
    assert p.presentation.frame_name("camlMain__f_1") == "Main.f"
    assert p.capabilities.pause_while_running is True

    b = build_ocaml_profile(adapter="ocamlearlybird")
    assert b.presentation.frame_name is None
    assert b.capabilities.pause_while_running is False  # pending probe Q4


@pytest.mark.skipif(sys.platform != "win32", reason="windows-only guard")
def test_windows_rejected():
    with pytest.raises(LanguageNotSupportedError, match="Windows"):
        build_ocaml_profile()


def test_registered():
    from tdb.languages import registry

    assert "ocaml" in registry.known_languages()

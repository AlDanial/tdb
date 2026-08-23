"""Detection of OCaml executables: bytecode trailer/shebang, ELF+caml marker."""

from __future__ import annotations

import pytest

from tdb.languages.base import LanguageNotSupportedError
from tdb.languages.ocaml import ocaml_flavor
from tdb.languages import registry

ELF_MAGIC = b"\x7fELF" + b"\x00" * 60


def _write(tmp_path, name, data: bytes):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def test_bytecode_trailer(tmp_path):
    # ocamlc output: arbitrary body, trailer ends ...Caml1999X033
    p = _write(tmp_path, "prog", b"\x00" * 200 + b"Caml1999X033")
    assert ocaml_flavor(p) == "bytecode"
    assert registry.detect(p) == "ocaml"


def test_bytecode_shebang(tmp_path):
    p = _write(tmp_path, "prog", b"#!/usr/bin/ocamlrun\n" + b"\x00" * 50)
    assert ocaml_flavor(p) == "bytecode"
    assert registry.detect(p) == "ocaml"


def test_native_elf_with_caml_marker(tmp_path):
    p = _write(
        tmp_path, "prog", ELF_MAGIC + b"\x00" * 100 + b"caml_program" + b"\x00" * 100
    )
    assert ocaml_flavor(p) == "native"
    assert registry.detect(p) == "ocaml"


def test_plain_elf_stays_cpp(tmp_path):
    p = _write(tmp_path, "prog", ELF_MAGIC + b"\x00" * 300)
    assert ocaml_flavor(p) is None
    assert registry.detect(p) == "cpp"


def test_marker_in_tail_chunk_of_large_binary(tmp_path):
    # marker beyond the head chunk: found by the tail scan
    body = ELF_MAGIC + b"\x00" * (3 * 1024 * 1024) + b"caml_startup"
    p = _write(tmp_path, "prog", body)
    assert ocaml_flavor(p) == "native"


def test_ml_source_is_actionable_error(tmp_path):
    p = _write(tmp_path, "main.ml", b"let () = ()\n")
    with pytest.raises(LanguageNotSupportedError, match="dune"):
        registry.detect(p)


def test_resolve_accepts_program_kwarg():
    # every builder must tolerate program=None / a path
    for lang in registry.known_languages():
        if lang == "go":
            continue  # extension-mapped but unregistered
        registry.resolve(lang, program=None)

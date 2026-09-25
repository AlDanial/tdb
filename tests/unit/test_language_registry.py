import pytest

from tdb.languages.base import LanguageNotSupportedError
from tdb.languages import registry


def test_detect_py_extension():
    assert registry.detect("/x/prog.py") == "python"


def test_detect_none_defaults_python():
    # remote-attach mode has no program; --lang overrides upstream
    assert registry.detect(None) == "python"


@pytest.mark.parametrize(
    "magic",
    [
        b"\x7fELF\x02\x01\x01" + b"\x00" * 9,  # ELF
        b"MZ\x90\x00" + b"\x00" * 12,  # PE
        b"\xcf\xfa\xed\xfe" + b"\x00" * 12,  # Mach-O 64 LE
        b"\xca\xfe\xba\xbe" + b"\x00" * 12,  # Mach-O universal
    ],
)
def test_detect_native_binaries_as_cpp(tmp_path, magic):
    binary = tmp_path / "prog"
    binary.write_bytes(magic)
    assert registry.detect(str(binary)) == "cpp"


def test_detect_python_shebang(tmp_path):
    script = tmp_path / "tool"
    script.write_text("#!/usr/bin/env python3\nprint('hi')\n")
    assert registry.detect(str(script)) == "python"


def test_compiled_source_extension_gets_build_hint(tmp_path):
    src = tmp_path / "main.cpp"
    src.write_text("int main() {}\n")
    with pytest.raises(LanguageNotSupportedError, match="compile.*-g.*tdb ./binary"):
        registry.detect(str(src))


def test_unknown_target_errors_with_lang_hint(tmp_path):
    f = tmp_path / "data.txt"
    f.write_text("hello\n")
    with pytest.raises(LanguageNotSupportedError, match="--lang"):
        registry.detect(str(f))


def test_go_extension_maps_to_go_language(tmp_path):
    # Go files are detected by extension and resolve to the go language profile
    assert registry.detect("/x/main.go") == "go"
    assert registry.resolve("go").id == "go"


def test_resolve_python():
    assert registry.resolve("python").id == "python"


def test_resolve_unknown_language_lists_known():
    with pytest.raises(LanguageNotSupportedError, match="python"):
        registry.resolve("cobol")


def test_resolve_passes_adapter_through():
    with pytest.raises(LanguageNotSupportedError, match="gdb"):
        registry.resolve("python", adapter="gdb")


def test_detect_perl_extensions(tmp_path):
    from tdb.languages import registry

    for ext in (".pl", ".pm", ".t"):
        f = tmp_path / f"x{ext}"
        f.write_text("print 1;\n")
        assert registry.detect(str(f)) == "perl"


def test_detect_perl_shebang(tmp_path):
    from tdb.languages import registry

    f = tmp_path / "tool"
    f.write_text("#!/usr/bin/perl\nprint 1;\n")
    assert registry.detect(str(f)) == "perl"


# --- --adapter as a path to gdb / lldb-dap ---------------------------------------


def _fake_exe(tmp_path, name):
    exe = tmp_path / name
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return exe


def test_resolve_adapter_path_to_gdb(tmp_path):
    exe = _fake_exe(tmp_path, "gdb")
    profile = registry.resolve("cpp", adapter=str(exe))
    assert profile.adapter.id == "gdb"
    assert profile.adapter.command()[0] == str(exe)


def test_resolve_adapter_path_to_versioned_lldb_dap(tmp_path):
    exe = _fake_exe(tmp_path, "lldb-dap-21")
    profile = registry.resolve("rust", adapter=str(exe))
    assert profile.adapter.id == "lldb-dap"
    assert profile.adapter.command()[0] == str(exe)


def test_resolve_adapter_path_beats_config_override(tmp_path):
    exe = _fake_exe(tmp_path, "gdb")
    profile = registry.resolve(
        "cpp", adapter=str(exe), adapter_paths={"gdb": "/opt/other/gdb"}
    )
    assert profile.adapter.command()[0] == str(exe)


def test_resolve_adapter_path_for_ocaml(tmp_path):
    exe = _fake_exe(tmp_path, "lldb-dap")
    profile = registry.resolve("ocaml", adapter=str(exe))
    assert profile.adapter.id == "lldb-dap"
    assert profile.adapter.command()[0] == str(exe)


def test_resolve_adapter_path_missing_file(tmp_path):
    with pytest.raises(LanguageNotSupportedError, match="not found"):
        registry.resolve("cpp", adapter=str(tmp_path / "gdb"))


def test_resolve_adapter_path_unrecognized_basename(tmp_path):
    exe = _fake_exe(tmp_path, "lldb")
    with pytest.raises(
        LanguageNotSupportedError, match="must be named gdb, lldb-dap, or dlv"
    ):
        registry.resolve("cpp", adapter=str(exe))


def test_resolve_adapter_path_wrong_language(tmp_path):
    """A gdb path for Python is still an unknown adapter for that language."""
    exe = _fake_exe(tmp_path, "gdb")
    with pytest.raises(LanguageNotSupportedError, match="unknown adapter"):
        registry.resolve("python", adapter=str(exe))


def test_resolve_adapter_path_to_dlv(tmp_path):
    exe = _fake_exe(tmp_path, "dlv")
    profile = registry.resolve("go", adapter=str(exe))
    assert profile.adapter.id == "dlv"
    assert profile.adapter.command()[0] == str(exe)


def test_normalize_adapter_passes_ids_through():
    assert registry.normalize_adapter("gdb", {"gdb": "/opt/gdb"}) == (
        "gdb",
        {"gdb": "/opt/gdb"},
    )
    assert registry.normalize_adapter(None, None) == (None, None)


def test_normalize_adapter_maps_path_to_id_and_override(tmp_path):
    exe = _fake_exe(tmp_path, "dlv")
    assert registry.normalize_adapter(str(exe), {"gdb": "/opt/gdb"}) == (
        "dlv",
        {"gdb": "/opt/gdb", "dlv": str(exe)},
    )

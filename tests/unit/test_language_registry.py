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


def test_go_maps_to_unregistered_language(tmp_path):
    with pytest.raises(LanguageNotSupportedError, match="go.*not supported"):
        registry.resolve(registry.detect("/x/main.go"))


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

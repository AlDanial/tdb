from tdb.languages import registry
from tdb.languages.rust import is_rust_binary


def test_rust_requires_explicit_language(tmp_path):
    binary = tmp_path / "app"
    binary.write_bytes(b"\x7fELF" + b"\0" * 60)
    assert registry.detect(str(binary)) == "cpp"
    assert registry.resolve("rust").id == "rust"


def test_is_rust_binary_scans_for_runtime_symbols(tmp_path):
    f = tmp_path / "bin"
    f.write_bytes(b"\x7fELF" + b"\0" * 100)
    assert is_rust_binary(str(f)) is False
    f.write_bytes(b"\x7fELF" + b"\0" * 100 + b"__rust_alloc\0")
    assert is_rust_binary(str(f)) is True
    assert is_rust_binary(str(tmp_path / "missing")) is False

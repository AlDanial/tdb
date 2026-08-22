from tdb.languages import registry


def test_rust_requires_explicit_language(tmp_path):
    binary = tmp_path / "app"
    binary.write_bytes(b"\x7fELF" + b"\0" * 60)
    assert registry.detect(str(binary)) == "cpp"
    assert registry.resolve("rust").id == "rust"

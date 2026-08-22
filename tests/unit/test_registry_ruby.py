from tdb.languages import registry


def test_rb_extension_detects_ruby(tmp_path):
    p = tmp_path / "x.rb"
    p.write_text("puts 1\n")
    assert registry.detect(str(p)) == "ruby"


def test_ruby_shebang_detects_ruby(tmp_path):
    p = tmp_path / "script"
    p.write_text("#!/usr/bin/env ruby\nputs 1\n")
    assert registry.detect(str(p)) == "ruby"


def test_resolve_default_adapter():
    profile = registry.resolve("ruby")
    assert profile.adapter.id == "rdbg"

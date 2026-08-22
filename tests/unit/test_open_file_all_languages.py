"""File->Open ungating: extension filters + same-language validation."""

from pathlib import Path

from tdb.languages import registry
from tdb.widgets.modals import _SourceFileTree


def test_extensions_for_each_language():
    assert registry.extensions_for("python") == (".py", ".pyw")
    assert registry.extensions_for("ruby") == (".rb",)
    assert registry.extensions_for("perl") == (".pl", ".pm", ".t")
    assert registry.extensions_for("bash") == (".bash", ".sh")
    assert registry.extensions_for("cpp") == ()  # magic bytes, no suffix


def test_matches_language(tmp_path):
    rb = tmp_path / "x.rb"
    rb.write_text("puts 1\n")
    py = tmp_path / "x.py"
    py.write_text("print(1)\n")
    assert registry.matches_language(str(rb), "ruby") is True
    assert registry.matches_language(str(py), "ruby") is False
    assert registry.matches_language(str(py), "python") is True
    # unknown extension -> False, never an exception
    junk = tmp_path / "x.xyz"
    junk.write_text("?")
    assert registry.matches_language(str(junk), "python") is False


def test_source_tree_filters_by_suffix(tmp_path):
    (tmp_path / "a.rb").write_text("")
    (tmp_path / "b.py").write_text("")
    (tmp_path / "sub").mkdir()
    tree = _SourceFileTree(str(tmp_path), suffixes=(".rb",))
    kept = {p.name for p in tree.filter_paths(tmp_path.iterdir())}
    assert kept == {"a.rb", "sub"}


def test_source_tree_empty_suffixes_shows_everything(tmp_path):
    (tmp_path / "a.rb").write_text("")
    (tmp_path / "binary").write_text("")
    tree = _SourceFileTree(str(tmp_path), suffixes=())
    kept = {p.name for p in tree.filter_paths(tmp_path.iterdir())}
    assert kept == {"a.rb", "binary"}

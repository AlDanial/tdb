"""The About modal and `tdb --info` must render the same facts."""

from pathlib import Path

from tdb import __version__
from tdb.app_helpers import about_text, install_dir


def test_about_text_plain_has_no_markup():
    text = about_text(markup=False)
    assert f"textual-debugger v{__version__}" in text
    assert "[bold]" not in text and "[/bold]" not in text


def test_about_text_markup_bolds_version():
    text = about_text(markup=True)
    assert f"[bold]textual-debugger v{__version__}[/bold]" in text


def test_about_text_variants_agree_on_content():
    import re

    plain = about_text(markup=False)
    stripped = re.sub(r"\[/?bold\]", "", about_text(markup=True))
    assert plain == stripped


def test_about_text_credits_and_blurb():
    text = about_text(markup=False)
    assert "by Al Danial (with Claude Code and Codex)" in text
    assert "Copyright (c) 2026" in text
    assert (
        "A TUI debugger for many languages based on textual and the "
        "Debug Adapter Protocol." in text
    )
    # Blurb precedes the links.
    assert text.index("A TUI debugger") < text.index("GitHub:")
    assert text.index("GitHub:") < text.index("PyPI  :")


def test_install_dir_is_the_tdb_package():
    d = Path(install_dir())
    assert (d / "__init__.py").is_file()
    assert d.name == "tdb"

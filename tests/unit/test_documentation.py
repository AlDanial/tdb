"""docs/ + mkdocs.yml nav, and their assembly into one document for
`tdb --doc` / `--doc-text` (tdb.documentation)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from tdb.documentation import combine, find_docs_root, load_docs, nav_pages

REPO = Path(__file__).resolve().parents[2]
DOCS = REPO / "docs"


def _nav() -> list[str]:
    return nav_pages((REPO / "mkdocs.yml").read_text(encoding="utf-8"))


def _user_pages() -> set[str]:
    """Every published page on disk (docs/superpowers/ is excluded from
    the MkDocs site, see exclude_docs in mkdocs.yml)."""
    return {
        p.relative_to(DOCS).as_posix()
        for p in DOCS.rglob("*.md")
        if p.relative_to(DOCS).parts[0] != "superpowers"
    }


# --- nav / tree consistency -------------------------------------------------


def test_nav_lists_pages_in_order_starting_with_index():
    nav = _nav()
    assert nav[0] == "index.md"
    assert "languages/perl.md" in nav
    assert len(nav) == len(set(nav)), "page listed twice in nav"


def test_every_nav_page_exists():
    missing = [p for p in _nav() if not (DOCS / p).is_file()]
    assert missing == []


def test_every_docs_page_is_in_nav():
    """A page missing from nav would be on Read the Docs only by URL and
    absent from `tdb --doc` entirely."""
    assert _user_pages() - set(_nav()) == set()


def test_package_data_ships_every_nav_page():
    config = tomllib.loads((REPO / "pyproject.toml").read_text())
    patterns = config["tool"]["setuptools"]["package-data"]["tdb"]
    assert "mkdocs.yml" in patterns
    pkg = REPO / "src" / "tdb"
    if not (pkg / "docs").is_dir():
        pytest.skip("checkout without symlink support")
    shipped = {
        p.relative_to(pkg / "docs").as_posix()
        for pat in patterns
        if pat.startswith("docs/")
        for p in pkg.glob(pat)
        if p.is_file()
    }
    assert set(_nav()) <= shipped
    assert not any(s.startswith("superpowers/") for s in shipped)


def test_package_symlinks_point_at_repo_docs():
    pkg = REPO / "src" / "tdb"
    if not (pkg / "docs").is_dir():
        pytest.skip("checkout without symlink support")
    assert (pkg / "docs").resolve() == DOCS.resolve()
    assert (pkg / "mkdocs.yml").resolve() == (REPO / "mkdocs.yml").resolve()


def test_find_docs_root_has_config_and_pages():
    root = find_docs_root()
    assert root is not None
    assert (root / "mkdocs.yml").is_file()
    assert (root / "docs" / "index.md").is_file()


# --- nav_pages --------------------------------------------------------------


def test_nav_pages_handles_titles_bare_entries_and_quotes():
    yml = """\
site_name: x
nav:
  - Home: index.md
  - plain.md
  - Group:
      # a comment
      - "Quoted: Title": sub/a.md
      - Other: 'sub/b.md'
theme:
  name: material
  - not_nav.md
"""
    assert nav_pages(yml) == ["index.md", "plain.md", "sub/a.md", "sub/b.md"]


def test_nav_pages_without_nav_is_empty():
    assert nav_pages("site_name: x\n") == []


# --- combine ----------------------------------------------------------------


def test_combine_rewrites_cross_page_and_anchor_links():
    pages = [
        (
            "index.md",
            "# Home\n\nSee [Perl](languages/perl.md) and [hook](languages/perl.md#live-hook).\n",
        ),
        (
            "languages/perl.md",
            "# Perl\n\n## Live `hook`\n\nBack to [home](../index.md).\n",
        ),
    ]
    out = combine(pages)
    assert "[Perl](#perl)" in out
    # MkDocs slug "live-hook" -> Textual slug of the same heading
    assert "[hook](#live-hook)" in out
    assert "[home](#home)" in out


def test_combine_maps_slugs_that_differ_between_mkdocs_and_textual():
    pages = [
        ("a.md", "# A\n\nSee [run](b.md#run-mode-run).\n"),
        ("b.md", "# B\n\n## Run Mode (`--run`)\n"),
    ]
    # MkDocs collapses "--" runs; Textual (GitHub-style) keeps each dash.
    assert "[run](#run-mode---run)" in combine(pages)


def test_combine_same_page_anchor_and_duplicate_headings():
    pages = [
        ("a.md", "# Limits\n\n## Notes\n"),
        ("b.md", "# B\n\n## Notes\n\n## Notes\n\nSee [second](#notes_1).\n"),
    ]
    out = combine(pages)
    # combined doc has three "Notes": Textual numbers them notes, notes-1, notes-2;
    # b.md's second "Notes" (MkDocs notes_1) is the third overall.
    assert "[second](#notes-2)" in out


def test_combine_leaves_external_and_unknown_links_alone():
    pages = [
        (
            "a.md",
            "# A\n\n[web](https://example.com/x.md) [mail](mailto:a@b.c) "
            "[gone](missing.md) [img](pic.png)\n",
        )
    ]
    out = combine(pages)
    for link in (
        "(https://example.com/x.md)",
        "(mailto:a@b.c)",
        "(missing.md)",
        "(pic.png)",
    ):
        assert link in out


def test_combine_ignores_headings_inside_code_fences():
    pages = [
        ("a.md", "# A\n\n```bash\n# not a heading\n```\n\n[x](b.md#real)\n"),
        ("b.md", "# B\n\n## Real\n"),
    ]
    assert "[x](#real)" in combine(pages)


# --- the real documentation --------------------------------------------------


def test_load_docs_has_no_unresolved_page_links():
    docs = load_docs()
    assert docs is not None
    assert docs.startswith("# `textual-debugger`")
    leftovers = [
        m.group(0)
        for m in re.finditer(r"\]\(([^)\s]+)\)", docs)
        if not re.match(r"[a-z][a-z0-9+.-]*:", m.group(1))  # external
        and not m.group(1).startswith("#")
    ]
    assert leftovers == []


async def test_every_anchor_in_load_docs_resolves_in_markdown_viewer():
    """The anchors combine() writes must be ones Textual's own
    goto_anchor finds, or clicking them in `tdb --doc` does nothing."""
    from textual.app import App
    from textual.widgets import Markdown, MarkdownViewer

    docs = load_docs()
    assert docs is not None
    anchors = sorted(set(re.findall(r"\]\(#([^)]+)\)", docs)))
    assert len(anchors) > 10

    class _Viewer(App):
        def compose(self):
            yield MarkdownViewer(docs, show_table_of_contents=True)

    app = _Viewer()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        markdown = app.query_one(Markdown)
        unresolved = [a for a in anchors if not markdown.goto_anchor(a)]
    assert unresolved == []

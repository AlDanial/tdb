"""Assemble the multi-page docs/ tree into one Markdown document.

The documentation lives as separate pages under docs/ (published by MkDocs
on Read the Docs), with the page order given by the `nav` section of
mkdocs.yml.  `tdb --doc`, `tdb --doc-text` and Help > Documentation show
it as a single document instead, so this module concatenates the pages in
nav order and rewrites the cross-page links MkDocs needs
(`../using/console.md#some-heading`) into in-document anchors that
Textual's MarkdownViewer can follow (`#console-output-and-input`).

Both locations are searched the same way README.md used to be: walking up
from the tdb package directory, which finds the src/tdb/{mkdocs.yml,docs}
symlinks in a checkout, the repository root when symlinks are unavailable
(Windows checkouts without core.symlinks), and the copies installed as
package data in a wheel.
"""

from __future__ import annotations

import posixpath
import re
import unicodedata
from pathlib import Path

from markdown_it import MarkdownIt
from textual._slug import TrackedSlugs

_NAV_KEY = re.compile(r"^nav:\s*$")
_TOP_LEVEL_KEY = re.compile(r"^\S")
_NAV_PAGE = re.compile(r"""(?:^\s*-|:)\s*["']?([^\s"':]+\.md)["']?\s*$""")
# [text](target) where target is a relative .md page and/or #anchor.
# External links (scheme://, mailto:) never match because `:` is excluded.
_LINK = re.compile(r"\]\(([^():\s]*?\.md)?(#[^)\s]*)?\)")


def find_docs_root() -> Path | None:
    """Return the directory holding both mkdocs.yml and docs/, or None."""
    import tdb as _tdb_pkg

    pkg_dir = Path(_tdb_pkg.__file__).resolve().parent
    for parent in [pkg_dir, *pkg_dir.parents]:
        if (parent / "mkdocs.yml").is_file() and (parent / "docs").is_dir():
            return parent
    return None


def nav_pages(mkdocs_yml: str) -> list[str]:
    """Page paths (relative to docs/) listed under `nav:`, in order.

    A deliberately small reader for the `- Title: page.md` / `- page.md`
    lines MkDocs nav uses, so tdb does not need a YAML parser at runtime.
    """
    pages: list[str] = []
    in_nav = False
    for line in mkdocs_yml.splitlines():
        if _NAV_KEY.match(line):
            in_nav = True
            continue
        if in_nav and line.strip() and not line.lstrip().startswith("#"):
            if _TOP_LEVEL_KEY.match(line):
                break
            m = _NAV_PAGE.search(line)
            if m:
                pages.append(m.group(1))
    return pages


def mkdocs_slug(text: str) -> str:
    """Python-Markdown's default toc slugify (what MkDocs anchors use)."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", text)


def _headings(md: MarkdownIt, text: str) -> list[str]:
    """Plain text of each heading, in order (fenced code is skipped)."""
    tokens = md.parse(text)
    titles = []
    for i, tok in enumerate(tokens):
        if tok.type == "heading_open":
            inline = tokens[i + 1]
            titles.append(
                "".join(
                    child.content
                    for child in inline.children or []
                    if child.type in ("text", "code_inline")
                )
            )
    return titles


def combine(pages: list[tuple[str, str]]) -> str:
    """Join (path, markdown) pages into one document with working anchors.

    `path` is relative to docs/ with forward slashes.  A link to another
    page becomes a link to that page's first heading; `page.md#slug` (an
    MkDocs heading slug) becomes the Textual slug of the same heading in
    the combined document, which is what MarkdownViewer.goto_anchor
    matches.  Unresolvable targets are left untouched.
    """
    md = MarkdownIt("commonmark").enable("table")
    textual_slugs = TrackedSlugs()
    page_top: dict[str, str] = {}
    anchors: dict[tuple[str, str], str] = {}
    for path, text in pages:
        page_seen: dict[str, int] = {}
        for i, title in enumerate(_headings(md, text)):
            target = textual_slugs.slug(title)
            if i == 0:
                page_top[path] = target
            # Python-Markdown de-duplicates per page with _1, _2, ...
            base = mkdocs_slug(title)
            n = page_seen.get(base, 0)
            page_seen[base] = n + 1
            anchors[(path, base if n == 0 else f"{base}_{n}")] = target

    out = []
    for path, text in pages:
        here = posixpath.dirname(path)

        def rewrite(m: re.Match[str], path: str = path, here: str = here) -> str:
            page, anchor = m.group(1), m.group(2)
            if page is None and anchor is None:
                return m.group(0)
            dest = posixpath.normpath(posixpath.join(here, page)) if page else path
            if anchor and anchor != "#":
                target = anchors.get((dest, anchor[1:]))
            else:
                target = page_top.get(dest)
            return f"](#{target})" if target is not None else m.group(0)

        out.append(_LINK.sub(rewrite, text).rstrip("\n"))
    return "\n\n".join(out) + "\n"


def load_docs() -> str | None:
    """The whole documentation as one Markdown string, or None if the
    docs are not installed."""
    root = find_docs_root()
    if root is None:
        return None
    try:
        paths = nav_pages((root / "mkdocs.yml").read_text(encoding="utf-8"))
        pages = [(p, (root / "docs" / p).read_text(encoding="utf-8")) for p in paths]
    except OSError:
        return None
    if not pages:
        return None
    return combine(pages)

"""Pure helpers used by TdbApp that don't depend on Textual.

Kept as module-level functions (not methods on the App) so they're
trivially testable in isolation. The App imports and calls into them.
"""

from __future__ import annotations

import ast
from pathlib import Path


def install_dir() -> str:
    """Directory of the installed `tdb` package (site-packages/tdb, or
    src/tdb in a checkout / editable install)."""
    import tdb as _tdb_pkg

    return str(Path(_tdb_pkg.__file__).resolve().parent)


def about_text(*, markup: bool) -> str:
    """The About box text, shared by the Help > About modal and `tdb --info`.

    `markup=True` wraps the title line in Rich/Textual [bold] tags for the
    modal; `markup=False` yields plain text for the terminal.
    """
    from textwrap import dedent

    from tdb import __version__

    title = f"tdb v{__version__}"
    if markup:
        title = f"[bold]{title}[/bold]"
    return dedent(f"""\
        {title}
        by Al Danial (with Claude Code)
        Copyright (c) 2026

        GitHub: https://github.com/AlDanial/tdb
        PyPI  : https://pypi.org/project/textual-debugger/

        A TUI Python debugger based on debugpy and textual""")


def info_text() -> str:
    """Body of `tdb --info`: where tdb lives, where the bundled PadWalker
    sources and per-perl builds live (so Perl users can put a build on
    PERL5LIB by hand, e.g. for a remote debuggee), then the About text.
    Read-only: never triggers a build."""
    from tdb.adapters.perl.padwalker import cache_root, padwalker_dir, padwalker_status

    return (
        about_text(markup=False)
        + "\n\n"
        f"tdb installation directory   : {install_dir()}\n"
        f"Perl PadWalker sources       : {padwalker_dir()}\n"
        f"Perl PadWalker build cache   : {cache_root()}\n"
        f"Perl PadWalker status        : {padwalker_status()}\n"
        "  (tdb builds PadWalker for each perl it launches, caches it, and\n"
        "   prepends the cache directory to the debuggee's PERL5LIB)"
    )


def find_readme() -> str | None:
    """Locate README.md across install layouts.

    Order: walk up from the tdb package dir (catches source checkouts and
    editable installs); try importlib.resources for a bundled README
    (catches pip installs that package it alongside the code); finally try
    the distribution's metadata directory.
    """
    import tdb as _tdb_pkg

    pkg_dir = Path(_tdb_pkg.__file__).resolve().parent
    for parent in [pkg_dir, *pkg_dir.parents]:
        candidate = parent / "README.md"
        if candidate.is_file():
            try:
                return candidate.read_text(encoding="utf-8")
            except OSError:
                pass

    try:
        import importlib.resources as ires

        with ires.files("tdb").joinpath("README.md").open("r", encoding="utf-8") as f:
            return f.read()
    except (FileNotFoundError, ModuleNotFoundError, AttributeError, OSError):
        pass

    try:
        import importlib.metadata as md

        text = md.distribution("textual-debugger").read_text("README.md")
        if text:
            return text
    except Exception:
        pass

    return None


def unquote_dap_string(s: str) -> str:
    """Strip repr quoting from a DAP evaluate result that is a Python string.

    debugpy returns string results as their repr (e.g. "'hello\\nworld'").
    This converts that back to the actual string content.
    """
    if not s:
        return s
    try:
        value = ast.literal_eval(s)
        if isinstance(value, str):
            return value
    except (ValueError, SyntaxError):
        pass
    return s

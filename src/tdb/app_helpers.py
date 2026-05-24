"""Pure helpers used by TdbApp that don't depend on Textual.

Kept as module-level functions (not methods on the App) so they're
trivially testable in isolation. The App imports and calls into them.
"""

from __future__ import annotations

import ast
from pathlib import Path


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

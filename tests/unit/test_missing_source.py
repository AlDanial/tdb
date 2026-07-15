"""A stack frame whose source file doesn't exist on disk must degrade to
a placeholder pane, not crash — C++ system-library frames hit this
constantly (DWARF compile-dir paths)."""

from tdb.app import TdbApp
from tdb.persist import TdbConfig
from tdb.widgets.code_view import CodeView

SNAPSHOT = {
    "version": 1,
    "exception": {"type": "X", "message": "m", "traceback_text": "tb"},
    "frames": [
        {
            "id": 1,
            "filename": "/nonexistent/path/lib.cpp",
            "lineno": 3,
            "funcname": "boom",
            "scopes": [{"name": "Locals", "variablesReference": 1001}],
        }
    ],
    "variables": {"1001": []},
}


async def test_missing_source_shows_placeholder():
    app = TdbApp(program="", config=TdbConfig(), post_mortem_snapshot=SNAPSHOT)
    async with app.run_test() as pilot:
        await pilot.pause()
        code_view = app.query_one("#code-view", CodeView)
        rendered = "\n".join(str(line) for line in code_view._lines)
        assert "Could not read" in rendered

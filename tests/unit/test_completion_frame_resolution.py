"""Tab completion must resolve a live frame id the same way evaluate does
(synthetic-frame fallback via resolve_evaluate_frame_id) and surface DAP
errors in the evaluate console instead of dying silently."""

from __future__ import annotations

from types import SimpleNamespace

from textual.widgets import RichLog

from tdb.app import TdbApp
from tdb.persist import TdbConfig
from tdb.widgets.evaluate_console import EvaluateConsole

SNAPSHOT = {
    "version": 1,
    "exception": {"type": "X", "message": "m", "traceback_text": "tb"},
    "frames": [
        {
            "id": 1,
            "filename": "/nonexistent/path/prog.py",
            "lineno": 3,
            "funcname": "boom",
            "scopes": [{"name": "Locals", "variablesReference": 1001}],
        }
    ],
    "variables": {"1001": []},
}


class _FakeClient:
    def __init__(self, error: Exception | None = None):
        self.calls: list[dict] = []
        self._error = error

    async def completions(self, text, column, frame_id=None):
        self.calls.append({"text": text, "column": column, "frame_id": frame_id})
        if self._error is not None:
            raise self._error
        return []


def _fake_controller(client: _FakeClient, resolved_frame_id: int | None):
    async def resolve(c):
        assert c is client  # must resolve against the same client it queries
        return resolved_frame_id

    return SimpleNamespace(
        active_client=client,
        resolve_evaluate_frame_id=resolve,
        # A stale id that must NOT be handed to DAP directly.
        state=SimpleNamespace(current_frame_id=999),
    )


async def test_completion_uses_resolved_frame_id():
    app = TdbApp(program="", config=TdbConfig(), post_mortem_snapshot=SNAPSHOT)
    async with app.run_test() as pilot:
        await pilot.pause()
        client = _FakeClient()
        app.controller = _fake_controller(client, resolved_frame_id=777)
        msg = EvaluateConsole.CompletionRequested("x.", 3)
        await app.on_evaluate_console_completion_requested(msg)
        assert client.calls == [{"text": "x.", "column": 3, "frame_id": 777}]


async def test_completion_error_surfaces_in_console():
    app = TdbApp(program="", config=TdbConfig(), post_mortem_snapshot=SNAPSHOT)
    async with app.run_test() as pilot:
        await pilot.pause()
        client = _FakeClient(error=RuntimeError("frame gone"))
        app.controller = _fake_controller(client, resolved_frame_id=5)
        msg = EvaluateConsole.CompletionRequested("x.", 3)
        await app.on_evaluate_console_completion_requested(msg)
        output = app.query_one("#eval-output", RichLog)
        rendered = "\n".join(str(line) for line in output.lines)
        assert "frame gone" in rendered

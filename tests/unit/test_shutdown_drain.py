"""Regression: TdbApp._drain_writer_queue_fast must drop bulky render
frames from Textual's writer-thread queue at quit time.

Without this drop, quitting after a `tdb.breakpoint()` session leaves
the writer thread joining for many seconds while it flushes a backlog
of queued render frames through the pty before the alt-screen-exit
sequence (which is at the END of the backlog) can fire. The user sees
the TUI vanish but the process stays alive and the terminal seems
frozen. See `project_breakpoint_hook.md` for the full incident.

These tests pin the contract of `_drain_writer_queue_fast` directly
(no Textual or DAP), so a future change to the heuristic that
re-introduces the backlog-flush failure mode fails here.
"""

from __future__ import annotations

import queue as _queue
import types

from tdb.app import TdbApp


def _fake_app_with_queue(items: list[str | None]) -> TdbApp:
    """Build a bare TdbApp with a stubbed driver/_writer_thread/queue.

    We don't run the Textual app — we just call the unbound method with a
    minimally-shaped self. This keeps the test fast and free of pty
    machinery.
    """
    q: _queue.Queue = _queue.Queue(maxsize=64)
    for item in items:
        q.put_nowait(item)

    driver = types.SimpleNamespace(
        _writer_thread=types.SimpleNamespace(_queue=q),
    )
    app = TdbApp.__new__(TdbApp)  # bypass __init__
    app._driver = driver  # type: ignore[attr-defined]
    return app


def test_drain_drops_large_render_frames():
    """A 30 KB render frame is exactly what Textual queues per repaint."""
    items = ["x" * 30_000, "x" * 8_000]
    app = _fake_app_with_queue(items)
    dropped = app._drain_writer_queue_fast()
    assert dropped == 2
    q = app._driver._writer_thread._queue
    assert q.qsize() == 0


def test_drain_keeps_short_control_sequences():
    """The terminal-restoration sequences (`ESC[?2004l` etc.) are all
    short — they MUST survive the drain so the alt-screen actually
    exits and the cursor is restored when the writer thread joins."""
    restoration = [
        "\x1b[?2004l",   # disable bracketed paste
        "\x1b[?7h",      # enable line wrap
        "\x1b[?1000l",   # disable mouse 1000
        "\x1b[?1003l",   # disable mouse 1003
        "\x1b[<u",       # disable kitty kbd
        "\x1b[?1049l",   # alt-screen off  ← critical
        "\x1b[?25h",     # show cursor
        "\x1b[?1004l",   # disable focus events
    ]
    app = _fake_app_with_queue(restoration)
    dropped = app._drain_writer_queue_fast()
    assert dropped == 0
    q = app._driver._writer_thread._queue
    # Pull them back out and verify order is preserved (FIFO).
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    assert out == restoration


def test_drain_mixed_keeps_only_short_items():
    """The realistic case at quit time: a queue of large render frames
    interleaved with the short restoration sequences. The drain must
    drop the renders and keep the restoration so the alt-screen-exit
    fires immediately when the writer thread next reads."""
    items = [
        "X" * 30_000,        # render frame
        "X" * 30_000,        # render frame
        "\x1b[?2004l",       # disable bracketed paste — keep
        "X" * 924,           # render fragment — drop (>64 B)
        "\x1b[?1049l",       # alt screen off — keep
        "\x1b[?25h",         # show cursor — keep
    ]
    app = _fake_app_with_queue(items)
    dropped = app._drain_writer_queue_fast()
    assert dropped == 3
    q = app._driver._writer_thread._queue
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    assert out == ["\x1b[?2004l", "\x1b[?1049l", "\x1b[?25h"]


def test_drain_preserves_none_sentinel_and_short_items():
    """The writer thread exits when it dequeues None. If a None is
    already in the queue at drain time we must keep it, otherwise the
    thread will spin on the (now-empty) queue and never join."""
    items: list = ["\x1b[?1049l", None, "X" * 5_000]
    app = _fake_app_with_queue(items)
    dropped = app._drain_writer_queue_fast()
    assert dropped == 1  # only the 5 KB render chunk
    q = app._driver._writer_thread._queue
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    assert out == ["\x1b[?1049l", None]


def test_drain_no_driver_or_writer_returns_zero():
    """Headless / post-mortem builds may not have a writer thread at all;
    the drain must be a clean no-op (and never raise) in that case."""
    app = TdbApp.__new__(TdbApp)
    app._driver = None  # type: ignore[attr-defined]
    assert app._drain_writer_queue_fast() == 0

    app2 = TdbApp.__new__(TdbApp)
    app2._driver = types.SimpleNamespace(_writer_thread=None)  # type: ignore[attr-defined]
    assert app2._drain_writer_queue_fast() == 0

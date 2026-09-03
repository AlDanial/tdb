"""tests/unit/test_goroutines_modal.py"""

from tdb.go_concurrency.models import (
    Confidence,
    GoFinding,
    GoFindingKind,
    GoroutineInfo,
    GoroutineSnapshot,
    GoroutineState,
)
from tdb.widgets.goroutines_modal import GoroutinesModal


def _g(tid, state=GoroutineState.RUNNING, runtime=False, res=None, op=None):
    return GoroutineInfo(tid, tid, f"main.f{tid}", state, op, res, (), runtime)


def _snap(goroutines, findings=(), uncollected=0):
    return GoroutineSnapshot(
        goroutines=tuple(goroutines),
        resources=(),
        edges=(),
        findings=tuple(findings),
        uncollected=uncollected,
        warnings=(),
    )


def test_runtime_goroutines_hidden_by_default():
    m = GoroutinesModal(_snap([_g(1), _g(2, GoroutineState.RUNTIME, runtime=True)]), 1)
    assert [g.thread_id for g in m.visible_items()] == [1]
    m._show_runtime = True
    assert [g.thread_id for g in m.visible_items()] == [1, 2]


def test_finding_members_marked():
    f = GoFinding(GoFindingKind.STUCK_CHANNEL, (2,), "stuck", Confidence.CONFIRMED)
    m = GoroutinesModal(_snap([_g(1), _g(2)], findings=[f]), 1)
    assert not m._in_finding(1)
    assert m._in_finding(2)


def test_header_reports_uncollected():
    m = GoroutinesModal(_snap([_g(1)], uncollected=7), 1)
    assert "7 more not collected" in m._header_text()


def test_row_shows_wait_target():
    g = _g(3, GoroutineState.CHAN_RECV, res="chan:0xc000024180", op="recv")
    m = GoroutinesModal(_snap([g]), 1)
    cells = m._format_row(g)
    assert any("chan:0xc000024180" in str(c) for c in cells)

"""DebugState.install_cli_breakpoints: the single install path shared by
the TUI (app.on_mount) and headless (run_headless) for -k / -t CLI
breakpoints. -t entries are installed with persist=False; existing
breakpoints at the same spot are never duplicated or downgraded."""

from tdb.dap.types import SourceBreakpoint
from tdb.session.state import DebugState


def test_install_sets_persist_flag_per_entry():
    state = DebugState()
    state.install_cli_breakpoints([("/abs/x.py", 1, True), ("/abs/x.py", 2, False)])
    bps = state.breakpoints["/abs/x.py"]
    assert [(b.line, b.persist) for b in bps] == [(1, True), (2, False)]


def test_install_does_not_duplicate_or_downgrade_existing():
    state = DebugState()
    state.breakpoints["/abs/x.py"] = [SourceBreakpoint(line=7)]
    state.install_cli_breakpoints([("/abs/x.py", 7, False)])
    bps = state.breakpoints["/abs/x.py"]
    assert len(bps) == 1
    assert bps[0].persist is True  # saved breakpoint stays persistent

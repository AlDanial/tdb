"""End-to-end (Python/debugpy): the gdb-style display list.

`display b` typed at the Evaluate console puts `b` under a "Display"
scope at the same stop and after a step; once execution leaves the
frame that defines `b`, the scope disappears while the expression stays
on the list, ready to reappear. `undisplay b` drops it for good.
"""

from __future__ import annotations

import asyncio

from tdb.dap.types import SourceBreakpoint
from tdb.languages import registry
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController
from tdb.session.state import DISPLAY_SCOPE_REF

WAIT = 30.0

SOURCE = """\
def f(a):
    b = a * 2
    c = b + 1
    return c

x = f(3)
y = x + 1
print(y)
"""
BP_IN_F = 3  # `c = b + 1`: b is defined
BP_TOP = 7  # `y = x + 1`: b is not


def _display(ctrl) -> dict[str, str] | None:
    if not ctrl.state.scopes or ctrl.state.scopes[0].name != "Display":
        assert DISPLAY_SCOPE_REF not in ctrl.state.variables
        return None
    return {v.name: v.value for v in ctrl.state.variables[DISPLAY_SCOPE_REF]}


async def test_display_follows_frame_definition(tmp_path):
    src = tmp_path / "p.py"
    src.write_text(SOURCE)
    program = str(src)
    profile = registry.resolve("python", program=program)
    handler = ServerEventHandler()
    ctrl = DebugController(handler, profile=profile)
    try:
        ctrl.state.breakpoints[str(src)] = [
            SourceBreakpoint(line=BP_IN_F),
            SourceBreakpoint(line=BP_TOP),
        ]
        await ctrl.start(program=program, stop_on_entry=False)
        await asyncio.wait_for(handler.initialized_event.wait(), WAIT)
        await ctrl.do_configure()
        assert await handler.wait_for_stop(timeout=WAIT)
        await ctrl.fetch_stop_info()
        assert _display(ctrl) is None

        assert await ctrl.evaluate_console("display b") == "display b"
        # Same stop: the console command alone refreshed the scopes.
        assert _display(ctrl) == {"b": "6"}

        handler.reset_for_continue()
        await ctrl.step_over()
        assert await handler.wait_for_stop(timeout=WAIT)
        await ctrl.fetch_stop_info()
        assert _display(ctrl) == {"b": "6"}

        # Leave f: b is undefined at module level, so the scope is gone
        # but the expression is still on the list.
        handler.reset_for_continue()
        await ctrl.continue_()
        assert await handler.wait_for_stop(timeout=WAIT)
        await ctrl.fetch_stop_info()
        assert ctrl.state.stack_frames[0].line == BP_TOP
        assert _display(ctrl) is None
        assert ctrl.state.display == ["b"]

        assert await ctrl.evaluate_console("display x") == "display x"
        assert _display(ctrl) == {"x": "7"}
        assert await ctrl.evaluate_console("display") == "b\nx"
        assert await ctrl.evaluate_console("undisplay x") == "undisplay x"
        assert _display(ctrl) is None
        assert ctrl.state.display == ["b"]
    finally:
        try:
            await asyncio.wait_for(ctrl.stop(), timeout=WAIT)
        except Exception:
            pass

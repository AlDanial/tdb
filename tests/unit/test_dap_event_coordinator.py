"""Unit tests for DapEventCoordinator (#1d split).

These exercise the helpers (`_stopped_inside_breakpoint_hook`, the
exception/traceback modal builders) that are pure logic on top of
controller state — no Textual app needed.
"""

from __future__ import annotations

from types import SimpleNamespace

from tdb.app_handlers.dap_events import DapEventCoordinator
from tdb.dap.types import Source, StackFrame
from tdb.server.event_handler import ServerEventHandler
from tdb.app_handlers.ui_panels import UIPanels
from tdb.session.controller import DebugController


class _StubApp:
    def __init__(self) -> None:
        self.controller = DebugController(ServerEventHandler())
        self._stderr_buffer: list[str] = []
        self.panels = UIPanels()
        self.pushed_screens: list[object] = []

    def push_screen(self, screen, callback=None):
        self.pushed_screens.append(screen)

    def _restart_session(self):  # only invoked via dismiss-callback paths
        pass


def _coord() -> tuple[DapEventCoordinator, _StubApp]:
    app = _StubApp()
    return DapEventCoordinator(app), app


# --- _stopped_inside_breakpoint_hook -----------------------------------


def test_breakpoint_hook_check_false_when_not_remote_attach():
    co, app = _coord()
    app.controller._is_remote_attach = False
    app.controller.state.stack_frames = [
        StackFrame(
            id=1,
            name="breakpoint",
            source=Source(path="/x/tdb/breakpoint_hook.py"),
            line=87,
        ),
    ]
    assert co._stopped_inside_breakpoint_hook() is False


def test_breakpoint_hook_check_false_when_no_frames():
    co, app = _coord()
    app.controller._is_remote_attach = True
    app.controller.state.stack_frames = []
    assert co._stopped_inside_breakpoint_hook() is False


def test_breakpoint_hook_check_false_when_top_frame_has_no_source():
    co, app = _coord()
    app.controller._is_remote_attach = True
    app.controller.state.stack_frames = [
        StackFrame(id=1, name="breakpoint", source=None, line=10),
    ]
    assert co._stopped_inside_breakpoint_hook() is False


def test_breakpoint_hook_check_true_for_basename_match():
    co, app = _coord()
    app.controller._is_remote_attach = True
    app.controller.state.stack_frames = [
        StackFrame(
            id=1,
            name="breakpoint",
            source=Source(path="/site-packages/tdb/breakpoint_hook.py"),
            line=87,
        ),
    ]
    assert co._stopped_inside_breakpoint_hook() is True


def test_breakpoint_hook_check_false_for_other_files():
    co, app = _coord()
    app.controller._is_remote_attach = True
    app.controller.state.stack_frames = [
        StackFrame(
            id=1, name="user_func", source=Source(path="/home/me/script.py"), line=5
        ),
    ]
    assert co._stopped_inside_breakpoint_hook() is False


def test_breakpoint_hook_check_false_for_non_python_profile():
    """`tdb.breakpoint()` is a Python-only helper; a non-Python profile
    (e.g. cpp) must never be treated as stopped inside it, even if the
    other conditions (remote attach, matching basename) hold."""
    from tests.unit.test_controller_actions import _bare_profile

    co, app = _coord()
    app.controller.profile = _bare_profile()
    app.controller._is_remote_attach = True
    app.controller.state.stack_frames = [
        StackFrame(
            id=1,
            name="breakpoint",
            source=Source(path="/site-packages/tdb/breakpoint_hook.py"),
            line=87,
        ),
    ]
    assert co._stopped_inside_breakpoint_hook() is False


# --- _check_stderr_traceback parsing -----------------------------------


def test_check_stderr_traceback_no_traceback_does_nothing():
    co, app = _coord()
    app._stderr_buffer = ["hello\n", "world\n"]
    co._check_stderr_traceback()
    assert app.pushed_screens == []


def test_check_stderr_traceback_parses_simple_tb():
    co, app = _coord()
    app._stderr_buffer = [
        "Traceback (most recent call last):\n",
        '  File "/foo/bar.py", line 7, in main\n',
        "    1 / 0\n",
        "ZeroDivisionError: division by zero\n",
    ]
    co._check_stderr_traceback()
    # Modal pushed, synthetic stack frame populated.
    assert len(app.pushed_screens) == 1
    assert app.controller.state.stack_frames
    top = app.controller.state.stack_frames[0]
    assert top.source.path == "/foo/bar.py"
    assert top.line == 7
    assert top.name == "main"


# --- exit-code gate threading (Task 4) ----------------------------------


def _perl_coord() -> tuple[DapEventCoordinator, _StubApp]:
    from tdb.languages import registry

    co, app = _coord()
    app.controller.profile = registry.resolve("perl")
    return co, app


def test_check_stderr_traceback_perl_unlisted_warning_clean_exit_no_modal():
    co, app = _perl_coord()
    app._stderr_buffer = ['Deep recursion on subroutine "main::f" at /w/x.pl line 3.\n']
    co._check_stderr_traceback(exit_code=0)
    assert app.pushed_screens == []


def test_check_stderr_traceback_perl_unlisted_warning_nonzero_exit_shows_modal():
    co, app = _perl_coord()
    app._stderr_buffer = ['Deep recursion on subroutine "main::f" at /w/x.pl line 3.\n']
    co._check_stderr_traceback(exit_code=255)
    assert len(app.pushed_screens) == 1


async def test_wait_for_exit_code_returns_immediately_when_already_set():
    co, app = _coord()
    app.controller.state.last_exit_code = 5
    result = await co._wait_for_exit_code(max_wait=5.0)
    assert result == 5


async def test_wait_for_exit_code_falls_back_to_none_after_bound():
    co, app = _coord()
    assert app.controller.state.last_exit_code is None
    result = await co._wait_for_exit_code(max_wait=0.05)
    assert result is None


def test_check_stderr_traceback_chained_uses_final_block_for_frames():
    """When multiple traceback blocks chain, synthetic frames come from
    the LAST block (the exception that actually killed the program)."""
    co, app = _coord()
    app._stderr_buffer = [
        "Traceback (most recent call last):\n",
        '  File "/inner.py", line 1, in inner\n',
        "    raise ValueError\n",
        "ValueError\n",
        "\nThe above exception was the direct cause of the following exception:\n\n",
        "Traceback (most recent call last):\n",
        '  File "/outer.py", line 22, in outer\n',
        "    inner()\n",
        "RuntimeError: wrapped\n",
    ]
    co._check_stderr_traceback()
    assert app.controller.state.stack_frames
    top = app.controller.state.stack_frames[0]
    assert top.source.path == "/outer.py"
    assert top.line == 22

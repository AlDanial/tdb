"""Unit tests for DapEventCoordinator (#1d split).

These exercise the helpers (`_stopped_inside_breakpoint_hook`, the
exception/traceback modal builders) that are pure logic on top of
controller state — no Textual app needed.
"""

from __future__ import annotations

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


def test_check_stderr_traceback_perl_innermost_frame_uses_main_not_module():
    """Final-review Minor #5: perl's innermost error frame always has
    func="" (top-level/BEGIN code, no named sub -- see
    languages/errors.py's parse_perl_error), and must not borrow
    Python's "<module>" placeholder for it. It should match perl's own
    live stackTrace convention instead (adapters/perl/server.py's
    `f.get("sub") or "main"`)."""
    co, app = _perl_coord()
    app._stderr_buffer = ["Illegal division by zero at /w/x.pl line 10.\n"]
    co._check_stderr_traceback(exit_code=255)
    assert app.controller.state.stack_frames
    top = app.controller.state.stack_frames[0]
    assert top.name == "main"


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


def test_breakpoint_hook_check_true_for_go_hook_frame():
    """The Go hook stops inside `tdb.Breakpoint()` (dlv hides the
    runtime.Breakpoint frame beneath it); the frame is identified by its
    fully qualified function name, since the module lives wherever `go get`
    put it."""
    from tdb.languages.go import build_go_profile

    co, app = _coord()
    app.controller.profile = build_go_profile(attach_pid=1)
    app.controller._is_remote_attach = True
    app.controller.state.stack_frames = [
        StackFrame(
            id=1,
            name="github.com/AlDanial/tdb/go/tdb.Breakpoint",
            source=Source(path="/go/pkg/mod/github.com/aldanial/tdb/go/tdb/tdb.go"),
            line=40,
        ),
    ]
    assert co._stopped_inside_breakpoint_hook() is True


def test_breakpoint_hook_check_false_for_go_user_frame():
    from tdb.languages.go import build_go_profile

    co, app = _coord()
    app.controller.profile = build_go_profile(attach_pid=1)
    app.controller._is_remote_attach = True
    app.controller.state.stack_frames = [
        StackFrame(
            id=1,
            name="main.compute",
            source=Source(path="/home/me/prog/main.go"),
            line=12,
        ),
    ]
    assert co._stopped_inside_breakpoint_hook() is False


# --- hook-frame step-out cap --------------------------------------------


def _hook_stop_coord(hook_frames: list[bool]):
    """A coordinator whose controller reports a stop per entry of
    `hook_frames` (True = top frame is a hook frame) and records step-outs
    and UI renders instead of talking to an adapter."""
    import asyncio

    from tdb.languages.cpp import build_cpp_profile

    co, app = _coord()
    ctrl = app.controller
    ctrl.profile = build_cpp_profile(attach_pid=1)
    ctrl._is_remote_attach = True
    calls = {"step_out": 0, "rendered": 0}
    frames = iter(hook_frames)

    async def fetch_stop_info():
        name = "tdb_breakpoint_stop" if next(frames) else "main"
        ctrl.state.stack_frames = [
            StackFrame(id=1, name=name, source=Source(path="/p/a.c"), line=3)
        ]

    async def step_out():
        calls["step_out"] += 1

    async def no():
        return False

    async def nothing():
        return None

    ctrl.fetch_stop_info = fetch_stop_info  # type: ignore[method-assign]
    ctrl.step_out = step_out  # type: ignore[method-assign]
    ctrl.maybe_continue_statement_step = no  # type: ignore[method-assign]
    ctrl.cleanup_run_to_cursor = nothing  # type: ignore[method-assign]

    def rendered():
        calls["rendered"] += 1

    app._update_ui_state = rendered
    app._update_thread_count = lambda: None
    app._fetch_process_count = lambda: None
    app._fetch_async_task_count = lambda: None
    app.stop_settled = asyncio.Event()
    return co, calls


async def test_hook_step_out_is_capped_per_stop_episode():
    from tdb.app_handlers.dap_events import MAX_HOOK_STEP_OUTS
    from tdb.session.messages import DapStopped

    co, calls = _hook_stop_coord([True] * (MAX_HOOK_STEP_OUTS + 1))
    for _ in range(MAX_HOOK_STEP_OUTS + 1):
        await co.on_stopped(DapStopped(1, "step"))
    # Eight step-outs, then the ninth hook stop is rendered where it is.
    assert calls["step_out"] == MAX_HOOK_STEP_OUTS
    assert calls["rendered"] == 1
    assert co._hook_step_outs == 0


async def test_hook_step_out_count_resets_after_a_normal_stop():
    from tdb.app_handlers.dap_events import MAX_HOOK_STEP_OUTS
    from tdb.session.messages import DapStopped

    # A few hook frames, a normal stop, then a full budget again.
    pattern = [True] * 3 + [False] + [True] * MAX_HOOK_STEP_OUTS + [False]
    co, calls = _hook_stop_coord(pattern)
    for _ in pattern:
        await co.on_stopped(DapStopped(1, "breakpoint"))
    assert calls["step_out"] == 3 + MAX_HOOK_STEP_OUTS
    assert calls["rendered"] == 2

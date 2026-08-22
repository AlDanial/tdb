"""A stop landing on a hidden (backup) thread re-points to a visible one.

Scenario: a pause lands with current_thread_id=2, an OCaml backup thread
whose stack is all runtime frames. classify_threads marks thread 2
hidden; fetch_stop_info must switch state.current_thread_id to thread 1
(Domain 0/main) and fetch THAT stack.
"""

from tdb.dap.types import StackFrame, Thread
from tdb.languages.ocaml import build_ocaml_profile
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController

from tests.unit.test_controller_actions import _FakeDAP


class _PerThreadDAP(_FakeDAP):
    """_FakeDAP with per-thread stack_trace results."""

    def __init__(self):
        super().__init__()
        self.frames_by_thread: dict[int, list[StackFrame]] = {}

    async def stack_trace(self, thread_id, start_frame=0, levels=20):
        self._hit("stackTrace", thread_id)
        return self.frames_by_thread.get(thread_id, [])


def _make_ocaml_ctrl(current_thread_id: int):
    ctrl = DebugController(
        ServerEventHandler(), profile=build_ocaml_profile(program=None)
    )
    fake = _PerThreadDAP()
    fake.threads_result = [Thread(id=1, name="prog"), Thread(id=2, name="prog")]
    fake.frames_by_thread = {
        1: [
            StackFrame(id=101, name="camlMain__entry", line=3),
            StackFrame(id=102, name="caml_start_program", line=0),
        ],
        2: [
            StackFrame(id=201, name="caml_thread_condwait", line=0),
            StackFrame(id=202, name="backup_thread_func", line=0),
        ],
    }
    ctrl.client = fake
    ctrl._active_client = fake
    ctrl.state.enter_stop(thread_id=current_thread_id, reason="pause")
    ctrl.state.current_thread_id = current_thread_id
    return ctrl, fake


async def test_stop_on_backup_thread_repoints_to_domain():
    ctrl, fake = _make_ocaml_ctrl(current_thread_id=2)
    await ctrl.fetch_stop_info()
    assert ctrl.state.current_thread_id == 1
    assert ctrl.state.stack_frames[0].name == "camlMain__entry"
    assert fake.calls_to("scopes") == [("scopes", 101)]


async def test_stop_on_visible_thread_unchanged():
    ctrl, fake = _make_ocaml_ctrl(current_thread_id=1)
    await ctrl.fetch_stop_info()
    assert ctrl.state.current_thread_id == 1
    assert ctrl.state.stack_frames[0].name == "camlMain__entry"


async def test_no_classifier_no_behavior_change():
    # A profile without classify_threads (python default) must not incur
    # the extra per-thread stack fetches.
    from tests.unit.test_controller_actions import _make

    ctrl, fake, _ = _make(with_frames=False)
    await ctrl.fetch_stop_info()
    # one stackTrace call: the current thread's — no classification sweep
    assert len(fake.calls_to("stackTrace")) == 1

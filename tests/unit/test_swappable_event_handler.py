"""Run mode swaps the live controller's event sink between a console
printer and the TUI without recreating the controller; every protocol
method must delegate to the *current* target."""

from tdb.session.event_bus import DebugEventHandler, SwappableEventHandler


class _Recorder:
    def __init__(self):
        self.calls = []

    def on_initialized(self):
        self.calls.append(("initialized",))

    def on_stopped(self, thread_id, reason, description=None, text=None):
        self.calls.append(("stopped", thread_id, reason, description, text))

    def on_continued(self):
        self.calls.append(("continued",))

    def on_terminated(self):
        self.calls.append(("terminated",))

    def on_exited(self, exit_code):
        self.calls.append(("exited", exit_code))

    def on_output(self, text, category):
        self.calls.append(("output", text, category))

    def on_external_terminal_started(self):
        self.calls.append(("terminal",))


def test_delegates_every_method_and_retargets():
    a, b = _Recorder(), _Recorder()
    h = SwappableEventHandler(a)
    assert isinstance(h, DebugEventHandler)  # runtime_checkable protocol
    assert h.target is a

    h.on_initialized()
    h.on_stopped(1, "pause", "d", "t")
    h.on_continued()
    h.on_output("x", "stdout")
    assert [c[0] for c in a.calls] == ["initialized", "stopped", "continued", "output"]
    assert a.calls[1] == ("stopped", 1, "pause", "d", "t")

    old = h.retarget(b)
    assert old is a
    assert h.target is b
    h.on_terminated()
    h.on_exited(3)
    h.on_external_terminal_started()
    assert b.calls == [("terminated",), ("exited", 3), ("terminal",)]
    assert len(a.calls) == 4  # nothing leaked to the old target

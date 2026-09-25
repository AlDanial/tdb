"""A compiled executable must never be rendered byte-for-byte in the
Code View (issue #54).

`tdb ./pi_digits_c` used to load the ELF bytes into the pane (garbage
to the user, and tens of seconds of syntax highlighting for a large
binary) until the first stop swapped in the DWARF-resolved source. And
when the adapter could not resolve any source — no debug info, or a
gdb too old to speak DAP properly — the garbage stayed on screen with
no explanation.
"""

from __future__ import annotations

import time

import pytest

from tdb.app import TdbApp
from tdb.dap.types import Source, StackFrame
from tdb.languages.base import (
    AdapterSpec,
    LanguageProfile,
    Presentation,
    ProfileCapabilities,
)
from tdb.persist import TdbConfig
from tdb.session.state import SessionPhase
from tdb.widgets.code_view import CodeView, looks_binary

# A believable ELF head followed by every byte value: NULs, high bytes,
# and a few printable runs, like a real executable.
ELF_BYTES = (
    b"\x7fELF\x02\x01\x01" + b"\x00" * 9 + b"\x03\x00>\x00" + bytes(range(256)) * 8
)


class _FakeGdb(AdapterSpec):
    id = "fakegdb"

    def command(self):
        return ["fakegdb", "-i", "dap"]

    def launch_body(self, **kw):
        return {}

    def attach_body(self, **kw):
        return {}

    def pick_exception_filters(self, caps):
        return []


def _profile() -> LanguageProfile:
    return LanguageProfile(
        id="cpp",
        display_name="C/C++",
        adapter=_FakeGdb(),
        presentation=Presentation(lexer="c"),
        capabilities=ProfileCapabilities(),
    )


@pytest.fixture
def binary(tmp_path) -> str:
    p = tmp_path / "pi_digits_c"
    p.write_bytes(ELF_BYTES)
    return str(p)


@pytest.fixture
def c_source(tmp_path) -> str:
    p = tmp_path / "pi_digits.c"
    p.write_text("int main(void) {\n    return 0;\n}\n")
    return str(p)


@pytest.fixture
def no_session(monkeypatch):
    """Keep the App from spawning an adapter: these tests drive state by hand."""
    monkeypatch.setattr(TdbApp, "_start_session", lambda self: None)


def _pane_text(code_view: CodeView) -> str:
    return "\n".join(code_view.lines())


# ---- looks_binary / CodeView.load_file, no App ------------------------------


def test_looks_binary(binary, c_source, tmp_path):
    assert looks_binary(binary)
    assert not looks_binary(c_source)
    assert not looks_binary(str(tmp_path / "missing"))


def test_load_file_refuses_binary_content(binary):
    cv = CodeView()
    cv.load_file(binary)
    text = _pane_text(cv)
    assert "\x7f" not in text and "\x00" not in text
    assert cv.is_placeholder
    assert cv.source_path == binary
    assert "pi_digits_c" in text


def test_load_file_reads_only_the_head_of_a_binary(tmp_path):
    big = tmp_path / "big"
    big.write_bytes(ELF_BYTES * (32 * 1024 * 1024 // len(ELF_BYTES)))
    cv = CodeView()
    started = time.monotonic()
    cv.load_file(str(big))
    assert time.monotonic() - started < 2.0
    assert cv.is_placeholder


def test_placeholder_refuses_edit_and_breakpoints(binary):
    cv = CodeView()
    cv.load_file(binary)
    reason = cv.edit_refusal_reason()
    assert reason is not None and "source" in reason.lower()
    assert cv._snap_breakpoint_line(1) is None


def test_missing_file_is_still_a_placeholder(tmp_path):
    cv = CodeView()
    cv.load_file(str(tmp_path / "nope.c"))
    assert cv.is_placeholder
    assert "Could not read" in _pane_text(cv)


# ---- App paths --------------------------------------------------------------


async def test_startup_shows_waiting_placeholder_not_bytes(binary, no_session):
    app = TdbApp(program=binary, config=TdbConfig(), profile=_profile())
    async with app.run_test() as pilot:
        await pilot.pause()
        cv = app.query_one("#code-view", CodeView)
        text = _pane_text(cv)
        assert "\x7f" not in text
        assert cv.is_placeholder
        assert "pi_digits_c" in text
        assert "fakegdb" in text


async def test_stop_without_source_explains_why(binary, no_session):
    app = TdbApp(program=binary, config=TdbConfig(), profile=_profile())
    async with app.run_test() as pilot:
        await pilot.pause()
        state = app.controller.state
        state.enter_stop(1, "breakpoint")
        state.set_stack([StackFrame(id=1, name="main", source=None, line=0)])
        app._update_ui_state()
        await pilot.pause()
        cv = app.query_one("#code-view", CodeView)
        text = _pane_text(cv)
        assert cv.is_placeholder
        assert "\x7f" not in text
        assert "main" in text
        assert "fakegdb" in text
        assert "debug info" in text or "-g" in text
        assert "too old" in text


async def test_stop_with_no_frames_keeps_waiting_note(binary, no_session):
    """An adapter that stops but cannot even produce a stack (dlv's raw
    process-entry stop) has not *reported* anything: the waiting note
    stays rather than a misleading "no debug info" diagnosis."""
    app = TdbApp(program=binary, config=TdbConfig(), profile=_profile())
    async with app.run_test() as pilot:
        await pilot.pause()
        state = app.controller.state
        state.enter_stop(1, "entry")
        state.set_stack([])
        app._update_ui_state()
        await pilot.pause()
        cv = app.query_one("#code-view", CodeView)
        text = _pane_text(cv)
        assert cv.is_placeholder
        assert "Waiting for fakegdb" in text
        assert "too old" not in text


async def test_stop_with_source_replaces_placeholder(binary, c_source, no_session):
    app = TdbApp(program=binary, config=TdbConfig(), profile=_profile())
    async with app.run_test() as pilot:
        await pilot.pause()
        state = app.controller.state
        state.enter_stop(1, "breakpoint")
        state.set_stack(
            [StackFrame(id=1, name="main", source=Source(path=c_source), line=2)]
        )
        app._update_ui_state()
        await pilot.pause()
        cv = app.query_one("#code-view", CodeView)
        assert not cv.is_placeholder
        assert cv.source_path == c_source
        assert cv.lines() == ["int main(void) {", "    return 0;", "}"]
        assert cv.current_line == 2


async def test_stop_without_source_keeps_real_source_on_screen(
    binary, c_source, no_session
):
    """A later stop in a frame with no source (e.g. libc) must not
    replace a real file with the explanation — only placeholders are
    replaced."""
    app = TdbApp(program=binary, config=TdbConfig(), profile=_profile())
    async with app.run_test() as pilot:
        await pilot.pause()
        state = app.controller.state
        state.enter_stop(1, "breakpoint")
        state.set_stack(
            [StackFrame(id=1, name="main", source=Source(path=c_source), line=2)]
        )
        app._update_ui_state()
        state.enter_stop(1, "step")
        state.set_stack([StackFrame(id=2, name="__libc_start_call_main", line=0)])
        app._update_ui_state()
        await pilot.pause()
        cv = app.query_one("#code-view", CodeView)
        assert not cv.is_placeholder
        assert cv.source_path == c_source


async def test_exit_without_any_source_explains_why(binary, no_session):
    app = TdbApp(program=binary, config=TdbConfig(), profile=_profile())
    async with app.run_test() as pilot:
        await pilot.pause()
        state = app.controller.state
        state.transition_to(SessionPhase.TERMINATED)
        state.set_stack([])
        app._update_ui_state()
        await pilot.pause()
        cv = app.query_one("#code-view", CodeView)
        text = _pane_text(cv)
        assert cv.is_placeholder
        assert "\x7f" not in text
        assert "fakegdb" in text
        assert "If a stop was expected" in text
        assert "too old" in text


async def test_launch_failure_replaces_waiting_note(binary, no_session, monkeypatch):
    """A rejected configurationDone (gdb: "launch or attach not
    specified") used to leave "Waiting for gdb..." on screen with only
    the sub-title changed — it read as a hang."""
    app = TdbApp(program=binary, config=TdbConfig(), profile=_profile())
    async with app.run_test() as pilot:
        await pilot.pause()

        async def boom():
            raise RuntimeError("launch or attach not specified")

        monkeypatch.setattr(app.controller, "do_configure", boom)
        await app._dap.do_configure()
        await pilot.pause()
        cv = app.query_one("#code-view", CodeView)
        text = _pane_text(cv)
        assert cv.is_placeholder
        assert "could not start" in text
        assert "launch or attach not specified" in text
        assert app.sub_title == "Launch failed"


async def test_restart_reloads_placeholder_not_bytes(binary, no_session):
    app = TdbApp(program=binary, config=TdbConfig(), profile=_profile())
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._restart_session(start_immediately=False).wait()
        await pilot.pause()
        cv = app.query_one("#code-view", CodeView)
        text = _pane_text(cv)
        assert "\x7f" not in text
        assert cv.is_placeholder
        assert "fakegdb" in text

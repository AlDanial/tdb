"""End-to-end: lldb-dap debugging real compiled OCaml (native) binaries.

Mirrors the harness idioms of test_cpp_session.py (controller +
ServerEventHandler, breakpoints seeded into controller.state before
start()) and test_cpp_pause.py (spin fixture + pause). Skipped wholesale
when ocamlopt or lldb-dap is missing.

Timing facts baked into this module (see tests/integration/ocaml_probe.py
and docs/superpowers/specs/2026-08-22-ocaml-support-design.md, probe Q3):
lldb-dap's `threads()` result is incomplete for ~1-2s after a stop, and
the FIRST breakpoint hit in a domain worker can occur before the other
domains have spawned. Both are handled with bounded polling/retry loops
rather than a fixed sleep or a single-shot assertion.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from tdb.dap.types import SourceBreakpoint, StackFrame, Thread
from tdb.languages.errors import parse_ocaml_error
from tdb.languages.ocaml import (
    build_ocaml_profile,
    classify_ocaml_threads,
    demangle_frame_name,
)
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController

FIXTURES = Path(__file__).parent / "fixtures"

_HAVE_OCAMLOPT = shutil.which("ocamlopt") is not None
_HAVE_LLDB_DAP = shutil.which("lldb-dap") is not None

pytestmark = pytest.mark.skipif(
    not (_HAVE_OCAMLOPT and _HAVE_LLDB_DAP),
    reason="ocamlopt and lldb-dap are required",
)

WAIT = 45.0  # generous ceiling for adapter spawn + debuggee start; bumped
# from 30.0 after an observed one-off timeout in test_step_and_continue_
# at_domain_breakpoint's final continue-to-exit when run as part of the
# full tests/integration suite (never reproduced standalone or repeated
# in isolation) -- consistent with CPU contention from many concurrently
# spawned adapter subprocesses across the suite, not a logic bug.
PAUSE_TIMEOUT = 10.0

BP_LINE = 5  # `Atomic.incr counter;` in ocaml_domains.ml (probe-verified line)

SPIN_SRC = """\
let () =
  let i = ref 0 in
  while true do
    incr i;
    Domain.cpu_relax ()
  done
"""


# --- module fixtures: compile fixtures into tmp_path_factory dirs, never
# --- into the repo -----------------------------------------------------


@pytest.fixture(scope="module")
def ocaml_domains_binary(tmp_path_factory):
    d = tmp_path_factory.mktemp("ocaml_domains")
    src = d / "ocaml_domains.ml"
    src.write_text((FIXTURES / "ocaml_domains.ml").read_text())
    exe = d / "ocaml_domains.exe"
    subprocess.run(["ocamlopt", "-g", "-o", str(exe), str(src)], cwd=d, check=True)
    return str(exe), str(src)


@pytest.fixture(scope="module")
def ocaml_fatal_binary(tmp_path_factory):
    d = tmp_path_factory.mktemp("ocaml_fatal_native")
    src = d / "ocaml_fatal.ml"
    src.write_text((FIXTURES / "ocaml_fatal.ml").read_text())
    exe = d / "ocaml_fatal.exe"
    subprocess.run(["ocamlopt", "-g", "-o", str(exe), str(src)], cwd=d, check=True)
    return str(exe)


@pytest.fixture(scope="module")
def ocaml_spin_binary(tmp_path_factory):
    d = tmp_path_factory.mktemp("ocaml_spin")
    src = d / "spin.ml"
    src.write_text(SPIN_SRC)
    exe = d / "spin.exe"
    subprocess.run(["ocamlopt", "-g", "-o", str(exe), str(src)], cwd=d, check=True)
    return str(exe)


# --- live-session fixtures/helpers, adapted from test_cpp_session.py ---


@pytest.fixture
async def session():
    """(controller, handler) pair with guaranteed teardown."""
    handler = ServerEventHandler()
    ctrl = DebugController(handler, profile=build_ocaml_profile(adapter="lldb-dap"))
    yield ctrl, handler
    try:
        await asyncio.wait_for(ctrl.stop(), timeout=WAIT)
    except Exception:
        pass  # already stopped / adapter already gone


async def _launch(
    ctrl: DebugController,
    handler: ServerEventHandler,
    program: str,
    *,
    breakpoints: list[tuple[str, int]] | None = None,
) -> None:
    """Launch, wait for `initialized`, configure, and wait for the first
    stop (the seeded breakpoint) — unlike the cpp harness's stop_on_entry
    toggle, every native test here launches running (stop_on_entry=False)
    and expects the breakpoint itself to produce the first stop."""
    if breakpoints:
        for source, line in breakpoints:
            ctrl.state.breakpoints.setdefault(source, []).append(
                SourceBreakpoint(line=line)
            )
    await ctrl.start(program=program, stop_on_entry=False)
    await asyncio.wait_for(handler.initialized_event.wait(), WAIT)
    await ctrl.do_configure()
    assert await handler.wait_for_stop(timeout=WAIT)
    await ctrl.fetch_stop_info()


async def _resume_and_wait(ctrl, handler, action_name: str) -> None:
    handler.reset_for_continue()
    await getattr(ctrl, action_name)()
    assert await handler.wait_for_stop(timeout=WAIT)
    if not ctrl.state.is_terminated:
        await ctrl.fetch_stop_info()


async def _continue_to_exit(ctrl, handler) -> None:
    handler.reset_for_continue()
    await ctrl.continue_()
    await asyncio.wait_for(handler.terminated_event.wait(), WAIT)


async def _stable_threads(ctrl: DebugController) -> list[Thread]:
    """Poll until lldb-dap's thread list stops growing (probe Q3: the
    list is incomplete for ~1-2s right after a stop)."""
    threads = await ctrl.client.threads()
    for _ in range(10):
        await asyncio.sleep(0.5)
        threads2 = await ctrl.client.threads()
        if len(threads2) == len(threads):
            return threads2
        threads = threads2
    return threads


async def _fetch_stacks(
    ctrl: DebugController, threads: list[Thread]
) -> dict[int, list[StackFrame]]:
    stacks: dict[int, list[StackFrame]] = {}
    for t in threads:
        try:
            stacks[t.id] = await ctrl.client.stack_trace(t.id, levels=30)
        except Exception:
            stacks[t.id] = []
    return stacks


# --- live-session tests --------------------------------------------------


async def test_domains_visible_at_breakpoint(session, ocaml_domains_binary):
    """THE multicore headline: domains show up as classified threads.

    Adapted per probe fact #2: the first breakpoint hit can land before
    other domains have spawned, so instead of asserting >=4 threads at
    the first stop, this bounded-continue loop re-stops and re-classifies
    up to 10 times until >=2 visible "Domain N" decorations and >=1
    hidden decoration are observed, then asserts on that achieved state.
    """
    binary, src = ocaml_domains_binary
    ctrl, handler = session
    await _launch(ctrl, handler, binary, breakpoints=[(src, BP_LINE)])

    threads: list[Thread] = []
    stacks: dict[int, list[StackFrame]] = {}
    decorations = []
    achieved = False
    for _attempt in range(10):
        threads = await _stable_threads(ctrl)
        stacks = await _fetch_stacks(ctrl, threads)
        decorations = classify_ocaml_threads(threads, stacks)
        visible_domains = [
            d
            for d in decorations
            if not d.hidden and d.label and d.label.startswith("Domain")
        ]
        hidden = [d for d in decorations if d.hidden]
        if len(visible_domains) >= 2 and hidden:
            achieved = True
            break
        if ctrl.state.is_terminated:
            break
        handler.reset_for_continue()
        await ctrl.continue_()
        if not await handler.wait_for_stop(timeout=WAIT):
            break
        await ctrl.fetch_stop_info()

    assert achieved, (
        "never observed >=2 visible 'Domain N' decorations plus >=1 "
        f"hidden backup thread within 10 continues; last decorations="
        f"{[(d.label, d.hidden) for d in decorations]}, "
        f"thread_count={len(threads)}"
    )
    # By construction of `achieved`, we have main (Domain 0) + >=1 other
    # visible domain + >=1 hidden backup thread: >= 3 total. This is the
    # minimum `achieved` actually guarantees; the real spec requirement
    # (>=2 visible "Domain N" decorations + >=1 hidden) is already
    # enforced by the `achieved` assertion above.
    assert len(threads) >= 3

    # The stopped thread's top demangled frame is the worker function.
    assert ctrl.state.stack_frames, "expected a populated stack at the stop"
    top = ctrl.state.stack_frames[0]
    demangled = demangle_frame_name(top.name)
    assert demangled.endswith(".worker"), (
        f"top frame {top.name!r} demangled to {demangled!r}, expected *.worker"
    )

    # A second visible (non-main) domain: prefer confirming its stack also
    # contains a `.worker` frame; if scheduling makes that flaky, relax to
    # "every visible non-main domain has a non-empty stack" (per brief).
    non_main_visible = [
        d
        for d in decorations
        if not d.hidden and d.label and d.label != "Domain 0 (main)"
    ]
    assert non_main_visible, (
        "expected at least one non-main visible domain decoration; "
        f"decorations={[(d.label, d.hidden) for d in decorations]}"
    )
    has_worker_frame = any(
        any(
            demangle_frame_name(f.name).endswith(".worker")
            for f in stacks.get(d.thread.id, [])
        )
        for d in non_main_visible
    )
    if not has_worker_frame:
        for d in non_main_visible:
            assert stacks.get(d.thread.id), (
                f"domain {d.label!r} (thread {d.thread.id}) has an "
                "unexpectedly empty stack"
            )


async def test_step_and_continue_at_domain_breakpoint(session, ocaml_domains_binary):
    binary, src = ocaml_domains_binary
    ctrl, handler = session
    await _launch(ctrl, handler, binary, breakpoints=[(src, BP_LINE)])

    await _resume_and_wait(ctrl, handler, "step_over")
    assert ctrl.state.stack_frames
    frame1 = ctrl.state.stack_frames[0]
    if frame1.source is not None:
        assert frame1.source.path.endswith("ocaml_domains.ml")

    await _resume_and_wait(ctrl, handler, "step_over")
    assert ctrl.state.stack_frames
    frame2 = ctrl.state.stack_frames[0]
    if frame2.source is not None:
        assert frame2.source.path.endswith("ocaml_domains.ml")

    await ctrl.remove_breakpoint(src, BP_LINE)
    await _continue_to_exit(ctrl, handler)
    assert "sum=6" in handler.drain_output()


async def test_pause_while_running(ocaml_spin_binary):
    """Mirrors test_cpp_pause.py's structure: launch with no breakpoints,
    stop_on_entry=False, then pause() with no prior stop event."""
    handler = ServerEventHandler()
    ctrl = DebugController(handler, profile=build_ocaml_profile(adapter="lldb-dap"))
    await ctrl.start(program=ocaml_spin_binary, stop_on_entry=False)
    await asyncio.wait_for(handler.initialized_event.wait(), WAIT)
    await ctrl.do_configure()
    try:
        ok = await ctrl.pause(timeout=PAUSE_TIMEOUT)
        assert ok is True
        await ctrl.fetch_stop_info()
        assert ctrl.state.stack_frames
    finally:
        try:
            await asyncio.wait_for(ctrl.stop(), timeout=WAIT)
        except Exception:
            pass


async def test_uncaught_exception_stops_or_parses(session, ocaml_fatal_binary):
    """Expect a stopped event at the preRunCommands
    caml_fatal_uncaught_exception breakpoint, then continue to
    termination; either way, the parsed stderr must name the exception.
    """
    ctrl, handler = session
    await ctrl.start(program=ocaml_fatal_binary, stop_on_entry=False)
    await asyncio.wait_for(handler.initialized_event.wait(), WAIT)
    await ctrl.do_configure()

    stopped = await handler.wait_for_stop(timeout=WAIT)
    pre_abort_stop = stopped and not handler.terminated_event.is_set()
    if pre_abort_stop:
        await ctrl.fetch_stop_info()
        found = False
        for t in ctrl.state.threads:
            try:
                frames = await ctrl.client.stack_trace(t.id, levels=30)
            except Exception:
                continue
            if any("caml_fatal_uncaught_exception" in f.name for f in frames):
                found = True
                break
        assert found, (
            "expected some thread's stack to contain a "
            "caml_fatal_uncaught_exception frame at the pre-abort stop"
        )
        handler.reset_for_continue()
        await ctrl.continue_()

    await asyncio.wait_for(handler.terminated_event.wait(), WAIT)
    captured = handler.drain_output()
    parsed = parse_ocaml_error(captured, handler.exit_code)
    assert parsed is not None, f"expected a parsed OCaml error; captured={captured!r}"
    assert 'Failure("boom")' in parsed.message


async def test_variables_report_what_dwarf_offers(session, ocaml_domains_binary):
    """Locals in native OCaml frames are documented (probe Q2) to be
    empty; assert the scopes/variables round trip SUCCEEDS and print
    what it returns, without asserting on specific values."""
    binary, src = ocaml_domains_binary
    ctrl, handler = session
    await _launch(ctrl, handler, binary, breakpoints=[(src, BP_LINE)])

    frame_id = ctrl.state.stack_frames[0].id
    scopes = await ctrl.client.scopes(frame_id)
    assert scopes, "expected at least one scope (Locals/Globals/Registers)"
    for s in scopes:
        variables = await ctrl.client.variables(s.variables_reference)
        print(
            f"[ocaml native locals] scope={s.name}: "
            f"{[(v.name, v.value, v.type) for v in variables]}"
        )

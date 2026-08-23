"""End-to-end: ocamlearlybird debugging a real compiled OCaml bytecode
program. Mirrors the harness idioms of test_cpp_session.py.

ocamlearlybird is installed via opam and is often only on
~/.opam/default/bin, not on the ambient PATH pytest inherits — the skip
guard AND the profile builder both need to find it, so this module
checks `shutil.which` first and falls back to the literal opam path,
then threads that resolved path through
`build_ocaml_profile(adapter="ocamlearlybird", adapter_paths=...)`.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tdb.dap.types import SourceBreakpoint
from tdb.languages.errors import parse_ocaml_error
from tdb.languages.ocaml import build_ocaml_profile
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController

FIXTURES = Path(__file__).parent / "fixtures"


def _find_ocamlearlybird() -> str | None:
    found = shutil.which("ocamlearlybird")
    if found:
        return found
    fallback = Path.home() / ".opam" / "default" / "bin" / "ocamlearlybird"
    if fallback.exists():
        return str(fallback)
    return None


_HAVE_OCAMLC = shutil.which("ocamlc") is not None
_EARLYBIRD_PATH = _find_ocamlearlybird()
_HAVE_EARLYBIRD = _EARLYBIRD_PATH is not None

pytestmark = pytest.mark.skipif(
    not (_HAVE_OCAMLC and _HAVE_EARLYBIRD),
    reason="ocamlc and ocamlearlybird are required",
)

WAIT = 30.0

ADD_SRC = """\
let add x y =
  let total = x + y in
  let msg = Printf.sprintf "total=%d" total in
  print_endline msg;
  total
let () = ignore (add 2 3)
"""
BP_LINE = 4  # `print_endline msg;`


# --- module fixtures: compile fixtures into tmp_path_factory dirs, never
# --- into the repo -----------------------------------------------------


@pytest.fixture(scope="module")
def add_binary(tmp_path_factory):
    d = tmp_path_factory.mktemp("ocaml_add")
    src = d / "add.ml"
    src.write_text(ADD_SRC)
    byte = d / "add.byte"
    subprocess.run(["ocamlc", "-g", "-o", str(byte), str(src)], cwd=d, check=True)
    return str(byte), str(src)


@pytest.fixture(scope="module")
def ocaml_fatal_binary(tmp_path_factory):
    d = tmp_path_factory.mktemp("ocaml_fatal_byte")
    src = d / "ocaml_fatal.ml"
    src.write_text((FIXTURES / "ocaml_fatal.ml").read_text())
    byte = d / "ocaml_fatal.byte"
    subprocess.run(["ocamlc", "-g", "-o", str(byte), str(src)], cwd=d, check=True)
    return str(byte)


# --- live-session fixtures/helpers, adapted from test_cpp_session.py ---


def _build_earlybird_profile():
    return build_ocaml_profile(
        adapter="ocamlearlybird",
        adapter_paths={"ocamlearlybird": _EARLYBIRD_PATH},
    )


@pytest.fixture
async def session():
    handler = ServerEventHandler()
    ctrl = DebugController(handler, profile=_build_earlybird_profile())
    yield ctrl, handler
    try:
        await asyncio.wait_for(ctrl.stop(), timeout=WAIT)
    except Exception:
        pass


async def _launch(
    ctrl: DebugController,
    handler: ServerEventHandler,
    program: str,
    *,
    breakpoints: list[tuple[str, int]] | None = None,
) -> None:
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


# --- live-session tests --------------------------------------------------


async def test_launch_breakpoint_locals(session, add_binary):
    """earlybird's rich locals actually work — assert them for real,
    unlike the native lldb-dap test."""
    byte, src = add_binary
    ctrl, handler = session
    await _launch(ctrl, handler, byte, breakpoints=[(src, BP_LINE)])

    assert ctrl.state.stack_frames
    frame_id = ctrl.state.stack_frames[0].id
    scopes = await ctrl.client.scopes(frame_id)
    variables: dict[str, str] = {}
    for s in scopes:
        for v in await ctrl.client.variables(s.variables_reference):
            variables[v.name] = v.value

    assert variables.get("total") == "5", variables
    assert "total=5" in variables.get("msg", ""), variables


async def test_evaluate_in_scope(session, add_binary):
    """DAP `evaluate` against ocamlearlybird 1.3.6, as spawned/framed by
    tdb's production DAPClient (compact JSON, correct request ordering),
    never receives a response -- confirmed for every expression tried
    (`total`, `total + 1`, `1+1`) and every `context` value tried (repl/
    hover/watch/clipboard), both through this exact code path AND via a
    standalone raw-DAP reproduction (compact-JSON framing, correct
    initialize/launch/setBreakpoints/configurationDone sequencing) that
    isolates it from every other tdb code path. scopes/variables/
    continue/stepping/threads/stackTrace all round-trip correctly on the
    same kind of session (see test_launch_breakpoint_locals) -- this is a
    real ocamlearlybird-side limitation/bug in this installed version,
    not a tdb defect.

    `ctrl.evaluate()` swallows the resulting client-side 30s DAP_REQUEST
    timeout (`asyncio.TimeoutError`, whose `str()` is `""`) and returns
    `""` rather than raising. This test documents that observed
    behavior; if a future earlybird release fixes `evaluate`, `result`
    will stop being `""` and the xfail below will XPASS, which is the
    correct signal to update this test.
    """
    byte, src = add_binary
    ctrl, handler = session
    await _launch(ctrl, handler, byte, breakpoints=[(src, BP_LINE)])

    result = await ctrl.evaluate("total + 1")
    if result == "":
        pytest.xfail(
            "ocamlearlybird's evaluate request never responds in this "
            "environment (independently confirmed via a raw-DAP "
            "reproduction) -- known adapter limitation, not a tdb bug"
        )
    assert "6" in result, result


async def test_fatal_error_parses(session, ocaml_fatal_binary):
    """Real finding, not a tdb bug: when ocaml_fatal.byte is run *through*
    ocamlearlybird's own debug session, earlybird intercepts the
    uncaught exception itself and reports a generic `output` event --
    "Program exited due to Uncaught_exc" -- with no exception message or
    backtrace, regardless of OCAMLRUNPARAM=b (confirmed via a standalone
    raw-DAP reproduction: passing `env: {"OCAMLRUNPARAM": "b"}` in the
    launch body doesn't change this). The real exception never reaches
    the debuggee's own stdout/stderr under earlybird's instrumented
    runtime, so tdb's OCaml parse-on-exit error modal has nothing useful
    to parse for earlybird/bytecode sessions specifically -- an external
    limitation of ocamlearlybird 1.3.6, not something tdb's adapter can
    work around.

    This test verifies both halves honestly: (1) parse_ocaml_error
    correctly declines to parse earlybird's generic message (no false
    positive), and (2) the SAME compiled binary, run directly (bypassing
    earlybird, exactly how a plain `OCAMLRUNPARAM=b ocaml_fatal.byte`
    looks outside a debugger), produces real Printexc text that
    parse_ocaml_error DOES parse correctly end-to-end -- proving the
    parser against genuinely-produced output even though the live
    earlybird session can't deliver that text to tdb.
    """
    ctrl, handler = session
    await ctrl.start(program=ocaml_fatal_binary, stop_on_entry=False)
    await asyncio.wait_for(handler.initialized_event.wait(), WAIT)
    await ctrl.do_configure()
    await asyncio.wait_for(handler.terminated_event.wait(), WAIT)

    captured_via_earlybird = handler.drain_output()
    assert "Uncaught_exc" in captured_via_earlybird, (
        "expected earlybird's own generic termination message; "
        f"captured={captured_via_earlybird!r}"
    )
    assert parse_ocaml_error(captured_via_earlybird, handler.exit_code) is None, (
        "parse_ocaml_error should NOT false-positive on earlybird's "
        f"generic termination message: {captured_via_earlybird!r}"
    )

    # The parser itself, proven against real (not hand-authored) output:
    # run the identical compiled binary directly, the way a user's
    # OCAMLRUNPARAM=b invocation actually looks outside the debugger.
    proc = subprocess.run(
        [ocaml_fatal_binary],
        capture_output=True,
        text=True,
        env={**os.environ, "OCAMLRUNPARAM": "b"},
    )
    parsed = parse_ocaml_error(proc.stderr, proc.returncode)
    assert parsed is not None, f"expected a parsed OCaml error; stderr={proc.stderr!r}"
    assert 'Failure("boom")' in parsed.message
    assert parsed.frames, f"expected at least one parsed frame; stderr={proc.stderr!r}"
    funcs = [f.func for f in parsed.frames]
    assert "Ocaml_fatal" in funcs, funcs
    assert any("failwith" in f for f in funcs), funcs

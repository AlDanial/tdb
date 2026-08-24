# Perl Error Modal + BEGIN-Block Debugging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Perl fatal errors surface in the same error modal Python errors do (message + call stack + Code View navigation), and Perl `BEGIN` blocks become steppable by stopping during the compile phase.

**Architecture:** Two independent halves. (A) A new language-agnostic error-parsing seam on `Presentation` — `parse_error(stderr) -> ParsedError | None` — with the existing Python traceback parser moved behind it and a new Perl `die` parser added; `_check_stderr_traceback` and `_TracebackModal` become profile-driven. (B) A `Devel::TdbCompile` preload module, injected by the Perl session driver, that wraps `DB::DB` with a phase-aware file-filtered shim and arms `$DB::single` before the program is compiled, so the first stop lands on the first compile-time statement of the user's file; breakpoint application is deferred until the RUN phase because the line table is incomplete during compilation.

**Tech Stack:** Python 3, perl5db (perl >= 5.18), textual, pytest.

## Background — verified facts (do not re-derive)

These were established empirically against perl 5.40.1 before this plan was written. Trust them.

1. Perl `BEGIN` blocks run at compile time, **before** perl5db's first prompt, so tdb never sees them. This is documented perl5db behavior (`perldebug`, "Debugging Compile-Time Statements").
2. Arming `$DB::single = 1` from a `-M`-preloaded module **does** make compile-time statements trap into `DB::DB`.
3. Naively armed, `s` drags the user through `strict.pm` / `warnings.pm` internals. Wrapping `DB::DB` with a filter that (a) passes through once `${^GLOBAL_PHASE} ne 'START'` and (b) during START only traps when `(caller)[1] eq $target_file` produces exactly the wanted behavior: on `work/has_begin.pl` the stops are line 2 (`use strict`), line 3 (`use warnings`), then **line 7 (`my $a = 10;`) and line 8 (`my %b;`) inside the BEGIN block**.
4. `helpers.pl` loads fine at a compile-time stop and `Devel::TdbHelper::location()` returns correct data there, reporting `sub: main::BEGIN`. So stack / scopes / variables / evaluate all work during the compile phase.
5. **END blocks already work today** — no change needed. Verified through tdb's own adapter: `stepIn` from `clean2.pl:12` gives `:15 → :16 → :17` with `sub=main::END`. Only step-*in* enters them; `next`/`continue` skip to termination. This plan must not regress that.
6. A Perl `die` (runtime **or** compile-time) does not produce any distinct perl5db stop — perl5db goes straight to its "Debugged program terminated" state. The error text reaches tdb only via the debuggee's **stderr pipe**, which the adapter already forwards with category `stderr`, so it is already in `TdbApp._stderr_buffer` when `on_terminated` runs.
7. Real Perl error text to parse (from `work/has_begin.pl`):
   ```
   Illegal division by zero at /home/al/projects/tdbg/work/has_begin.pl line 10.
    at /home/al/projects/tdbg/work/has_begin.pl line 10.
   	main::BEGIN() called at /home/al/projects/tdbg/work/has_begin.pl line 11
   	eval {...} called at /home/al/projects/tdbg/work/has_begin.pl line 11
   BEGIN failed--compilation aborted at /home/al/projects/tdbg/work/has_begin.pl line 11.
   ```
   A plain runtime die is just the first line, e.g. `Illegal division by zero at /tmp/x.pl line 4.`
8. During the compile phase the user's file is only **partially compiled**: `b <file>:<line>` on a later line answers `not breakable`. Hence Task 5's deferral.

## Global Constraints

- Run tests with `uv run pytest <paths> -q --no-cov` from `/home/al/projects/tdbg/work` (never bare `pytest`).
- NEVER `git add -A`, `git add .`, or `git commit -a` — stage explicit paths only.
- End every commit message with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Shell is zsh: avoid bare `*` globs and `==` tokens in test commands.
- A formatter hook may rewrite files after Edit — if an anchor is missing, Read the file again.
- Cross-platform: no POSIX-only path assumptions.
- Perl code must stay compatible with perl >= 5.18 and pass `perl -c`.
- Never change `helpers.pl`'s existing marker protocol (`TDB>>>{json}<<<TDB`) or bump `$PROTOCOL` without updating both ends.

## File Structure

- `src/tdb/languages/base.py` — MODIFY: add `ErrorFrame`, `ParsedError`, and `Presentation.parse_error`.
- `src/tdb/languages/errors.py` — NEW: `parse_python_error`, `parse_perl_error`.
- `src/tdb/languages/python.py`, `src/tdb/languages/perl.py` — MODIFY: wire the parsers into `Presentation`.
- `src/tdb/app_handlers/dap_events.py` — MODIFY: `_check_stderr_traceback` becomes profile-driven.
- `src/tdb/widgets/modals.py` — MODIFY: `_TracebackModal` takes its header instead of hardcoding Python's.
- `src/tdb/adapters/perl/Devel/TdbCompile.pm` — NEW: the compile-phase shim.
- `src/tdb/adapters/perl/session.py` — MODIFY: inject the preload; expose phase.
- `src/tdb/adapters/perl/helpers.pl` — MODIFY: add `phase()` helper.
- `src/tdb/adapters/perl/server.py` — MODIFY: defer breakpoints during compile phase; real exit code.
- `README.md` — MODIFY: document both behaviors and their limitations.

---

### Task 1: Error-parsing seam + Python parser moved behind it

**Files:**
- Modify: `src/tdb/languages/base.py` (near `Presentation`, ~line 110)
- Create: `src/tdb/languages/errors.py`
- Modify: `src/tdb/languages/python.py` (its `Presentation(...)` construction)
- Test: `tests/unit/test_error_parsers.py`

**Interfaces produced** (Tasks 2-4 depend on these exact names):

```python
@dataclass(frozen=True)
class ErrorFrame:
    path: str
    line: int
    func: str  # "" when the language doesn't name one


@dataclass(frozen=True)
class ParsedError:
    header: str  # modal's first line, e.g. "Traceback (most recent call last):"
    message: str  # e.g. "ZeroDivisionError: division by zero"
    frames: list[ErrorFrame]  # OUTERMOST-first (source order), same as Python prints

    # on Presentation:
    parse_error: Callable[[str], "ParsedError | None"] | None = None
```

`parse_python_error(stderr: str) -> ParsedError | None` must reproduce today's behavior exactly: bail unless `"Traceback (most recent call last):"` is present; split on chained-traceback headers and use the LAST block; extract frames with the existing `_TB_FILE_RE` (`r'^\s*File "(.+)", line (\d+)(?:, in (.+))?'`, `re.MULTILINE`); derive the exception line with the existing "first non-indented line after the frames" heuristic. Move this logic out of `dap_events.py:206-295` — do not reimplement it from memory; copy it.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_error_parsers.py
"""Language-specific fatal-error parsers behind Presentation.parse_error."""

from tdb.languages.errors import parse_python_error

SIMPLE = """Traceback (most recent call last):
  File "/app/main.py", line 12, in <module>
    boom()
  File "/app/lib.py", line 5, in boom
    return 1 / 0
ZeroDivisionError: division by zero
"""

CHAINED = """Traceback (most recent call last):
  File "/app/a.py", line 2, in <module>
    inner()
ValueError: first

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "/app/b.py", line 9, in <module>
    outer()
RuntimeError: second
"""


def test_returns_none_without_traceback():
    assert parse_python_error("just some stderr noise\n") is None


def test_parses_message_and_frames_in_source_order():
    p = parse_python_error(SIMPLE)
    assert p is not None
    assert p.header == "Traceback (most recent call last):"
    assert p.message == "ZeroDivisionError: division by zero"
    assert [(f.path, f.line, f.func) for f in p.frames] == [
        ("/app/main.py", 12, "<module>"),
        ("/app/lib.py", 5, "boom"),
    ]


def test_chained_traceback_uses_last_block():
    p = parse_python_error(CHAINED)
    assert p is not None
    assert p.message == "RuntimeError: second"
    assert [f.path for f in p.frames] == ["/app/b.py"]


def test_presentation_exposes_parser_for_python():
    from tdb.languages import registry

    profile = registry.resolve("python")
    assert profile.presentation.parse_error is not None
    assert profile.presentation.parse_error(SIMPLE) is not None


def test_presentation_parse_error_defaults_to_none():
    from tdb.languages.base import Presentation

    assert Presentation().parse_error is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_error_parsers.py -q --no-cov`
Expected: FAIL — `ModuleNotFoundError: No module named 'tdb.languages.errors'`

- [ ] **Step 3: Implement**

Add `ErrorFrame`, `ParsedError` to `base.py` (near `Presentation`) and the `parse_error` field to `Presentation`. Create `errors.py` with `parse_python_error` carrying the moved logic. Wire `parse_error=parse_python_error` into the Python profile's `Presentation(...)`. Do NOT touch `dap_events.py` yet — that is Task 3.

- [ ] **Step 4: Verify green**

Run: `uv run pytest tests/unit/test_error_parsers.py tests/unit -q --no-cov`
Expected: new tests pass; zero regressions.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/languages/base.py src/tdb/languages/errors.py src/tdb/languages/python.py tests/unit/test_error_parsers.py
git commit -m "feat: language-agnostic error-parsing seam on Presentation

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Perl `die` parser

**Files:**
- Modify: `src/tdb/languages/errors.py`, `src/tdb/languages/perl.py`
- Test: `tests/unit/test_error_parsers.py` (append)

**Interfaces consumed:** `ErrorFrame`, `ParsedError` (Task 1).
**Produces:** `parse_perl_error(stderr) -> ParsedError | None`, wired as the Perl profile's `parse_error`.

Rules, derived from the real output in Background §7:
- Detect a fatal error by the trailing-location form `... at <FILE> line <N>.` on the FIRST line of the error. Return `None` when no line matches — plain warnings such as `Use of uninitialized value in division (/) at x.pl line 10.` also match that shape, so only treat stderr as fatal when it also contains a `died`-shaped terminator: either the message is the last non-empty content, or a `BEGIN failed--compilation aborted` / `Compilation failed in require` line is present. Prefer a conservative rule and pin it with the tests below.
- `header` is `"Perl error:"`.
- `message` is the first line with its trailing ` at FILE line N.` location stripped (e.g. `Illegal division by zero`).
- `frames` come from the innermost location plus every `\t<SUB> called at <FILE> line <N>` line, converted to OUTERMOST-first order to match Python's convention. Skip `eval {...} called at` frames — they are perl's compile-phase scaffolding, not user frames.
- For `has_begin.pl` the expected result is message `Illegal division by zero`, frames `[(".../has_begin.pl", 11, "main::BEGIN"), (".../has_begin.pl", 10, "")]` (outermost-first: the caller at line 11, then the failing statement at line 10).

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/unit/test_error_parsers.py
from tdb.languages.errors import parse_perl_error

PERL_COMPILE = """Illegal division by zero at /w/has_begin.pl line 10.
 at /w/has_begin.pl line 10.
\tmain::BEGIN() called at /w/has_begin.pl line 11
\teval {...} called at /w/has_begin.pl line 11
BEGIN failed--compilation aborted at /w/has_begin.pl line 11.
"""

PERL_RUNTIME = "Illegal division by zero at /tmp/x.pl line 4.\n"

PERL_DIE_IN_SUB = """boom at /tmp/x.pl line 3.
\tmain::inner() called at /tmp/x.pl line 7
\tmain::outer() called at /tmp/x.pl line 10
"""


def test_perl_returns_none_for_plain_output():
    assert parse_perl_error("hello world\n") is None


def test_perl_runtime_die_message_and_frame():
    p = parse_perl_error(PERL_RUNTIME)
    assert p is not None
    assert p.header == "Perl error:"
    assert p.message == "Illegal division by zero"
    assert [(f.path, f.line) for f in p.frames] == [("/tmp/x.pl", 4)]


def test_perl_compile_abort_frames_skip_eval_scaffolding():
    p = parse_perl_error(PERL_COMPILE)
    assert p is not None
    assert p.message == "Illegal division by zero"
    assert [(f.path, f.line, f.func) for f in p.frames] == [
        ("/w/has_begin.pl", 11, "main::BEGIN"),
        ("/w/has_begin.pl", 10, ""),
    ]


def test_perl_nested_call_frames_outermost_first():
    p = parse_perl_error(PERL_DIE_IN_SUB)
    assert p is not None
    assert [(f.line, f.func) for f in p.frames] == [
        (10, "main::outer"),
        (7, "main::inner"),
        (3, ""),
    ]


def test_perl_warning_alone_is_not_fatal():
    warn = "Use of uninitialized value in division (/) at /w/x.pl line 10.\n"
    assert parse_perl_error(warn) is None


def test_presentation_exposes_parser_for_perl():
    from tdb.languages import registry

    profile = registry.resolve("perl")
    assert profile.presentation.parse_error is not None
    assert profile.presentation.parse_error(PERL_RUNTIME) is not None
```

Implementer note: `test_perl_warning_alone_is_not_fatal` is the constraint that forces a conservative detector. If your rule cannot separate a lone warning from a lone runtime die (both are a single `... at FILE line N.` line), prefer treating a **lone** trailing-location line as fatal ONLY when it does not begin with a known warning prefix — and if you cannot make that reliable, report the conflict rather than deleting the test: the alternative is to have the Perl adapter mark the stderr region that follows the last resumption, which is Task 4 territory. Resolve it, document what you chose in the report, and keep every other test.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_error_parsers.py -q --no-cov`
Expected: FAIL — `ImportError: cannot import name 'parse_perl_error'`

- [ ] **Step 3: Implement** `parse_perl_error` in `errors.py`; wire it into `perl.py`'s `Presentation(...)`.

- [ ] **Step 4: Verify green**

Run: `uv run pytest tests/unit/test_error_parsers.py tests/unit -q --no-cov`

- [ ] **Step 5: Commit**

```bash
git add src/tdb/languages/errors.py src/tdb/languages/perl.py tests/unit/test_error_parsers.py
git commit -m "feat: parse perl die output into a ParsedError

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Route the modal through the profile

**Files:**
- Modify: `src/tdb/app_handlers/dap_events.py` (`_check_stderr_traceback`, ~206-295; `_show_exception_modal`, ~118-150)
- Modify: `src/tdb/widgets/modals.py` (`_TracebackModal.compose`, ~381)
- Test: `tests/unit/test_error_modal_routing.py`

**Interfaces consumed:** `ParsedError`, `Presentation.parse_error` (Tasks 1-2).

Requirements:
- `_check_stderr_traceback` must read `self.app.controller.profile.presentation.parse_error`; return immediately when it is `None` or returns `None`. All the downstream machinery — building `StackFrame`/`Source`, `state.set_stack(frames, synthetic=True)`, caching `panels.last_exception_text` / `last_frames_text` / `last_can_restart`, pushing `_TracebackModal`, and the `_update_ui_state()` navigation — stays as it is today and stays language-neutral.
- Frame list order: `ParsedError.frames` is outermost-first; today's code reverses parsed frames so the deepest is index 0 (DAP order). Preserve that inversion.
- `_TracebackModal` must render `ParsedError.header` instead of the hardcoded `"Traceback (most recent call last):"`. Give the modal a `header: str` parameter defaulting to the Python string so the third call site (`on_code_view_show_last_traceback` in `app.py`) keeps working; cache the header alongside the other `panels.last_*` values so the `e` re-summon shows the right one.
- The Python path's observable behavior must not change at all.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_error_modal_routing.py
"""The stderr error modal is driven by the active language profile."""

import pytest

from tdb.app import TdbApp
from tdb.persist import TdbConfig

PY_TB = """Traceback (most recent call last):
  File "/app/main.py", line 3, in <module>
    boom()
ZeroDivisionError: division by zero
"""

PERL_DIE = "Illegal division by zero at /w/x.pl line 4.\n"


async def _pushed_modal(app, stderr_text):
    pushed = []
    app.push_screen = lambda screen, callback=None: pushed.append(screen)
    app._stderr_buffer.clear()
    app._stderr_buffer.append(stderr_text)
    app._dap._check_stderr_traceback()
    return pushed


async def test_python_traceback_still_shows_modal():
    app = TdbApp(program="", config=TdbConfig())
    async with app.run_test() as pilot:
        await pilot.pause()
        pushed = await _pushed_modal(app, PY_TB)
        assert pushed, "python traceback should push a modal"
        assert "ZeroDivisionError" in app.panels.last_exception_text


async def test_perl_die_shows_modal_with_frames():
    from tdb.languages import registry

    app = TdbApp(program="", config=TdbConfig(), profile=registry.resolve("perl"))
    async with app.run_test() as pilot:
        await pilot.pause()
        pushed = await _pushed_modal(app, PERL_DIE)
        assert pushed, "perl die should push a modal"
        assert "Illegal division by zero" in app.panels.last_exception_text
        assert "/w/x.pl" in app.panels.last_frames_text
        # Code View / stack navigated to the failing frame
        assert app.controller.state.stack_frames
        assert app.controller.state.stack_frames[0].line == 4


async def test_non_error_stderr_shows_nothing():
    from tdb.languages import registry

    app = TdbApp(program="", config=TdbConfig(), profile=registry.resolve("perl"))
    async with app.run_test() as pilot:
        await pilot.pause()
        pushed = await _pushed_modal(app, "just a log line\n")
        assert pushed == []
```

Implementer note: check how `_check_stderr_traceback` is reached (it is called from `on_terminated`) and whether `state.set_stack(..., synthetic=True)` needs a live session; adapt the test's plumbing (not its assertions) if a mounted app with `program=""` cannot reach it directly. If `app._dap` is not the attribute name for the `DapEventCoordinator`, use the real one.

- [ ] **Step 2: Run to verify failure** — expected: the Perl test fails (no modal pushed) while the Python test passes.

- [ ] **Step 3: Implement** the profile-driven refactor + modal header parameter.

- [ ] **Step 4: Verify green**

Run: `uv run pytest tests/unit/test_error_modal_routing.py tests/unit -q --no-cov`
Expected: all pass, including every pre-existing traceback/modal test.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/app_handlers/dap_events.py src/tdb/widgets/modals.py tests/unit/test_error_modal_routing.py
git commit -m "feat: drive the error modal from the language profile

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Report the debuggee's real exit code, and gate fatality on it

**Files:**
- Modify: `src/tdb/adapters/perl/server.py` (`_classify_and_emit_stop`'s `"?"` branch ~315-322; `_forward_output`'s `__eof__` branch ~292-296)
- Modify: `src/tdb/adapters/perl/session.py` (expose the child's exit status)
- Modify: `src/tdb/languages/base.py`, `src/tdb/languages/errors.py`, `src/tdb/app_handlers/dap_events.py` (the exit-code gate below)
- Test: `tests/integration/test_perl_exit_code.py`, `tests/unit/test_error_parsers.py` (extend)

**Part 1 — real exit codes.** Today both termination routes hardcode `{"exitCode": 0}`, so a program that died is indistinguishable from a clean exit. Add a way to read the owned child's return code (launch mode only; attach has no owned child — keep `0` there) and report it.

Note the child may not have reaped yet when perl5db parks at its "terminated" prompt: perl5db stays alive at a live prompt after the program ends. Wait for the process with a short bounded timeout (~2 s) and fall back to `0` rather than blocking the event loop.

**Part 2 — replace Task 2's warning denylist with an exit-code gate.** Task 2 had to distinguish a fatal `die` from a non-fatal `warn` on a lone `... at FILE line N.` stderr line, and (with the plan's blessing) used a hardcoded denylist of five known warning prefixes. Both the implementer and the task reviewer flagged this as fragile: common warnings that are NOT on that list (`Deep recursion on subroutine`, `Subroutine x redefined`, `Name "main::x" used only once`, `Wide character in print`, and any bare user `warn "..."` call) would be misclassified as fatal, popping a spurious error modal on a clean run. Now that real exit codes exist, replace it with a deterministic signal:

- Widen the seam to `parse_error(stderr: str, exit_code: int | None) -> ParsedError | None` on `Presentation`. Update `parse_python_error` to accept and ignore `exit_code` (its sentinel is unambiguous — do NOT gate the Python path on exit code; a program can print a caught traceback and still exit 0, and changing that is out of scope).
- `parse_perl_error`: when `exit_code` is a non-`None` integer, fatality is exactly `exit_code != 0` — delete the prefix denylist from that path entirely. Keep the existing heuristic ONLY as the `exit_code is None` fallback (attach mode, or an exit code that never arrived), and say so in a comment.
- `_check_stderr_traceback` must pass the exit code through. Find where the `exited` event's code is recorded (`on_exited` in `dap_events.py`; check whether `controller.state` or the event handler retains it) and thread it; if nothing retains it today, store it when `exited` arrives. Beware ordering: `terminated` and `exited` are separate events and `_check_stderr_traceback` runs on `terminated` — verify empirically which arrives first for both debugpy and the Perl adapter, and if `exited` can arrive later, wait for it the same bounded way `_wait_for_stderr_quiescent` already waits, or pass `None` and accept the fallback. State what you found in your report.

Add unit tests pinning: a lone unlisted warning (`Deep recursion on subroutine "main::f" at /w/x.pl line 3.`) with `exit_code=0` returns `None`; the same text with `exit_code=255` parses as fatal; a real die with `exit_code=None` still parses via the fallback.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_perl_exit_code.py
"""A perl program that dies must not report exitCode 0."""

import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, "tests/integration")
from perl_adapter_harness import AdapterClient

pytestmark = pytest.mark.skipif(
    shutil.which("perl") is None
    or subprocess.run(["perl", "-e", "require v5.18"]).returncode != 0,
    reason="perl >= 5.18 required",
)


async def _run_to_exit(tmp_path, source):
    prog = tmp_path / "p.pl"
    prog.write_text(source)
    c = AdapterClient()
    await c.start()
    await c.request("initialize", {})
    c.send("launch", {"program": str(prog), "cwd": str(tmp_path)})
    await c.wait_event("initialized")
    await c.request("configurationDone", {})
    await c.wait_event("stopped")
    await c.request("continue", {"threadId": 1})
    ev = await c.wait_event("exited", timeout=30)
    await c.stop()
    return ev["body"]["exitCode"]


async def test_clean_exit_reports_zero(tmp_path):
    assert await _run_to_exit(tmp_path, 'print "ok\\n";\n') == 0


async def test_die_reports_nonzero(tmp_path):
    code = await _run_to_exit(tmp_path, "my $x = 0;\nmy $y = 1 / $x;\n")
    assert code != 0
```

- [ ] **Step 2: Run to verify failure** — expected: `test_die_reports_nonzero` fails with `0 != 0`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Verify green** — `uv run pytest tests/integration/test_perl_exit_code.py tests/integration -k perl -q --no-cov`
- [ ] **Step 5: Commit**

```bash
git add src/tdb/adapters/perl/server.py src/tdb/adapters/perl/session.py tests/integration/test_perl_exit_code.py
git commit -m "fix: report the perl debuggee's real exit code

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Compile-phase stopping (BEGIN blocks)

This is the largest task. Read Background §1-4 and §8 before starting.

**Files:**
- Create: `src/tdb/adapters/perl/Devel/TdbCompile.pm`
- Modify: `src/tdb/adapters/perl/helpers.pl` (add `phase()`)
- Modify: `src/tdb/adapters/perl/session.py` (`launch`)
- Modify: `src/tdb/adapters/perl/server.py` (defer breakpoints during compile phase)
- Modify: `pyproject.toml` if package data globs don't already ship `Devel/*.pm` (check — `Devel/TdbRemote.pm` already ships, so this probably needs nothing)
- Test: `tests/integration/test_perl_begin_blocks.py`

**Reference implementation of the shim** (verified working — use it, adapting only the self-trap fix):

```perl
package Devel::TdbCompile;
BEGIN {
    my $target = $ENV{TDB_COMPILE_FILE} or return;
    my $orig = \&DB::DB;
    no warnings 'redefine';
    *DB::DB = sub {
        if ( ${^GLOBAL_PHASE} ne 'START' ) {
            *DB::DB = $orig;
            goto &$orig;
        }
        my ( undef, $file ) = caller;
        return unless defined $file && $file eq $target;
        goto &$orig;
    };
    $DB::single = 1;
}
1;
```

**Requirements:**

1. `session.py::launch` inserts `-I<dir-of-adapters/perl>` and `-MDevel::TdbCompile` into the perl argv before the program, and sets `TDB_COMPILE_FILE` in the child env to the program path exactly as perl will report it in `caller` (the same string perl5db uses for `main::(<file>:<line>)`). Verify the match empirically — a mismatch silently disables the whole feature.
2. Attach mode (`Devel::TdbRemote`) is unchanged: the program is already running when tdb attaches, so there is no compile phase to catch.
3. **No stop may ever surface inside `TdbCompile.pm`.** The verified prototype leaked exactly one stop at its own `goto &$orig;` line during the START→RUN transition. Fix it — either by keeping the shim installed and making the RUN branch a pure pass-through, or by having the adapter auto-step past a stop whose file is `TdbCompile.pm` (the same pattern `_on_attach` already uses to step out of `TdbRemote`). Add an assertion for this to the tests.
4. Add `Devel::TdbHelper::phase()` to `helpers.pl`, emitting `{"phase": "${^GLOBAL_PHASE}"}`. Keep it inside an `eval` and degrade to a JSON error like every other helper. Do NOT bump `$PROTOCOL`.
5. **Breakpoint deferral (critical — see Background §8).** During the compile phase the user's file is only partially compiled, so `breakable()` reports a partial line table and `b file:line` answers `not breakable` for later lines. `_on_setBreakpoints` must therefore, when `phase()` reports `START`: store the request as pending, respond with the requested breakpoints marked `verified: false`, and NOT call `breakable()` or `b` at all (calling `breakable()` on a partially-compiled file risks the line-table poisoning documented in `helpers.pl`'s own comments). When the adapter next observes a stop with phase `RUN`, flush every pending request through the existing `_on_setBreakpoints` logic and emit a DAP `breakpoint` **changed** event per breakpoint so the UI updates its verified state.
6. Statement-stepping and END-block behavior must not regress (Background §5).

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_perl_begin_blocks.py
"""BEGIN blocks are steppable: tdb stops during perl's compile phase."""

import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, "tests/integration")
from perl_adapter_harness import AdapterClient

pytestmark = pytest.mark.skipif(
    shutil.which("perl") is None
    or subprocess.run(["perl", "-e", "require v5.18"]).returncode != 0,
    reason="perl >= 5.18 required",
)

WITH_BEGIN = """use strict;
use warnings;

BEGIN {
    my $x = 10;
    my $y = $x * 2;
    print STDERR "begin $x $y\\n";
}

my $b = 20;
print STDERR "main $b\\n";

END {
    my $z = 99;
    print STDERR "end $z\\n";
}
"""

NO_PRAGMAS = 'my $a = 1;\nmy $b = 2;\nprint STDERR "x\\n";\n'


async def _launch(tmp_path, source, name="p.pl"):
    prog = tmp_path / name
    prog.write_text(source)
    c = AdapterClient()
    await c.start()
    await c.request("initialize", {})
    c.send("launch", {"program": str(prog), "cwd": str(tmp_path)})
    await c.wait_event("initialized")
    await c.request("configurationDone", {})
    await c.wait_event("stopped")
    return c, str(prog)


async def _where(c):
    st = await c.request("stackTrace", {"threadId": 1})
    f = st["body"]["stackFrames"][0]
    return f["source"]["path"], f["line"], f["name"]


async def test_first_stop_is_compile_phase_statement(tmp_path):
    c, prog = await _launch(tmp_path, WITH_BEGIN)
    try:
        path, line, _ = await _where(c)
        assert path == prog
        assert line == 1  # `use strict;` -- a compile-time statement
    finally:
        await c.stop()


async def test_step_reaches_inside_the_begin_block(tmp_path):
    c, prog = await _launch(tmp_path, WITH_BEGIN)
    try:
        seen = []
        for _ in range(12):
            path, line, name = await _where(c)
            seen.append((line, name))
            if line == 5:  # `my $x = 10;` inside BEGIN
                break
            await c.request("stepIn", {"threadId": 1})
            await c.wait_event("stopped", timeout=20)
        assert any(line == 5 for line, _ in seen), f"never entered BEGIN: {seen}"
        assert all(
            "TdbCompile" not in str(p) for p in [prog]
        )  # sanity; real check below
    finally:
        await c.stop()


async def test_no_stop_is_reported_inside_tdbcompile_pm(tmp_path):
    c, prog = await _launch(tmp_path, WITH_BEGIN)
    try:
        for _ in range(20):
            path, _, _ = await _where(c)
            assert "TdbCompile.pm" not in path, "leaked a stop inside tdb's own shim"
            await c.request("stepIn", {"threadId": 1})
            try:
                await c.wait_event("stopped", timeout=15)
            except AssertionError:
                break
    finally:
        await c.stop()


async def test_program_without_compile_time_statements_unchanged(tmp_path):
    c, prog = await _launch(tmp_path, NO_PRAGMAS)
    try:
        path, line, _ = await _where(c)
        assert (path, line) == (prog, 1)
    finally:
        await c.stop()


async def test_breakpoints_set_during_compile_phase_still_fire(tmp_path):
    """Regression for the partial-line-table hazard: a breakpoint requested
    at the first (compile-phase) stop must fire once the program runs."""
    c, prog = await _launch(tmp_path, WITH_BEGIN)
    try:
        await c.request(
            "setBreakpoints",
            {"source": {"path": prog}, "breakpoints": [{"line": 11}]},
        )
        await c.request("continue", {"threadId": 1})
        await c.wait_event("stopped", timeout=30)
        path, line, _ = await _where(c)
        assert (path, line) == (prog, 11)
    finally:
        await c.stop()


async def test_end_block_still_steppable(tmp_path):
    """Background 5: END blocks already worked; must not regress."""
    c, prog = await _launch(tmp_path, WITH_BEGIN)
    try:
        await c.request(
            "setBreakpoints",
            {"source": {"path": prog}, "breakpoints": [{"line": 11}]},
        )
        await c.request("continue", {"threadId": 1})
        await c.wait_event("stopped", timeout=30)
        seen = []
        for _ in range(8):
            await c.request("stepIn", {"threadId": 1})
            try:
                await c.wait_event("stopped", timeout=15)
            except AssertionError:
                break
            _, line, name = await _where(c)
            seen.append((line, name))
        assert any("END" in str(name) for _, name in seen), f"END not reached: {seen}"
    finally:
        await c.stop()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/integration/test_perl_begin_blocks.py -q --no-cov`
Expected: the compile-phase tests fail (first stop is the first *runtime* statement, line 10). `test_end_block_still_steppable` and `test_program_without_compile_time_statements_unchanged` may already pass — that is correct and expected.

- [ ] **Step 3: Implement** the shim, the launch wiring, `phase()`, and breakpoint deferral.

- [ ] **Step 4: Verify green, and re-check the whole Perl suite**

Run: `uv run pytest tests/integration/test_perl_begin_blocks.py -q --no-cov`, then `uv run pytest tests/unit -q --no-cov` and `uv run pytest tests/integration -q --no-cov`.
Several pre-existing Perl tests assert the entry-stop location; where the new default legitimately changes it, update those expectations and say so in your report — but if a pre-existing test fails in a way that indicates a real behavior regression (breakpoints not firing, inspection broken, attach changed), fix the product, not the test.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/adapters/perl/Devel/TdbCompile.pm src/tdb/adapters/perl/helpers.pl src/tdb/adapters/perl/session.py src/tdb/adapters/perl/server.py tests/integration/test_perl_begin_blocks.py
git commit -m "feat: stop during perl's compile phase so BEGIN blocks are steppable

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Documentation + full-suite gate

**Files:**
- Modify: `README.md`
- Modify: `SKILL.md` if it documents Perl behavior (check)
- Test: full suites

- [ ] **Step 1: Document** in the Perl section of README.md:
  - Fatal Perl errors now open the error modal with the message, the call stack, and Code View navigation to the failing line — same as Python.
  - tdb stops during perl's **compile phase**, so the first stop is the first compile-time statement of your program (typically `use strict;` on line 1-2) rather than the first runtime statement, and `BEGIN` blocks can be stepped into.
  - `END` blocks are entered with **step-in** (`s`); `next`/`continue` run past them to termination.
  - Limitation: breakpoints requested while the program is still compiling are reported unverified and applied once compilation finishes, because perl's line table is incomplete during the compile phase. A breakpoint *inside* a `BEGIN` block therefore may not fire on the first run — step into the block from the initial stop instead.
  - Limitation: the compile-phase shim adds a per-statement filter during compilation, so programs with very large `use` graphs start more slowly under tdb.

- [ ] **Step 2: Full-suite gate**

Run: `uv run pytest tests/unit -q --no-cov` and `uv run pytest tests/integration -q --no-cov`. Both must be green; report the counts.

- [ ] **Step 3: Manual end-to-end verification against the user's own file**

Run the adapter against `work/has_begin.pl` (a scripted `AdapterClient` session is fine — do not try to drive the TUI) and confirm in your report, with pasted output: (a) a stop occurs inside the BEGIN block at line 7, and (b) the stderr the adapter forwards, when fed to `parse_perl_error`, yields message `Illegal division by zero` with a frame at `has_begin.pl:10`.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: perl error modal and compile-phase BEGIN debugging

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

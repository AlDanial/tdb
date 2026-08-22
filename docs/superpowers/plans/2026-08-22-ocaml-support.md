# OCaml Debugging Support (Multicore-First) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Debug OCaml programs in tdb: native executables via lldb-dap with
OCaml 5 domains presented as threads (the multicore headline), plus bytecode
via ocamlearlybird for rich single-domain inspection.

**Architecture:** One new `LanguageProfile` (`languages/ocaml.py`) with two
stock stdio DAP adapters — `OCamlLldbAdapter` (subclasses the existing cpp
`LldbDapAdapter`, adds lldb formatter injection + `OCAMLRUNPARAM=b`) and
`EarlybirdAdapter`. OCaml-specific behavior lands in existing extension
points: detection in `registry.py`, error parsing in `languages/errors.py`,
plus three new profile hooks (`Presentation.frame_name` demangling,
`ProfileCapabilities.classify_threads` for domain labeling/hiding).
No proxy shim.

**Tech Stack:** Python 3.10+, textual, DAP over stdio; lldb-dap (LLVM ≥ 17),
`gdb -i dap` (≥ 14, fallback), ocamlearlybird (opam); OCaml ≥ 5.0 toolchain
for fixtures/integration tests only.

**Spec:** `docs/superpowers/specs/2026-08-22-ocaml-support-design.md`
(read it first; Task 1 appends a "Probe-verified facts" section that later
tasks must reconcile against).

## Global Constraints

- Platforms: Linux + macOS. On Windows `build_ocaml_profile` raises
  `LanguageNotSupportedError("OCaml debugging is not supported on Windows yet")`.
- Invocation: executable path only; tdb never invokes dune. `tdb main.ml`
  is an actionable error.
- No new Python runtime dependencies. Use `uv` for any pip operations.
- Profile compartmentalization rules (header of `src/tdb/languages/base.py`):
  profiles never import controller/app/widgets; capability values are
  data/callables consumed via `is not None` gates.
- Test commands: `uv run pytest <path> -v`. Integration tests must
  skip-gate on toolchain availability (pattern:
  `tests/integration/test_cpp_pause.py`).
- All paths below are relative to the repo root (`work/`). Branch:
  `add-ocaml-support`.
- Commit messages end with:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` and
  `Claude-Session: https://claude.ai/code/session_01P1WmSf7SKFp1gaAxYndGwY`.

---

### Task 1: Validation probe (spec "Task 0")

Answers the spec's five open questions before any dependent work. Requires
a local OCaml ≥ 5.0 toolchain (`ocamlopt`, `ocamlc`), `lldb-dap`, and
`ocamlearlybird` (`opam install earlybird`). If a tool is missing, install
it first (`sudo apt-get install ocaml lldb` or opam equivalents); if it
cannot be installed, STOP and report — later tasks depend on these findings.

**Files:**
- Create: `tests/integration/fixtures/ocaml_domains.ml`
- Create: `tests/integration/fixtures/ocaml_fatal.ml`
- Create: `tests/integration/ocaml_probe.py` (manual script, not a pytest test)
- Modify: `docs/superpowers/specs/2026-08-22-ocaml-support-design.md`
  (append findings section)

**Interfaces:**
- Produces: a "## Probe-verified facts (2026-08-22)" section in the spec
  answering: (1) earlybird CLI invocation + launch-body field names,
  (2) DWARF locals visible per frame under lldb, (3) domain/backup-thread
  naming + distinguishing stack frames, (4) earlybird `pause` support,
  (5) `caml_fatal_uncaught_exception` breakpoint validity. Tasks 5, 7, 9,
  10 read this section.

- [ ] **Step 1: Write the fixtures**

`tests/integration/fixtures/ocaml_domains.ml` (stdlib only — no unix dep):

```ocaml
let counter = Atomic.make 0

let worker n =
  for _ = 1 to 100_000 do
    Atomic.incr counter;          (* breakpoint line for domain tests *)
    Domain.cpu_relax ()
  done;
  n

let () =
  let ds = List.init 3 (fun i -> Domain.spawn (fun () -> worker (i + 1))) in
  let results = List.map Domain.join ds in
  Printf.printf "sum=%d total=%d\n"
    (List.fold_left ( + ) 0 results)
    (Atomic.get counter)
```

`tests/integration/fixtures/ocaml_fatal.ml`:

```ocaml
let boom () = failwith "boom"
let middle () = boom ()
let () = middle ()
```

- [ ] **Step 2: Verify the fixtures compile both ways**

Run:
```bash
cd tests/integration/fixtures
ocamlopt -g -o ocaml_domains.exe ocaml_domains.ml && ./ocaml_domains.exe
ocamlc  -g -o ocaml_fatal.byte  ocaml_fatal.ml   && ./ocaml_fatal.byte; echo "exit=$?"
rm -f *.cm* *.o ocaml_domains.exe ocaml_fatal.byte
```
Expected: `sum=6 total=300000`; the bytecode run prints
`Fatal error: exception Failure("boom")` and a nonzero exit. Record the
exact fatal-error text (with `OCAMLRUNPARAM=b ./ocaml_fatal.byte`) for
Task 3's parser tests.

- [ ] **Step 3: Write the probe script**

`tests/integration/ocaml_probe.py`. The lldb-dap side reuses tdb's own
stack via the existing cpp profile (no OCaml profile exists yet); the
earlybird side uses a minimal raw stdio DAP speaker because its launch
fields are exactly what we're probing.

```python
"""Manual probe: answers the 5 open questions in the OCaml design spec.

Run:  uv run python tests/integration/ocaml_probe.py
Not a pytest test. Prints findings as labeled sections; copy them into
docs/superpowers/specs/2026-08-22-ocaml-support-design.md.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


# ---------- native / lldb-dap side (reuses tdb's cpp profile) ----------

async def probe_lldb() -> None:
    from tdb.dap.types import SourceBreakpoint
    from tdb.languages.cpp import build_cpp_profile
    from tdb.server.event_handler import ServerEventHandler
    from tdb.session.controller import DebugController

    exe = FIXTURES / "ocaml_domains.exe"
    subprocess.run(
        ["ocamlopt", "-g", "-o", str(exe), "ocaml_domains.ml"],
        cwd=FIXTURES, check=True,
    )
    # Same launch sequence as tests/integration/test_cpp_pause.py /
    # test_cpp_session.py's _launch: start, wait initialized, seed
    # state.breakpoints, do_configure, wait for the stop.
    handler = ServerEventHandler()
    controller = DebugController(handler, profile=build_cpp_profile(adapter="lldb-dap"))
    await controller.start(program=str(exe), stop_on_entry=False)
    await asyncio.wait_for(handler.initialized_event.wait(), 30)
    # Breakpoint on the Atomic.incr line (line 6) — hit from a spawned
    # domain. Seed exactly as test_cpp_session.py's _launch does (read it;
    # SourceBreakpoint's constructor shape comes from there).
    src = str(FIXTURES / "ocaml_domains.ml")
    controller.state.breakpoints.setdefault(src, []).append(SourceBreakpoint(line=6))
    await controller.do_configure()
    assert await handler.wait_for_stop(30), "breakpoint never hit"

    print("== Q3: thread naming (raw DAP threads while stopped) ==")
    threads = await controller.client.threads()
    for t in threads:
        frames = await controller.client.stack_trace(t.id)
        top = [f.name for f in frames[:6]]
        print(f"  id={t.id} name={t.name!r} top_frames={top}")

    print("== Q2: DWARF locals in an OCaml frame ==")
    for t in threads:
        frames = await controller.client.stack_trace(t.id)
        for f in frames[:3]:
            try:
                scopes = await controller.client.scopes(f.id)
                for s in scopes:
                    var_list = await controller.client.variables(
                        s.variables_reference)
                    print(f"  frame={f.name!r} scope={s.name}: "
                          f"{[(v.name, v.value, v.type) for v in var_list]}")
            except Exception as e:
                print(f"  frame={f.name!r}: scopes failed: {e}")
        break  # one thread's worth is enough signal

    print("== Q5: caml_fatal_uncaught_exception symbol ==")
    nm = subprocess.run(["nm", str(exe)], capture_output=True, text=True)
    hits = [l for l in nm.stdout.splitlines() if "fatal_uncaught" in l]
    print(f"  nm hits: {hits or 'NONE — record fallback'}")

    await controller.stop()


# ---------- bytecode / earlybird side (raw stdio DAP) ----------

class RawDap:
    """Minimal Content-Length-framed DAP over a subprocess's stdio."""

    def __init__(self, argv: list[str]) -> None:
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        self.seq = 0

    def send(self, command: str, arguments: dict | None = None) -> int:
        self.seq += 1
        msg = {"seq": self.seq, "type": "request", "command": command}
        if arguments is not None:
            msg["arguments"] = arguments
        raw = json.dumps(msg).encode()
        self.proc.stdin.write(
            b"Content-Length: %d\r\n\r\n%s" % (len(raw), raw))
        self.proc.stdin.flush()
        return self.seq

    def recv(self, timeout: float = 15.0) -> dict:
        # Blocking framed read; fine for a manual probe.
        header = b""
        while b"\r\n\r\n" not in header:
            b1 = self.proc.stdout.read(1)
            if not b1:
                raise EOFError(self.proc.stderr.read().decode())
            header += b1
        length = int(header.split(b"Content-Length:")[1].split(b"\r\n")[0])
        return json.loads(self.proc.stdout.read(length))

    def recv_until(self, pred) -> dict:
        while True:
            msg = self.recv()
            print(f"    <- {msg.get('type')} "
                  f"{msg.get('command') or msg.get('event')}: "
                  f"{json.dumps(msg)[:300]}")
            if pred(msg):
                return msg


def probe_earlybird() -> None:
    byte = FIXTURES / "ocaml_fatal.byte"
    subprocess.run(["ocamlc", "-g", "-o", str(byte), "ocaml_fatal.ml"],
                   cwd=FIXTURES, check=True)
    print("== Q1: earlybird invocation + launch fields ==")
    help_out = subprocess.run(["ocamlearlybird", "--help"],
                              capture_output=True, text=True)
    print(help_out.stdout or help_out.stderr)

    dap = RawDap(["ocamlearlybird", "debug"])
    dap.send("initialize", {"adapterID": "probe", "clientID": "tdb"})
    init = dap.recv_until(lambda m: m.get("command") == "initialize")
    print(f"  capabilities: {json.dumps(init.get('body', {}), indent=2)}")
    # Try the field names the plan assumes; if the response is an error,
    # its message names the expected schema — record it.
    dap.send("launch", {
        "program": str(byte), "arguments": [], "cwd": str(FIXTURES),
        "stopOnEntry": True, "console": "internalConsole",
    })
    dap.recv_until(lambda m: m.get("event") == "initialized"
                   or m.get("command") == "launch")
    dap.send("configurationDone")
    stopped = dap.recv_until(lambda m: m.get("event") == "stopped"
                             or m.get("event") == "terminated")
    if stopped.get("event") == "stopped":
        print("== Q4: pause support ==")
        dap.send("threads")
        tmsg = dap.recv_until(lambda m: m.get("command") == "threads")
        tid = tmsg["body"]["threads"][0]["id"]
        dap.send("continue", {"threadId": tid})
        dap.recv_until(lambda m: m.get("command") == "continue")
        dap.send("pause", {"threadId": tid})
        print("  (watch whether a 'stopped' event or an error follows)")
        dap.recv_until(lambda m: m.get("event") in ("stopped", "terminated")
                       or m.get("command") == "pause")
    dap.proc.kill()


if __name__ == "__main__":
    if shutil.which("lldb-dap") and shutil.which("ocamlopt"):
        asyncio.run(probe_lldb())
    else:
        print("SKIP lldb side: need lldb-dap + ocamlopt")
    if shutil.which("ocamlearlybird") and shutil.which("ocamlc"):
        probe_earlybird()
    else:
        print("SKIP earlybird side: need ocamlearlybird + ocamlc")
```

Note: `DebugController`'s exact construction/start API — mirror whatever
`tests/integration/test_cpp_pause.py` does (read that file and copy its
harness idioms; the snippet above shows intent, the cpp test shows the
authoritative call sequence).

- [ ] **Step 4: Run the probe and capture output**

Run: `uv run python tests/integration/ocaml_probe.py | tee /tmp/ocaml_probe.txt`
Expected: both sections print findings (or a named SKIP explaining what to
install). Iterate on the earlybird launch fields until a `stopped` event
arrives; every field-name correction IS a finding.

- [ ] **Step 5: Record findings in the spec**

Append to `docs/superpowers/specs/2026-08-22-ocaml-support-design.md` a
section `## Probe-verified facts (2026-08-22, ocaml X.Y / earlybird X.Y /
lldb X.Y)` with one bullet per question Q1–Q5, including exact launch-body
JSON that worked, thread names/frames observed, and locals visibility.
State explicitly if a fallback triggers (e.g. "Q5: symbol absent — skip
the preRunCommands breakpoint, rely on parse-on-exit modal").

- [ ] **Step 6: Clean fixtures build artifacts and commit**

```bash
cd tests/integration/fixtures && rm -f *.cm* *.o ocaml_domains.exe ocaml_fatal.byte && cd -
git add tests/integration/fixtures/ocaml_domains.ml \
        tests/integration/fixtures/ocaml_fatal.ml \
        tests/integration/ocaml_probe.py \
        docs/superpowers/specs/2026-08-22-ocaml-support-design.md
git commit -m "probe: verify OCaml adapter facts (earlybird fields, DWARF locals, thread naming)"
```

---

### Task 2: Detection — flavor sniffing + registry integration

**Files:**
- Create: `src/tdb/languages/ocaml.py` (sniffing helpers only in this task)
- Modify: `src/tdb/languages/registry.py`
- Modify: `src/tdb/cli.py:403-405` (pass `program=` to resolve)
- Test: `tests/unit/test_ocaml_detection.py`

**Interfaces:**
- Produces: `ocaml_flavor(program: str) -> str | None` returning
  `"native"`, `"bytecode"`, or `None`, in `tdb.languages.ocaml`;
  `registry.detect()` returns `"ocaml"` for OCaml executables;
  `registry.resolve(lang_id, adapter=None, adapter_paths=None,
  program=None)` — new `program` kwarg forwarded to every builder.
- Consumes: nothing from other tasks.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_ocaml_detection.py`:

```python
"""Detection of OCaml executables: bytecode trailer/shebang, ELF+caml marker."""

from __future__ import annotations

import struct

import pytest

from tdb.languages.base import LanguageNotSupportedError
from tdb.languages.ocaml import ocaml_flavor
from tdb.languages import registry

ELF_MAGIC = b"\x7fELF" + b"\x00" * 60


def _write(tmp_path, name, data: bytes):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def test_bytecode_trailer(tmp_path):
    # ocamlc output: arbitrary body, trailer ends ...Caml1999X033
    p = _write(tmp_path, "prog", b"\x00" * 200 + b"Caml1999X033")
    assert ocaml_flavor(p) == "bytecode"
    assert registry.detect(p) == "ocaml"


def test_bytecode_shebang(tmp_path):
    p = _write(tmp_path, "prog", b"#!/usr/bin/ocamlrun\n" + b"\x00" * 50)
    assert ocaml_flavor(p) == "bytecode"
    assert registry.detect(p) == "ocaml"


def test_native_elf_with_caml_marker(tmp_path):
    p = _write(tmp_path, "prog",
               ELF_MAGIC + b"\x00" * 100 + b"caml_program" + b"\x00" * 100)
    assert ocaml_flavor(p) == "native"
    assert registry.detect(p) == "ocaml"


def test_plain_elf_stays_cpp(tmp_path):
    p = _write(tmp_path, "prog", ELF_MAGIC + b"\x00" * 300)
    assert ocaml_flavor(p) is None
    assert registry.detect(p) == "cpp"


def test_marker_in_tail_chunk_of_large_binary(tmp_path):
    # marker beyond the head chunk: found by the tail scan
    body = ELF_MAGIC + b"\x00" * (3 * 1024 * 1024) + b"caml_startup"
    p = _write(tmp_path, "prog", body)
    assert ocaml_flavor(p) == "native"


def test_ml_source_is_actionable_error(tmp_path):
    p = _write(tmp_path, "main.ml", b"let () = ()\n")
    with pytest.raises(LanguageNotSupportedError, match="dune"):
        registry.detect(p)


def test_resolve_accepts_program_kwarg():
    # every builder must tolerate program=None / a path
    for lang in registry.known_languages():
        if lang == "go":
            continue  # extension-mapped but unregistered
        registry.resolve(lang, program=None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_ocaml_detection.py -v`
Expected: FAIL — `ModuleNotFoundError: tdb.languages.ocaml` (and, once the
module exists, cpp/error mismatches). Also note whether
`test_resolve_accepts_program_kwarg` fails on languages whose builder can
raise for other reasons (e.g. ocaml on Windows guard later) — the test
runs on Linux/macOS so it must pass there.

- [ ] **Step 3: Implement sniffing in `src/tdb/languages/ocaml.py`**

```python
"""The OCaml language profile (built up across Tasks 2-7).

This task: executable-flavor sniffing used by registry.detect().
"""

from __future__ import annotations

from pathlib import Path

_BYTECODE_TRAILER_MARK = b"Caml1999"  # e.g. b"Caml1999X033" at file end
_NATIVE_MAGIC = (
    b"\x7fELF",
    b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe",
)
_CAML_MARKERS = (b"caml_program", b"caml_startup")
_SCAN_CHUNK = 2 * 1024 * 1024  # spec risk 5: bounded scan, head + tail


def ocaml_flavor(program: str) -> str | None:
    """"native"/"bytecode" when `program` is an OCaml executable, else None.

    Best-effort byte sniffing: a stripped native binary may return None
    (lands in cpp; --lang ocaml overrides — documented in README).
    """
    path = Path(program)
    try:
        with open(path, "rb") as f:
            head = f.read(64)
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 16))
            tail = f.read()
    except OSError:
        return None
    if head.startswith(b"#!") and b"ocamlrun" in head.splitlines()[0]:
        return "bytecode"
    if _BYTECODE_TRAILER_MARK in tail:
        return "bytecode"
    if any(head.startswith(m) for m in _NATIVE_MAGIC):
        if _scan_for_caml_marker(path, size):
            return "native"
    return None


def _scan_for_caml_marker(path: Path, size: int) -> bool:
    try:
        with open(path, "rb") as f:
            if any(m in f.read(_SCAN_CHUNK) for m in _CAML_MARKERS):
                return True
            if size > _SCAN_CHUNK:
                # overlap by 16 bytes so a marker straddling the boundary
                # of head and tail chunks is still seen for mid-size files
                f.seek(max(_SCAN_CHUNK - 16, size - _SCAN_CHUNK))
                data = f.read()
                return any(m in data for m in _CAML_MARKERS)
    except OSError:
        pass
    return False
```

- [ ] **Step 4: Integrate into `registry.py`**

In `src/tdb/languages/registry.py`:

(a) Add `.ml`/`.mli` handling right after the `_COMPILED_SOURCE_EXTS`
check in `detect()` (keep the OCaml message dune-specific):

```python
    if ext in (".ml", ".mli"):
        raise LanguageNotSupportedError(
            f"{program!r} is OCaml source — build it first (dune's dev "
            f"profile keeps debug info) and run "
            f"`tdb ./_build/default/.../main.exe`, or pass --lang explicitly"
        )
```

(b) Before the `_MAGIC` loop in `detect()`, add:

```python
    from tdb.languages.ocaml import ocaml_flavor  # lazy: avoid import cycle

    if ocaml_flavor(program) is not None:
        return "ocaml"
```

(c) Change `resolve()`'s signature and forwarding:

```python
def resolve(
    lang_id: str,
    adapter: str | None = None,
    adapter_paths: dict[str, str] | None = None,
    program: str | None = None,
) -> LanguageProfile:
    ...
    return builder(adapter=adapter, adapter_paths=adapter_paths, program=program)
```

(d) Add `program: str | None = None` to the signature of EVERY registered
builder (`build_python_profile`, `build_cpp_profile`, `build_perl_profile`,
`build_bash_profile`, `build_tcsh_profile`, `build_ruby_profile`) — each
ignores it. In `src/tdb/cli.py` (~line 405) pass it through:

```python
        profile = registry.resolve(
            lang_id,
            adapter=adapter,
            adapter_paths=config.adapters,
            program=args.program,
        )
```
(Match the existing call's exact keyword names — read the call before
editing; only ADD `program=`.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_ocaml_detection.py -v`
Expected: all PASS except any that reference `registry.detect` returning
`"ocaml"` before registration — registration happens in Task 5; if
`resolve("ocaml", ...)` appears in a failure, confirm the test above only
resolves `known_languages()` (it does). Then run the full unit suite to
catch builder-signature fallout: `uv run pytest tests/unit -x -q`.
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/tdb/languages/ocaml.py src/tdb/languages/registry.py \
        src/tdb/cli.py src/tdb/languages/*.py tests/unit/test_ocaml_detection.py
git commit -m "feat: detect OCaml native/bytecode executables; resolve() gains program kwarg"
```

---

### Task 3: Fatal-error parser

**Files:**
- Modify: `src/tdb/languages/errors.py` (append after `parse_ruby_error`)
- Test: `tests/unit/test_error_parsers.py` (append a class/section, matching
  the file's existing structure — read it first and mimic)

**Interfaces:**
- Produces: `parse_ocaml_error(stderr: str, exit_code: int | None = None)
  -> ParsedError | None` in `tdb.languages.errors`.
- Consumes: `ParsedError`, `ErrorFrame` from `tdb.languages.base`
  (existing).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_error_parsers.py` (use the real texts captured
in Task 1 Step 2 if they differ from these canonical forms):

```python
OCAML_WITH_BACKTRACE = """\
Fatal error: exception Failure("boom")
Raised at Stdlib.failwith in file "stdlib.ml", line 29, characters 17-33
Called from Fatal.boom in file "ocaml_fatal.ml", line 1, characters 15-31
Called from Fatal.middle in file "ocaml_fatal.ml", line 2, characters 18-25
Called from Fatal in file "ocaml_fatal.ml", line 3, characters 9-18
"""

OCAML_NO_BACKTRACE = 'Fatal error: exception Failure("boom")\n'


def test_ocaml_error_with_backtrace():
    from tdb.languages.errors import parse_ocaml_error

    err = parse_ocaml_error(OCAML_WITH_BACKTRACE, 2)
    assert err is not None
    assert err.header == 'Fatal error: exception Failure("boom")'
    assert err.message == 'Failure("boom")'
    # OUTERMOST-first (source order), like python's parser
    assert [f.func for f in err.frames] == [
        "Fatal", "Fatal.middle", "Fatal.boom", "Stdlib.failwith"]
    assert err.frames[0].path == "ocaml_fatal.ml"
    assert err.frames[0].line == 3
    assert 'Raised at Stdlib.failwith' in err.detail


def test_ocaml_error_without_backtrace_has_hint():
    from tdb.languages.errors import parse_ocaml_error

    err = parse_ocaml_error(OCAML_NO_BACKTRACE, 2)
    assert err is not None
    assert err.frames == []
    assert "compile with -g" in err.detail


def test_ocaml_error_none_on_clean_output():
    from tdb.languages.errors import parse_ocaml_error

    assert parse_ocaml_error("all good\n", 0) is None
    assert parse_ocaml_error("", None) is None


def test_ocaml_reraised_and_inlined_frames():
    from tdb.languages.errors import parse_ocaml_error

    text = (
        "Fatal error: exception Not_found\n"
        'Raised by primitive operation at M.find in file "m.ml" (inlined),'
        " line 7, characters 1-9\n"
        'Re-raised at M.wrap in file "m.ml", line 12, characters 4-11\n'
    )
    err = parse_ocaml_error(text, 2)
    assert err is not None
    assert [f.func for f in err.frames] == ["M.wrap", "M.find"]
    assert err.frames[0].line == 12
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_error_parsers.py -k ocaml -v`
Expected: FAIL — `ImportError: cannot import name 'parse_ocaml_error'`.

- [ ] **Step 3: Implement the parser**

Append to `src/tdb/languages/errors.py`:

```python
# --- OCaml ----------------------------------------------------------------
#
# With OCAMLRUNPARAM=b (injected by the OCaml adapters) an uncaught
# exception prints, on stderr, identically for native and bytecode:
#
#   Fatal error: exception Failure("boom")
#   Raised at Stdlib.failwith in file "stdlib.ml", line 29, characters 17-33
#   Called from Fatal.middle in file "ocaml_fatal.ml", line 2, characters ...
#
# Without -g the backtrace lines are absent — header-only modal plus a hint.

_OCAML_FATAL_RE = re.compile(
    r"^Fatal error: exception (?P<msg>.+?)\s*$", re.MULTILINE
)
_OCAML_FRAME_RE = re.compile(
    r"^(?:Raised at|Raised by primitive operation at|Re-raised at"
    r"|Called from) (?P<func>.+?) in file \"(?P<path>[^\"]+)\""
    r"(?: \(inlined\))?, line (?P<line>\d+)",
    re.MULTILINE,
)


def parse_ocaml_error(stderr: str, exit_code: int | None = None) -> ParsedError | None:
    """Parse OCaml's fatal-error output. The header line is an unambiguous
    signal, so `exit_code` is accepted and ignored (python-style)."""
    fatal = _OCAML_FATAL_RE.search(stderr)
    if fatal is None:
        return None
    header = fatal.group(0).strip()
    tail = stderr[fatal.start():]
    frames = [
        ErrorFrame(path=m.group("path"), line=int(m.group("line")),
                   func=m.group("func"))
        for m in _OCAML_FRAME_RE.finditer(tail)
    ]
    frames.reverse()  # OCaml prints innermost-first; ParsedError wants outermost-first
    detail = tail.rstrip("\n")
    if not frames:
        detail += "\n\n(no backtrace — compile with -g, e.g. dune's dev profile)"
    return ParsedError(
        header=header, message=fatal.group("msg"), frames=frames, detail=detail
    )
```

(`re`, `ErrorFrame`, `ParsedError` are already imported at the top of
`errors.py` — verify, don't re-import.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_error_parsers.py -v`
Expected: all PASS (including the pre-existing python/perl/ruby tests).

- [ ] **Step 5: Commit**

```bash
git add src/tdb/languages/errors.py tests/unit/test_error_parsers.py
git commit -m "feat: parse OCaml fatal-error output for the error modal"
```

---

### Task 4: Frame-name demangling hook (`Presentation.frame_name`)

**Files:**
- Modify: `src/tdb/languages/base.py` (`Presentation`)
- Modify: `src/tdb/languages/ocaml.py` (add `demangle_frame_name`)
- Modify: `src/tdb/widgets/stack_view.py`
- Modify: `src/tdb/widgets/threads_modal.py`
- Modify: `src/tdb/app.py:335` area (wire filter into StackView)
- Modify: `src/tdb/app_handlers/inspection.py:229` area (pass into ThreadsModal)
- Test: `tests/unit/test_ocaml_demangle.py`

**Interfaces:**
- Produces: `Presentation.frame_name: Callable[[str], str] | None = None`;
  `demangle_frame_name(name: str) -> str` in `tdb.languages.ocaml`;
  `StackView.name_filter` attribute; `ThreadsModal.__init__(...,
  frame_name: Callable[[str], str] | None = None)`.
- Consumes: `Presentation` from Task-independent base.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_ocaml_demangle.py`:

```python
from tdb.languages.ocaml import demangle_frame_name


def test_simple_symbol():
    assert demangle_frame_name("camlMain__worker_271") == "Main.worker"


def test_nested_modules():
    assert demangle_frame_name("camlFoo__Bar__run_17") == "Foo.Bar.run"


def test_no_numeric_suffix():
    assert demangle_frame_name("camlMain__entry") == "Main.entry"


def test_runtime_c_symbols_untouched():
    for name in ("caml_apply2", "caml_start_program", "main",
                 "pthread_cond_wait", "camlcase_but_no_sep"):
        assert demangle_frame_name(name) == name


def test_presentation_has_frame_name_field():
    from tdb.languages.base import Presentation

    assert Presentation().frame_name is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_ocaml_demangle.py -v`
Expected: FAIL — `ImportError: cannot import name 'demangle_frame_name'`.

- [ ] **Step 3: Implement**

(a) `src/tdb/languages/base.py`, inside `Presentation` after
`frame_placeholder`:

```python
    # Rewrite a stack frame's display name (e.g. demangle OCaml's
    # "camlMain__worker_271" -> "Main.worker"). Display-only: DAP frame
    # ids/sources are untouched. None -> show adapter names verbatim.
    frame_name: Callable[[str], str] | None = None
```

(b) `src/tdb/languages/ocaml.py`:

```python
import re

_MANGLED_SUFFIX_RE = re.compile(r"_\d+$")


def demangle_frame_name(name: str) -> str:
    """"camlFoo__Bar__run_17" -> "Foo.Bar.run"; anything else unchanged.

    Runtime C symbols (caml_apply2, caml_start_program) contain no "__"
    after the caml prefix, so they pass through.
    """
    if not name.startswith("caml") or "__" not in name:
        return name
    body = _MANGLED_SUFFIX_RE.sub("", name[len("caml"):])
    return body.replace("__", ".")
```

(c) `src/tdb/widgets/stack_view.py` — in `__init__` add
`self.name_filter: Callable[[str], str] | None = None` (add
`from typing import Callable` import if absent; keep it under
TYPE_CHECKING-safe plain import since it's runtime-used). In
`update_frames`, change the `add_row` line:

```python
                display_name = (
                    self.name_filter(frame.name) if self.name_filter else frame.name
                )
                self.add_row(str(i), display_name, source_name, key=str(frame.id))
```

(d) `src/tdb/widgets/threads_modal.py` — `__init__` gains
`frame_name: Callable[[str], str] | None = None`, stored as
`self._frame_name`. In `show_thread_detail`'s stack loop:

```python
                name = self._frame_name(frame.name) if self._frame_name else frame.name
                content.append(f"  #{i} {name}{loc}\n")
```

(e) `src/tdb/app.py` — in the method containing line 335
(`code_view.lexer_name = ...`), add immediately after:

```python
        stack_view = self.query_one("#stack-view", StackView)
        stack_view.name_filter = self.controller.profile.presentation.frame_name
```

(f) `src/tdb/app_handlers/inspection.py` (~line 229):

```python
        modal = ThreadsModal(
            threads,
            ctrl.state.current_thread_id,
            frame_name=ctrl.profile.presentation.frame_name,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_ocaml_demangle.py tests/unit -x -q`
Expected: PASS (full unit suite guards the widget-constructor change —
any existing ThreadsModal test that constructs positionally must still
pass since the new kwarg is last and defaulted).

- [ ] **Step 5: Commit**

```bash
git add src/tdb/languages/base.py src/tdb/languages/ocaml.py \
        src/tdb/widgets/stack_view.py src/tdb/widgets/threads_modal.py \
        src/tdb/app.py src/tdb/app_handlers/inspection.py \
        tests/unit/test_ocaml_demangle.py
git commit -m "feat: Presentation.frame_name hook + OCaml symbol demangling"
```

---

### Task 5: lldb formatters (decode core + glue)

**Files:**
- Create: `src/tdb/adapters/ocaml/__init__.py` (empty, package marker)
- Create: `src/tdb/adapters/ocaml/lldb_formatters.py`
- Test: `tests/unit/test_ocaml_formatters.py`

**Interfaces:**
- Produces: in `tdb.adapters.ocaml.lldb_formatters`:
  `describe_value(word: int, read_memory: Callable[[int, int], bytes | None],
  depth: int = 0) -> tuple[str, list[tuple[str, int]]]` (summary text,
  child (name, word) pairs); `formatter_script_path() -> str` in
  `tdb.languages.ocaml` (Task 6 uses it in `initCommands`).
- Consumes: nothing from other tasks. The module must import cleanly
  WITHOUT lldb (`import lldb` only inside `__lldb_init_module`/glue guards)
  so pytest can test the pure core.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_ocaml_formatters.py`:

```python
"""Pure-function tests for the OCaml value decoder (no lldb needed)."""

import struct

from tdb.adapters.ocaml.lldb_formatters import describe_value


class FakeMemory:
    """addr -> bytes store with OCaml block layout helpers (64-bit)."""

    def __init__(self):
        self.mem: dict[int, bytes] = {}

    def read(self, addr: int, size: int) -> bytes | None:
        blob = self.mem.get(addr)
        if blob is None or len(blob) < size:
            return None
        return blob[:size]

    def add_block(self, addr: int, tag: int, fields: list[int]) -> int:
        """Lay out header at addr-8 and fields at addr. Returns the value
        word (the pointer, which is even)."""
        header = (len(fields) << 10) | tag
        self.mem[addr - 8] = struct.pack("<Q", header)
        for i, f in enumerate(fields):
            self.mem[addr + 8 * i] = struct.pack("<Q", f)
        return addr

    def add_string(self, addr: int, s: bytes) -> int:
        nwords = (len(s) // 8) + 1
        data = s + b"\x00" * (nwords * 8 - len(s) - 1)
        padding = nwords * 8 - len(s) - 1
        data += bytes([padding])
        self.mem[addr - 8] = struct.pack("<Q", (nwords << 10) | 252)
        for i in range(nwords):
            self.mem[addr + 8 * i] = data[8 * i: 8 * i + 8]
        return addr


def test_immediate_int():
    summary, children = describe_value(2 * 21 + 1, lambda a, s: None)
    assert "21" in summary
    assert children == []


def test_string_block():
    m = FakeMemory()
    v = m.add_string(0x1000, b"hello")
    summary, children = describe_value(v, m.read)
    assert '"hello"' in summary
    assert children == []


def test_float_block():
    m = FakeMemory()
    m.mem[0x2000 - 8] = struct.pack("<Q", (1 << 10) | 253)
    m.mem[0x2000] = struct.pack("<d", 3.5)
    summary, _ = describe_value(0x2000, m.read)
    assert "3.5" in summary


def test_structured_block_with_children():
    m = FakeMemory()
    inner = m.add_string(0x3000, b"hi")
    v = m.add_block(0x4000, 0, [2 * 7 + 1, inner])
    summary, children = describe_value(v, m.read)
    assert "block(tag=0, size=2)" in summary
    assert children == [("[0]", 15), ("[1]", inner)]


def test_closure_and_custom_tags():
    m = FakeMemory()
    fn = m.add_block(0x5000, 247, [0x9999, 3])
    summary, children = describe_value(fn, m.read)
    assert "fun" in summary and children == []
    cu = m.add_block(0x6000, 255, [0x1234])
    summary, _ = describe_value(cu, m.read)
    assert "custom" in summary


def test_unreadable_pointer_degrades():
    summary, children = describe_value(0x7000, lambda a, s: None)
    assert "0x7000" in summary  # falls back to the raw pointer
    assert children == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_ocaml_formatters.py -v`
Expected: FAIL — `ModuleNotFoundError: tdb.adapters.ocaml`.

- [ ] **Step 3: Implement the decoder + glue**

`src/tdb/adapters/ocaml/lldb_formatters.py`:

```python
"""OCaml value decoding for lldb, loaded INTO lldb's Python via
`command script import` (see OCamlLldbAdapter.launch_body).

Layout (64-bit, spec: "Variable inspection"):
  odd word  -> immediate: int n encoded as 2n+1 (may really be bool/char/
               constructor — DWARF can't tell us, so show both forms)
  even word -> pointer to a heap block; header word at ptr-8:
               tag = header & 0xff, size (words) = header >> 10

Pure decoding lives in `describe_value` (unit-tested without lldb);
the lldb API is confined to the provider glue at the bottom.
"""

from __future__ import annotations

import struct
from typing import Callable

WORD = 8
MAX_DEPTH = 3
MAX_FIELDS = 16

_STRING_TAG = 252
_DOUBLE_TAG = 253
_DOUBLE_ARRAY_TAG = 254
_CUSTOM_TAG = 255
_ABSTRACT_TAG = 251
_CLOSURE_TAG = 247
_OBJECT_TAG = 248
_INFIX_TAG = 249
_FORWARD_TAG = 250
_LAZY_TAG = 246

ReadMemory = Callable[[int, int], "bytes | None"]


def describe_value(
    word: int, read_memory: ReadMemory, depth: int = 0
) -> tuple[str, list[tuple[str, int]]]:
    """Decode one OCaml value word.

    Returns (summary, children) where children are (display_name, word)
    pairs for expandable block fields (empty for leaves). Any unreadable
    memory degrades to a raw-pointer summary — never raises.
    """
    if word & 1:
        return f"{word >> 1} (int, raw {hex(word)})", []
    ptr = word
    header_raw = read_memory(ptr - WORD, WORD)
    if header_raw is None or len(header_raw) < WORD:
        return f"<unreadable {hex(ptr)}>", []
    header = struct.unpack("<Q", header_raw)[0]
    tag = header & 0xFF
    size = header >> 10

    if tag == _STRING_TAG:
        return _decode_string(ptr, size, read_memory), []
    if tag == _DOUBLE_TAG:
        raw = read_memory(ptr, WORD)
        if raw is None:
            return f"<unreadable float {hex(ptr)}>", []
        return repr(struct.unpack("<d", raw)[0]), []
    if tag == _DOUBLE_ARRAY_TAG:
        vals = []
        for i in range(min(size, MAX_FIELDS)):
            raw = read_memory(ptr + i * WORD, WORD)
            vals.append(repr(struct.unpack("<d", raw)[0]) if raw else "?")
        suffix = ", ..." if size > MAX_FIELDS else ""
        return f"float array [{'; '.join(vals)}{suffix}]", []
    if tag in (_CLOSURE_TAG, _INFIX_TAG):
        return "fun (closure)", []
    if tag == _CUSTOM_TAG:
        return f"custom block (size={size})", []
    if tag == _ABSTRACT_TAG:
        return f"abstract block (size={size})", []
    if tag == _OBJECT_TAG:
        return f"object (size={size})", []
    if tag == _LAZY_TAG:
        return "lazy", []
    if tag == _FORWARD_TAG:
        raw = read_memory(ptr, WORD)
        if raw is not None:
            return describe_value(struct.unpack("<Q", raw)[0], read_memory, depth)
        return f"<forward {hex(ptr)}>", []

    # Plain structured block: tuple / record / constructor with args.
    children: list[tuple[str, int]] = []
    if depth < MAX_DEPTH:
        for i in range(min(size, MAX_FIELDS)):
            raw = read_memory(ptr + i * WORD, WORD)
            if raw is None:
                break
            children.append((f"[{i}]", struct.unpack("<Q", raw)[0]))
    return f"block(tag={tag}, size={size})", children


def _decode_string(ptr: int, size: int, read_memory: ReadMemory) -> str:
    data = read_memory(ptr, size * WORD)
    if data is None:
        return f"<unreadable string {hex(ptr)}>"
    padding = data[-1]
    raw = data[: size * WORD - 1 - padding]
    try:
        return repr(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return f"bytes {raw[:32]!r}{'...' if len(raw) > 32 else ''}"


# --- lldb glue (only reachable inside lldb's embedded Python) -------------

def _read_via_process(process):
    import lldb  # noqa: F401  (import here: absent under pytest)

    def read(addr: int, size: int):
        err = __import__("lldb").SBError()
        data = process.ReadMemory(addr, size, err)
        return data if err.Success() else None

    return read


def ocaml_value_summary(valobj, _internal_dict):
    """Type summary for OCaml `value`-typed variables."""
    try:
        word = valobj.GetValueAsUnsigned()
        read = _read_via_process(valobj.GetProcess())
        summary, _children = describe_value(word, read)
        return summary
    except Exception as exc:  # never let a formatter kill the session
        return f"<ocaml decode error: {exc}>"


def __lldb_init_module(debugger, _internal_dict):
    debugger.HandleCommand(
        'type summary add -F {}.ocaml_value_summary value'.format(__name__)
    )
    debugger.HandleCommand(
        'type summary add -F {}.ocaml_value_summary "unsigned long"'
        " -x '^caml.*'".format(__name__)
    )
```

`src/tdb/adapters/ocaml/__init__.py`: empty file.

Note for the implementer: the exact `type summary add` matching that works
against real OCaml DWARF is a Task 1/Q2 finding — reconcile the two
`HandleCommand` lines with what the probe recorded (which type names lldb
reports for OCaml locals). The pure decoder and its tests do not depend on
that.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_ocaml_formatters.py -v`
Expected: all PASS. Also verify the module imports without lldb:
`uv run python -c "import tdb.adapters.ocaml.lldb_formatters as m; print(m.MAX_DEPTH)"`
Expected: `3`.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/adapters/ocaml tests/unit/test_ocaml_formatters.py
git commit -m "feat: OCaml value decoder + lldb formatter glue"
```

---

### Task 6: Adapters + profile + registration

**Files:**
- Modify: `src/tdb/languages/ocaml.py` (adapters + builder)
- Modify: `src/tdb/languages/registry.py` (register at bottom)
- Test: `tests/unit/test_ocaml_profile.py`

**Interfaces:**
- Consumes: `LldbDapAdapter`, `GdbDapAdapter` from `tdb.languages.cpp`
  (existing); `parse_ocaml_error` (Task 3); `demangle_frame_name`,
  `ocaml_flavor` (Tasks 2/4); `tdb.adapters.ocaml.lldb_formatters`
  module path (Task 5).
- Produces: `build_ocaml_profile(adapter=None, adapter_paths=None,
  program=None) -> LanguageProfile`; adapter ids `"lldb-dap"`, `"gdb"`,
  `"ocamlearlybird"`; `formatter_script_path() -> str`;
  `_with_runparam(env: dict[str, str] | None) -> dict[str, str]`.
  Registry id `"ocaml"`. Task 7 extends the same builder with
  `classify_threads`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_ocaml_profile.py` (model file structure on
`tests/unit/test_cpp_profile.py` — read it first for local conventions):

```python
import sys

import pytest

from tdb.languages.base import LanguageNotSupportedError
from tdb.languages.ocaml import (
    OCamlLldbAdapter,
    _with_runparam,
    build_ocaml_profile,
    formatter_script_path,
)


def _native_launch_body(adapter):
    return adapter.launch_body(
        program="/x/main.exe", args=["a"], cwd="/x", env=None,
        stop_on_entry=True, console="integratedTerminal", opts={},
    )


def test_lldb_launch_body_injects_formatters_and_runparam():
    body = _native_launch_body(OCamlLldbAdapter())
    assert body["program"] == "/x/main.exe"
    assert any("command script import" in c and "lldb_formatters.py" in c
               for c in body["initCommands"])
    assert any("caml_fatal_uncaught_exception" in c
               for c in body.get("preRunCommands", []))
    assert "OCAMLRUNPARAM=b" in body["env"]  # lldb-dap env is a list


def test_runparam_merge_preserves_user_flags():
    assert _with_runparam(None) == {"OCAMLRUNPARAM": "b"}
    assert _with_runparam({"OCAMLRUNPARAM": "v=61"}) == {
        "OCAMLRUNPARAM": "v=61,b"}
    assert _with_runparam({"OCAMLRUNPARAM": "b,v=61"}) == {
        "OCAMLRUNPARAM": "b,v=61"}
    assert _with_runparam({"PATH": "/x"})["PATH"] == "/x"


def test_formatter_script_path_exists():
    import os
    assert os.path.isfile(formatter_script_path())


def test_default_adapter_by_flavor(tmp_path):
    native = tmp_path / "prog"
    native.write_bytes(b"\x7fELF" + b"\x00" * 64 + b"caml_program")
    byte = tmp_path / "prog.byte"
    byte.write_bytes(b"#!/usr/bin/ocamlrun\n\x00" * 4 + b"Caml1999X033")

    assert build_ocaml_profile(program=str(native)).adapter.id == "lldb-dap"
    assert build_ocaml_profile(program=str(byte)).adapter.id == "ocamlearlybird"
    assert build_ocaml_profile(program=None).adapter.id == "lldb-dap"
    assert build_ocaml_profile(adapter="gdb", program=str(native)).adapter.id == "gdb"


def test_unknown_adapter_rejected():
    with pytest.raises(LanguageNotSupportedError, match="ocamlearlybird"):
        build_ocaml_profile(adapter="nope")


def test_presentation_and_capabilities():
    p = build_ocaml_profile(program=None)  # native default
    assert p.id == "ocaml" and p.presentation.lexer == "ocaml"
    assert p.presentation.frame_placeholder == "<top>"
    assert p.presentation.parse_error is not None
    assert p.presentation.frame_name("camlMain__f_1") == "Main.f"
    assert p.capabilities.pause_while_running is True

    b = build_ocaml_profile(adapter="ocamlearlybird")
    assert b.presentation.frame_name is None
    assert b.capabilities.pause_while_running is False  # pending probe Q4


@pytest.mark.skipif(sys.platform != "win32", reason="windows-only guard")
def test_windows_rejected():
    with pytest.raises(LanguageNotSupportedError, match="Windows"):
        build_ocaml_profile()


def test_registered():
    from tdb.languages import registry

    assert "ocaml" in registry.known_languages()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_ocaml_profile.py -v`
Expected: FAIL — `ImportError` on `OCamlLldbAdapter` etc.

- [ ] **Step 3: Implement adapters + builder**

Append to `src/tdb/languages/ocaml.py` (reconcile the earlybird
`launch_body` field names and `pause_while_running` value with the spec's
"Probe-verified facts" Q1/Q4 before writing — if the probe found different
names, use the probe's, and update this task's test to match):

```python
import shutil
import sys
from importlib import resources
from typing import Any

from tdb.languages.base import (
    AdapterNotFoundError,
    AdapterQuirks,
    AdapterSpec,
    LanguageNotSupportedError,
    LanguageProfile,
    Presentation,
    ProfileCapabilities,
)
from tdb.languages.cpp import GdbDapAdapter, LldbDapAdapter
from tdb.languages.errors import parse_ocaml_error


def formatter_script_path() -> str:
    """Absolute path of the lldb formatter script, for initCommands."""
    return str(resources.files("tdb.adapters.ocaml") / "lldb_formatters.py")


def _with_runparam(env: dict[str, str] | None) -> dict[str, str]:
    """Merge OCAMLRUNPARAM=b (backtraces) into the debuggee env without
    clobbering user flags."""
    merged = dict(env or {})
    current = merged.get("OCAMLRUNPARAM", "")
    flags = [f for f in current.split(",") if f]
    if not any(f == "b" or f.startswith("b=") for f in flags):
        flags.append("b")
    merged["OCAMLRUNPARAM"] = ",".join(flags)
    return merged


class OCamlLldbAdapter(LldbDapAdapter):
    """lldb-dap with OCaml twists: formatter injection, backtrace env,
    and a stop-before-abort breakpoint on the uncaught-exception hook."""

    def launch_body(self, *, program, args, cwd, env, stop_on_entry,
                    console, opts: dict[str, Any]) -> dict[str, Any]:
        body = super().launch_body(
            program=program, args=args, cwd=cwd, env=_with_runparam(env),
            stop_on_entry=stop_on_entry, console=console, opts=opts,
        )
        body["initCommands"] = [
            f"command script import {formatter_script_path()}",
        ]
        body["preRunCommands"] = [
            "breakpoint set --name caml_fatal_uncaught_exception",
        ]
        return body


class OCamlGdbAdapter(GdbDapAdapter):
    """gdb -i dap fallback (Linux). No formatter injection (lldb-only
    script) and no pre-run breakpoint (gdb DAP has no initCommands);
    the parse-on-exit error modal still works via OCAMLRUNPARAM=b."""

    def launch_body(self, *, program, args, cwd, env, stop_on_entry,
                    console, opts: dict[str, Any]) -> dict[str, Any]:
        return super().launch_body(
            program=program, args=args, cwd=cwd, env=_with_runparam(env),
            stop_on_entry=stop_on_entry, console=console, opts=opts,
        )


class EarlybirdAdapter(AdapterSpec):
    """ocamlearlybird: bytecode-only, stdio DAP, rich OCaml locals.
    Field names below are the probe-verified ones (spec Q1)."""

    id = "ocamlearlybird"
    quirks = AdapterQuirks()

    def __init__(self, executable: str | None = None) -> None:
        self._executable = executable

    def command(self) -> list[str]:
        exe = self._executable or shutil.which("ocamlearlybird")
        if exe is None:
            raise AdapterNotFoundError(
                "ocamlearlybird not found on PATH — `opam install earlybird`, "
                'or set {"adapters": {"ocamlearlybird": "/path/to/it"}} '
                "in tdb's config.json"
            )
        return [exe, "debug"]

    def launch_body(self, *, program, args, cwd, env, stop_on_entry,
                    console, opts: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": "ocaml",
            "request": "launch",
            "program": program,
            "arguments": args,
            "cwd": cwd,
            "stopOnEntry": stop_on_entry,
            "console": "internalConsole",
        }
        if env:
            body["env"] = _with_runparam(env)
        return body

    def attach_body(self, *, host, port, opts) -> dict[str, Any]:
        raise LanguageNotSupportedError(
            "remote attach is not supported for ocaml yet"
        )


def build_ocaml_profile(
    adapter: str | None = None,
    adapter_paths: dict[str, str] | None = None,
    program: str | None = None,
) -> LanguageProfile:
    if sys.platform == "win32":
        raise LanguageNotSupportedError(
            "OCaml debugging is not supported on Windows yet"
        )
    adapters: dict[str, type[AdapterSpec]] = {
        "lldb-dap": OCamlLldbAdapter,
        "gdb": OCamlGdbAdapter,
        "ocamlearlybird": EarlybirdAdapter,
    }
    if adapter is None:
        flavor = ocaml_flavor(program) if program else None
        adapter = "ocamlearlybird" if flavor == "bytecode" else "lldb-dap"
    if adapter not in adapters:
        raise LanguageNotSupportedError(
            f"unknown adapter {adapter!r} for ocaml "
            f"(known: {', '.join(sorted(adapters))})"
        )
    executable = (adapter_paths or {}).get(adapter)
    native = adapter in ("lldb-dap", "gdb")
    return LanguageProfile(
        id="ocaml",
        display_name="OCaml",
        adapter=adapters[adapter](executable=executable),
        presentation=Presentation(
            lexer="ocaml",
            parse_error=parse_ocaml_error,
            frame_placeholder="<top>",
            frame_name=demangle_frame_name if native else None,
        ),
        capabilities=ProfileCapabilities(
            # lldb-dap/gdb pause verified for cpp (test_cpp_pause.py);
            # earlybird per probe Q4 (default False until verified True).
            pause_while_running=native,
        ),
    )
```

Then at the bottom of `src/tdb/languages/registry.py`, after the ruby
registration:

```python
from tdb.languages.ocaml import build_ocaml_profile  # noqa: E402

register("ocaml", build_ocaml_profile)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_ocaml_profile.py tests/unit -x -q`
Expected: PASS (full suite catches registry/cli fallout).

- [ ] **Step 5: Smoke the CLI end-to-end (no debugging, just resolution)**

Run:
```bash
uv run python - <<'EOF'
from tdb.languages import registry
p = registry.resolve("ocaml", program=None)
print(p.display_name, p.adapter.id)
EOF
```
Expected: `OCaml lldb-dap`.

- [ ] **Step 6: Commit**

```bash
git add src/tdb/languages/ocaml.py src/tdb/languages/registry.py \
        tests/unit/test_ocaml_profile.py
git commit -m "feat: OCaml language profile with lldb-dap/gdb/earlybird adapters"
```

---

### Task 7: `classify_threads` capability + OCaml classifier

Spec amendment (recorded there): the two per-thread hooks were merged into
one list-in/list-out hook so domain numbering ("Domain 0 (main)",
"Domain 1", ...) can be assigned across the whole thread list.

**Files:**
- Modify: `src/tdb/languages/base.py` (`ThreadDecoration` +
  `ProfileCapabilities.classify_threads`)
- Modify: `src/tdb/languages/ocaml.py` (`classify_ocaml_threads`, wire into
  builder for native adapters)
- Test: `tests/unit/test_ocaml_threads.py`

**Interfaces:**
- Produces: in `tdb.languages.base`:

  ```python
  @dataclass(frozen=True)
  class ThreadDecoration:
      thread: "Thread"
      label: str | None   # display override; None -> adapter's name
      hidden: bool        # runtime service thread: hide by default
  ```

  and on `ProfileCapabilities`:
  `classify_threads: Callable[[list[Thread], dict[int, list[StackFrame]]],
  list[ThreadDecoration]] | None = None` (stacks dict may be missing
  entries — classifier must tolerate absent/empty stacks). Tasks 8 and 9
  consume both.
- Consumes: `Thread`, `StackFrame` from `tdb.dap.types` (existing).

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_ocaml_threads.py`. Frame-name markers below come from the
probe's Q3 findings — if the probe recorded different marker frames,
substitute them in BOTH the fixtures and `_BACKUP_FRAME_MARKERS` /
`_DOMAIN_FRAME_MARKERS`:

```python
from tdb.dap.types import StackFrame, Thread
from tdb.languages.ocaml import classify_ocaml_threads


def _frames(*names):
    return [StackFrame(id=i, name=n) for i, n in enumerate(names)]


def test_domains_numbered_backups_hidden():
    threads = [Thread(1, "prog"), Thread(2, "prog"), Thread(3, "prog"),
               Thread(4, "prog")]
    stacks = {
        1: _frames("camlMain__entry", "caml_start_program", "main"),
        2: _frames("caml_thread_condwait", "backup_thread_func"),
        3: _frames("camlMain__worker_271", "domain_thread_func",
                   "start_thread"),
        4: _frames("caml_thread_condwait", "backup_thread_func"),
    }
    decs = classify_ocaml_threads(threads, stacks)
    assert [d.thread.id for d in decs] == [1, 2, 3, 4]
    assert decs[0].label == "Domain 0 (main)" and not decs[0].hidden
    assert decs[1].hidden
    assert decs[2].label == "Domain 1" and not decs[2].hidden
    assert decs[3].hidden


def test_missing_stack_degrades_to_visible_unlabeled():
    threads = [Thread(1, "prog"), Thread(9, "mystery")]
    decs = classify_ocaml_threads(threads, {})
    assert decs[0].label == "Domain 0 (main)"  # first thread is main
    assert not decs[1].hidden and decs[1].label is None


def test_capability_field_default_none():
    from tdb.languages.base import ProfileCapabilities

    assert ProfileCapabilities().classify_threads is None


def test_native_profile_has_classifier_bytecode_does_not():
    from tdb.languages.ocaml import build_ocaml_profile

    assert build_ocaml_profile(program=None).capabilities.classify_threads \
        is not None
    assert build_ocaml_profile(adapter="ocamlearlybird") \
        .capabilities.classify_threads is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_ocaml_threads.py -v`
Expected: FAIL — `ImportError: classify_ocaml_threads`.

- [ ] **Step 3: Implement**

(a) `src/tdb/languages/base.py` — add near `ErrorFrame` (it needs the
`Thread` type; base.py already imports from `tdb.dap.types`, extend that
import):

```python
@dataclass(frozen=True)
class ThreadDecoration:
    """Display decision for one DAP thread (see classify_threads)."""

    thread: Thread
    label: str | None  # display override; None -> adapter's name
    hidden: bool  # runtime service thread: hidden unless "show all"
```

and in `ProfileCapabilities`:

```python
    # Classify the debuggee's threads for display: label domains, hide
    # runtime service threads (OCaml backup threads). Receives all
    # threads plus per-thread stacks (dict may be missing entries —
    # classify without them). Returns decorations in the same order.
    # None -> every thread shown with the adapter's name.
    classify_threads: (
        Callable[[list[Thread], dict[int, list[StackFrame]]],
                 list[ThreadDecoration]] | None
    ) = None
```

(Add `Thread`, `StackFrame` to base.py's `tdb.dap.types` import — this is
a runtime import there already for `Capabilities`, no cycle: `dap/types`
imports nothing from `languages`.)

(b) `src/tdb/languages/ocaml.py`:

```python
from tdb.dap.types import StackFrame, Thread
from tdb.languages.base import ThreadDecoration

# Marker frames observed under lldb (probe Q3). Substring match on frame
# names; tolerant of symbol prefixes/suffixes across OCaml versions.
_BACKUP_FRAME_MARKERS = ("backup_thread_func", "caml_thread_condwait")
_DOMAIN_FRAME_MARKERS = ("domain_thread_func", "caml_start_program",
                         "caml_domain_spawn")


def _stack_matches(frames: list[StackFrame], markers: tuple[str, ...]) -> bool:
    return any(m in f.name for f in frames for m in markers)


def classify_ocaml_threads(
    threads: list[Thread], stacks: dict[int, list[StackFrame]]
) -> list[ThreadDecoration]:
    """Label domain threads "Domain N" (creation order; the first thread
    is always Domain 0/main) and hide runtime backup threads. A thread
    with no stack info stays visible under the adapter's name."""
    decorations: list[ThreadDecoration] = []
    domain_no = 0
    for i, t in enumerate(threads):
        frames = stacks.get(t.id, [])
        if i == 0:
            decorations.append(ThreadDecoration(t, "Domain 0 (main)", False))
            domain_no = 1
            continue
        if frames and _stack_matches(frames, _BACKUP_FRAME_MARKERS) \
                and not _stack_matches(frames, _DOMAIN_FRAME_MARKERS):
            decorations.append(ThreadDecoration(t, None, True))
            continue
        if frames and _stack_matches(frames, _DOMAIN_FRAME_MARKERS):
            decorations.append(ThreadDecoration(t, f"Domain {domain_no}", False))
            domain_no += 1
            continue
        decorations.append(ThreadDecoration(t, None, False))
    return decorations
```

(c) In `build_ocaml_profile`'s `ProfileCapabilities(...)` add:

```python
            classify_threads=classify_ocaml_threads if native else None,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_ocaml_threads.py tests/unit -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/languages/base.py src/tdb/languages/ocaml.py \
        tests/unit/test_ocaml_threads.py
git commit -m "feat: classify_threads capability + OCaml domain/backup classifier"
```

---

### Task 8: Controller — prefer a visible thread on stop

**Files:**
- Modify: `src/tdb/session/controller.py` (`fetch_stop_info`, ~line 905)
- Test: `tests/unit/test_controller_visible_thread.py`

**Interfaces:**
- Consumes: `ProfileCapabilities.classify_threads`, `ThreadDecoration`
  (Task 7).
- Produces: `DebugController._prefer_visible_thread()` — after a stop, if
  the classifier marks the current thread hidden, re-point
  `state.current_thread_id` at the first visible thread before
  stack/scope fetching.

- [ ] **Step 1: Read the existing pattern**

Read `src/tdb/session/controller.py:905-935` (`fetch_stop_info`) and
`tests/unit/test_controller_opaque_frames.py` (the mock-client pattern
for controller tests). Reuse that test file's fake-client scaffolding
verbatim — same constructor calls, same async plumbing.

- [ ] **Step 2: Write the failing test**

`tests/unit/test_controller_visible_thread.py`:

```python
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
```

(If `_make`/`_FakeDAP`/`enter_stop` spellings drift from
`tests/unit/test_controller_actions.py`, that file is authoritative —
adjust the test, not the scaffolding. Check the async test convention the
suite uses — anyio/asyncio marker — and match neighboring controller
tests.)

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_controller_visible_thread.py -v`
Expected: FAIL — `current_thread_id` stays 2.

- [ ] **Step 4: Implement in `fetch_stop_info`**

In `src/tdb/session/controller.py`, inside `fetch_stop_info` after
`self.state.threads = await ac.threads()` succeeds and before the
current-thread stack fetch, insert:

```python
        await self._prefer_visible_thread(ac)
```

and add the helper next to `_initial_frame`:

```python
    async def _prefer_visible_thread(self, ac) -> None:
        """If the profile classifies the stopped thread as a hidden
        runtime thread (OCaml backup threads), re-point
        state.current_thread_id at the first visible thread. Best-effort:
        any DAP failure leaves the selection unchanged."""
        classify = self.profile.capabilities.classify_threads
        if classify is None or self.state.current_thread_id is None:
            return
        threads = self.state.threads
        if not threads:
            return
        try:
            stacks = {}
            for t in threads:
                stacks[t.id] = await ac.stack_trace(t.id)
            decorations = classify(threads, stacks)
            by_id = {d.thread.id: d for d in decorations}
            current = by_id.get(self.state.current_thread_id)
            if current is None or not current.hidden:
                return
            for d in decorations:
                if not d.hidden:
                    self.state.current_thread_id = d.thread.id
                    return
        except Exception:
            log.exception("visible-thread preference failed; keeping stop thread")
```

(All-stop means every thread is stopped, so fetching each stack is legal;
thread counts are small — a few domains plus their backups.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_controller_visible_thread.py tests/unit -x -q`
Expected: PASS, including all pre-existing controller tests (the helper
no-ops when `classify_threads is None`, i.e. for every other language).

- [ ] **Step 6: Commit**

```bash
git add src/tdb/session/controller.py tests/unit/test_controller_visible_thread.py
git commit -m "feat: stop selection prefers visible threads over OCaml backup threads"
```

---

### Task 9: ThreadsModal — labels, hiding, show-all toggle

**Files:**
- Modify: `src/tdb/widgets/threads_modal.py`
- Modify: `src/tdb/session/inspect_service.py` (add `thread_frames`)
- Modify: `src/tdb/app_handlers/inspection.py` (`show_threads_modal`,
  `refresh_threads`)
- Test: `tests/unit/test_threads_modal_decorations.py`

**Interfaces:**
- Consumes: `ThreadDecoration`, `classify_threads` (Task 7);
  `frame_name` kwarg (Task 4).
- Produces: `ThreadsModal.__init__(threads, current_thread_id=None,
  frame_name=None, decorations: list[ThreadDecoration] | None = None)`;
  pure helper `visible_threads(decorations, show_all) ->
  list[ThreadDecoration]` (module-level in `threads_modal.py`);
  `InspectService.thread_frames(thread_id: int) -> list[StackFrame]`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_threads_modal_decorations.py` (pure-logic tests; modal
rendering is exercised by the integration task):

```python
from tdb.dap.types import Thread
from tdb.languages.base import ThreadDecoration
from tdb.widgets.threads_modal import visible_threads


def _decs():
    return [
        ThreadDecoration(Thread(1, "prog"), "Domain 0 (main)", False),
        ThreadDecoration(Thread(2, "prog"), None, True),
        ThreadDecoration(Thread(3, "prog"), "Domain 1", False),
    ]


def test_hidden_filtered_by_default():
    vis = visible_threads(_decs(), show_all=False)
    assert [d.thread.id for d in vis] == [1, 3]


def test_show_all_reveals_everything():
    vis = visible_threads(_decs(), show_all=True)
    assert [d.thread.id for d in vis] == [1, 2, 3]


def test_none_decorations_means_no_filtering():
    assert visible_threads(None, show_all=False) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_threads_modal_decorations.py -v`
Expected: FAIL — `ImportError: visible_threads`.

- [ ] **Step 3: Implement the modal changes**

`src/tdb/widgets/threads_modal.py`:

(a) Module-level helper:

```python
def visible_threads(
    decorations: "list[ThreadDecoration] | None", show_all: bool
) -> "list[ThreadDecoration] | None":
    """Decorations to display (None -> language has no classifier)."""
    if decorations is None:
        return None
    if show_all:
        return list(decorations)
    return [d for d in decorations if not d.hidden]
```

(b) `__init__` gains `decorations: list[ThreadDecoration] | None = None`
after `frame_name` (import `ThreadDecoration` under TYPE_CHECKING). Store
`self._decorations = decorations`, `self._show_all = False`, and derive
items:

```python
        self._all_threads: list[Thread] = threads
        self._apply_visibility()

    def _apply_visibility(self) -> None:
        vis = visible_threads(self._decorations, self._show_all)
        if vis is None:
            self._items = list(self._all_threads)
            self._labels = {}
        else:
            self._items = [d.thread for d in vis]
            self._labels = {d.thread.id: d.label for d in vis if d.label}
```

(replacing the plain `self._items = threads` assignment; `update_threads`
similarly sets `self._all_threads = threads`, re-derives decorations —
see (e) — then calls `self._apply_visibility()` before
`self._reload_after_items_change()`).

(c) `_format_row` uses the label:

```python
        name = Text(self._labels.get(thread.id, thread.name))
```

Same substitution in `_render_loading_detail` and `show_thread_detail`
("Name:" line).

(d) Add the toggle — extend the class BINDINGS (the base
`_InspectableListModal` defines BINDINGS; ADD, don't replace: copy the
base list into ThreadsModal and append):

```python
    BINDINGS = _InspectableListModal.BINDINGS + [
        ("a", "toggle_all", "All threads"),
    ]

    def action_toggle_all(self) -> None:
        if self._decorations is None:
            return
        self._show_all = not self._show_all
        self._apply_visibility()
        self._reload_after_items_change()
```

Update `FOOTER_HINT` to `"ESC close  |  r refresh  |  a all threads  |  "
"Enter/double-click jump to thread"`.

(e) `update_threads` gains a `decorations=None` parameter mirroring the
constructor and stores it before `_apply_visibility()`.

(f) `src/tdb/session/inspect_service.py` — add beside `thread_stack`
(reuse its `_gate()` idiom):

```python
    async def thread_frames(self, thread_id: int) -> list[StackFrame]:
        """A thread's stack only — no scopes/variables (cheap, for
        thread classification)."""
        self._gate()
        return await self._ctrl.client.stack_trace(thread_id)
```

(g) `src/tdb/app_handlers/inspection.py` — in `show_threads_modal`, after
`threads` is fetched, classify when the profile supports it:

```python
        decorations = None
        classify = ctrl.profile.capabilities.classify_threads
        if classify is not None:
            stacks = {}
            for t in threads:
                try:
                    stacks[t.id] = await self._svc.thread_frames(t.id)
                except Exception:
                    log.debug("stack fetch for thread %d failed", t.id)
            decorations = classify(threads, stacks)
        modal = ThreadsModal(
            threads,
            ctrl.state.current_thread_id,
            frame_name=ctrl.profile.presentation.frame_name,
            decorations=decorations,
        )
```

and in `refresh_threads`, build decorations the same way (factor the
classify block into a small local helper `_classify(threads)` used by
both) and pass them to `update_threads(threads,
ctrl.state.current_thread_id, decorations=decorations)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_threads_modal_decorations.py tests/unit -x -q`
Expected: PASS — full suite guards existing ThreadsModal construction
sites (python's threads flow passes no decorations and must behave
exactly as before).

- [ ] **Step 5: Commit**

```bash
git add src/tdb/widgets/threads_modal.py src/tdb/session/inspect_service.py \
        src/tdb/app_handlers/inspection.py \
        tests/unit/test_threads_modal_decorations.py
git commit -m "feat: ThreadsModal domain labels, backup-thread hiding, show-all toggle"
```

---

### Task 10: Integration tests

Requires the OCaml toolchain + adapters locally (Task 1 installed them).
Every test module skip-gates so the suite passes on machines without them.

**Files:**
- Create: `tests/integration/test_ocaml_native_session.py`
- Create: `tests/integration/test_ocaml_earlybird_session.py`
- Test fixtures: reuse `tests/integration/fixtures/ocaml_domains.ml`,
  `ocaml_fatal.ml` (Task 1)

**Interfaces:**
- Consumes: everything shipped in Tasks 2–9;
  `build_ocaml_profile` (Task 6). Harness idioms come from
  `tests/integration/test_cpp_pause.py` /
  `tests/integration/test_cpp_session.py` — read both before writing.

- [ ] **Step 1: Write the native-session tests**

`tests/integration/test_ocaml_native_session.py` — module docstring, skip
guards, and a module fixture that compiles the fixtures with
`ocamlopt -g` into `tmp_path_factory` (never into the repo). Skip guards:

```python
_HAVE_OCAMLOPT = shutil.which("ocamlopt") is not None
_HAVE_LLDB_DAP = shutil.which("lldb-dap") is not None

pytestmark = pytest.mark.skipif(
    not (_HAVE_OCAMLOPT and _HAVE_LLDB_DAP),
    reason="ocamlopt and lldb-dap are required",
)
```

Tests (each built on the cpp-session harness idioms — controller +
`ServerEventHandler`, generous `WAIT = 30.0`):

1. `test_domains_visible_at_breakpoint` — THE multicore headline.
   Launch `ocaml_domains.exe`, set a breakpoint on the `Atomic.incr`
   line (line 6), wait for the stop, then:
   - `threads = await controller.client.threads()` has ≥ 4 entries
     (main + 3 domains; backups make it more).
   - `classify_ocaml_threads(threads, stacks)` (fetch each stack first)
     yields ≥ 2 visible decorations labeled `Domain 0 (main)`,
     `Domain 1`, ... and ≥ 1 hidden decoration.
   - The stopped thread's top demangled frame is `Main.worker` (apply
     `demangle_frame_name`; module name for a file `ocaml_domains.ml`
     compiled directly is `Ocaml_domains` — assert
     `.endswith(".worker")` to stay layout-agnostic).
   - A second visible domain's stack also contains a `.worker` frame
     (breakpoint reachable from multiple domains; if scheduling makes
     this flaky, relax to: every visible non-main domain has a non-empty
     stack).
2. `test_step_and_continue_at_domain_breakpoint` — from the stop: `next`
   twice (stops stay in the same source file), then remove the
   breakpoint, `continue`, and expect a `terminated`/`exited` event with
   the program's `sum=6` line in captured output.
3. `test_pause_while_running` — launch with no breakpoints,
   `stop_on_entry=False`, then `controller.pause()` after 0.5 s; expect a
   stop within 10 s (mirrors `test_cpp_pause.py` structure).
4. `test_uncaught_exception_stops_or_parses` — launch `ocaml_fatal.exe`
   (compiled from `ocaml_fatal.ml` with `ocamlopt -g`). If probe Q5
   confirmed the `caml_fatal_uncaught_exception` breakpoint: expect a
   stopped event before termination and ≥ 1 thread with a
   `caml_fatal_uncaught_exception` frame; then continue to termination.
   Either way, after termination assert
   `parse_ocaml_error(captured_stderr, exit_code)` returns a
   `ParsedError` whose `message` contains `Failure("boom")` (native
   `Printexc` output requires `OCAMLRUNPARAM=b`, which the adapter
   injects — this also verifies the injection end-to-end).
5. `test_variables_report_what_dwarf_offers` — at the domain breakpoint,
   fetch scopes/variables for the top frame and assert the request
   *succeeds* (list may be empty — record its actual contents with a
   `print` for the docs task; do NOT assert specific locals, per the
   spec's DWARF-locals risk).

- [ ] **Step 2: Write the earlybird-session tests**

`tests/integration/test_ocaml_earlybird_session.py` — skip guard on
`shutil.which("ocamlc")` + `shutil.which("ocamlearlybird")`. Compile
`ocaml_fatal.ml` and a small locals fixture (write inline in the test
module, compiled to bytecode in the module fixture):

```ocaml
let add x y =
  let total = x + y in
  let msg = Printf.sprintf "total=%d" total in
  print_endline msg;      (* breakpoint line *)
  total
let () = ignore (add 2 3)
```

Tests, using `build_ocaml_profile(adapter="ocamlearlybird")` through the
same controller harness:

1. `test_launch_breakpoint_locals` — breakpoint on the `print_endline`
   line; at the stop, scopes/variables contain `total` = `5` and `msg`
   containing `total=5` (earlybird's rich locals — assert them for real,
   unlike the native test).
2. `test_evaluate_in_scope` — DAP `evaluate` of `total + 1` in the
   stopped frame returns `6` (adjust the expression syntax to probe Q1
   findings if earlybird wants something different).
3. `test_fatal_error_parses` — run `ocaml_fatal.byte` to termination;
   `parse_ocaml_error` on captured stderr yields frames naming `boom`
   and `middle`.

- [ ] **Step 3: Run the integration tests**

Run: `uv run pytest tests/integration/test_ocaml_native_session.py \
tests/integration/test_ocaml_earlybird_session.py -v`
Expected: all PASS locally (with toolchain present). Fix real product
bugs they surface — this is the first end-to-end contact; expect
adapter-behavior surprises, and consult the spec's probe findings before
changing product code.

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest tests -x -q`
Expected: PASS (integration modules for absent toolchains skip).

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_ocaml_native_session.py \
        tests/integration/test_ocaml_earlybird_session.py
git commit -m "test: OCaml integration coverage — domains, stepping, pause, errors, locals"
```

---

### Task 11: Lifecycle / exit-path audit (CLAUDE.md requirement)

**Files:**
- Create: `docs/superpowers/plans/2026-08-22-ocaml-exit-path-audit.md`
  (filled-in checklist, committed as the audit record)
- Modify: whatever the audit flags (fix root causes, not symptoms)

**Interfaces:**
- Consumes: the complete feature (Tasks 2–10).

- [ ] **Step 1: Run the manual audit**

With `ocaml_domains.exe` (compile a scratch copy) and the real TUI
(`uv run tdb ./ocaml_domains.exe`), walk EVERY exit/lifecycle path and
record PASS/FAIL per row in the audit file:

| Path | Action | Expected |
|---|---|---|
| quit key | `q` while stopped at a domain breakpoint | clean exit, no orphan lldb-dap or debuggee (`pgrep -f lldb-dap; pgrep -f ocaml_domains` empty) |
| quit while running | `q` while continuing | same |
| Ctrl-C | interrupt tdb | same |
| restart | `R` at a breakpoint | fresh session stops again; thread labels still correct |
| ESC modals | open Threads modal (incl. `a` toggle), ESC | modal closes, main views intact |
| menu quit | File menu → Quit | clean exit |
| `--run` | `tdb --run ./ocaml_domains.exe`, then signal | pause lands on a VISIBLE thread (Task 8) |
| `--terminal` | `tdb --terminal ./ocaml_domains.exe` | debuggee I/O in external terminal (lldb-dap runInTerminal path) |
| natural exit | let program run to completion | exit code + `sum=6` in console, no hang |
| fatal exit | `tdb ./ocaml_fatal.exe`, continue past the stop | error modal shows parsed backtrace |
| no debug info | breakpoint in a binary built WITHOUT `-g` | the existing unbound-breakpoint console warning (controller.py `_emit_unbound_warning`) appears once, mentioning `-g` |
| bytecode pass | repeat quit/Ctrl-C/restart/natural/fatal rows with `--adapter ocamlearlybird` on a bytecode build | same expectations |

- [ ] **Step 2: Fix what fails**

For each FAIL: root-cause it (superpowers:systematic-debugging), fix,
re-run the affected row AND the full test suite, and note the fix in the
audit file. Audit ALL related code paths for the same issue per
CLAUDE.md.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/plans/2026-08-22-ocaml-exit-path-audit.md
git commit -m "audit: OCaml exit-path/lifecycle walkthrough (with fixes)"
```
(Fixes found in Step 2 get their own commits with their own tests.)

---

### Task 12: Documentation + CI

**Files:**
- Modify: `README.md` (language table + "Debugging OCaml" section)
- Modify: `Dockerfile` (OCaml toolchain for integration tests)

**Interfaces:**
- Consumes: the shipped feature; the Task 10 Step 1.5 printout of actual
  native-locals visibility (for honest docs).

- [ ] **Step 1: README**

Add an OCaml row to the language support table (match the existing
table's columns exactly), and a "Debugging OCaml" section covering:
- the two flavors: native (`ocamlopt`/dune default) → lldb-dap, domains
  as threads, `t` opens the Threads modal with `Domain N` labels and an
  `a` show-all toggle; bytecode (`ocamlc -g`) → ocamlearlybird, rich
  locals + OCaml `evaluate`, single-domain only
- build requirements: `-g` (dune's dev profile has it); pass the BUILT
  EXECUTABLE to tdb, never `.ml`
- what the Variables view shows in native mode (write EXACTLY what
  Task 10's test 5 printed — no aspirational claims)
- the Evaluate console per flavor: bytecode evaluates OCaml expressions
  in scope; native evaluates lldb/C-level expressions (runtime
  spelunking, not OCaml)
- `--adapter ocamlearlybird|lldb-dap|gdb`, config
  `{"adapters": {"ocamlearlybird": ...}}`
- limitations: no Windows, no remote attach, stripped binaries need
  `--lang ocaml`, `Unix.fork`/eio fibers out of scope

- [ ] **Step 2: Dockerfile**

Extend the existing image (read the Dockerfile first; keep its structure)
with the OCaml toolchain. On the Debian-based image:

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
        ocaml opam lldb \
    && rm -rf /var/lib/apt/lists/*
RUN opam init --disable-sandboxing -y \
    && opam install -y earlybird \
    && ln -s "$(opam var bin)/ocamlearlybird" /usr/local/bin/ocamlearlybird
```

If the distro's `ocaml` package is < 5.0 (check `ocaml -version` in the
build), install the compiler through opam instead
(`opam switch create 5.2.0`) and symlink `ocamlopt`/`ocamlc` the same
way. If the base image is Alpine and opam fights musl (see the Alpine CI
history in `docs/`), scope the OCaml layer to the Debian image only and
note it in the Dockerfile comment — do not block the feature on Alpine.

- [ ] **Step 3: Verify the container runs the OCaml tests**

Run (match the repo's documented docker test invocation — check README/CI
config for the exact command):
`docker build -t tdb-test . && docker run --rm tdb-test uv run pytest tests/integration -k ocaml -v`
Expected: OCaml tests PASS inside the container (not skipped — if they
skip, a tool is missing from the image; fix the Dockerfile).

- [ ] **Step 4: Full-suite final check and commit**

Run: `uv run pytest tests -q`
Expected: PASS.

```bash
git add README.md Dockerfile
git commit -m "docs+ci: OCaml debugging documentation and container toolchain"
```

---

## Completion

After Task 12: run the superpowers:requesting-code-review skill against
the branch, then superpowers:finishing-a-development-branch. Update the
memory file `project_ocaml_support.md` with final state (branch head,
merged-or-not, probe findings worth remembering).

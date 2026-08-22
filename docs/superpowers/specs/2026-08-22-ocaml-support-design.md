# OCaml Debugging Support (Multicore-First) — Design

**Date:** 2026-08-22
**Status:** Approved design, pre-implementation
**Branch:** `add-ocaml-support` (to be based on `main` @ 4e518fa)
**Goal:** Debug OCaml programs in tdb with first-class OCaml 5 multicore
support: domains presented the way Python threads are (Threads modal,
per-domain stacks and locals, breakpoints hittable from any domain,
all-stop), via a native-code adapter — plus a bytecode adapter for rich
single-domain inspection.

## User decisions (2026-08-22)

- **Adapter route:** both adapters, multicore-first. Native (`lldb-dap`)
  is the headline route because it is the only one that can see domains;
  bytecode (`ocamlearlybird`) ships alongside for rich OCaml-level
  inspection of single-domain programs.
- **Invocation:** executable path only. `tdb ./_build/default/bin/main.exe`
  — the user builds with dune themselves; tdb never invokes dune.
  `tdb main.ml` is an actionable error.
- **Inspection depth (native):** basic decoding in v1 — untag ints, decode
  strings/floats/booleans, show structured blocks as
  `block(tag=N, size=M)` with expandable fields. No list/variant-name
  reconstruction in v1.
- **Parallelism scope:** domains-as-threads only. `Unix.fork` children and
  eio/effect fiber awareness are explicitly out of scope for v1.
- **Platforms:** Linux + macOS. On Windows the profile builder raises
  `LanguageNotSupportedError("OCaml debugging is not supported on Windows
  yet")`.
- **Integration approach:** Approach A — direct profiles over stock stdio
  DAP adapters (no bundled proxy shim). Escalation trigger: if Task 0
  finds a *protocol-level* misbehavior in lldb-dap (not a cosmetic quirk),
  that adapter escalates to the perl/ruby proxy-shim pattern without
  redesigning the rest.

## Ecosystem facts the design rests on

- `ocamlearlybird` is the only OCaml-native DAP adapter (used by the
  VS Code OCaml Platform). It drives the **bytecode** runtime's debug
  protocol — the same machinery as `ocamldebug`, which does not work on
  native code and does not support multi-domain programs. Earlybird alone
  cannot deliver the multicore story.
- OCaml 5 domains are OS threads. `lldb-dap` (LLVM >= 17, already tdb's
  alternate cpp adapter) reports them through DAP `threads`, stops all of
  them together on any stop, and hits breakpoints from any domain. This is
  the Python-threads-parity baseline, for free.
- Stock OCaml native `-g` DWARF has good **line/function** info but
  historically thin **local-variable** info. Named locals in native mode
  are the at-risk feature (see Risks); bytecode covers rich locals.
- Native OCaml frame names arrive mangled (`camlMain__worker_271`).
- With `OCAMLRUNPARAM=b`, an uncaught exception prints
  `Fatal error: exception ...` plus `Raised at`/`Called from` backtrace
  lines — parseable for the error modal, identical text for native and
  bytecode.

## Architecture

One new profile module `src/tdb/languages/ocaml.py` registering `ocaml`,
with two `AdapterSpec`s and no proxy process:

- **`OCamlLldbAdapter`** (native, default) — subclasses/reuses the
  existing `LldbDapAdapter` from `languages/cpp.py`. Additions:
  - `initCommands`: `command script import <abs path>` for the OCaml
    formatter script (path resolved via `importlib.resources` at
    launch-body build time), plus one function breakpoint on
    `caml_fatal_uncaught_exception`.
  - Debuggee env: inject `OCAMLRUNPARAM=b`, merged with (never
    clobbering) any user-supplied `OCAMLRUNPARAM`.
- **`EarlybirdAdapter`** (bytecode) — spawns `ocamlearlybird debug` over
  stdio (a well-behaved stdio DAP adapter, like debugpy).
  `AdapterNotFoundError` hint: `opam install earlybird`. Config override
  `{"adapters": {"ocamlearlybird": "/path/..."}}`, same shape as
  perl/ruby. Exact launch-body field names are confirmed by Task 0.
- **`gdb -i dap`** remains selectable via `--adapter gdb` as the Linux
  fallback (same trio pattern as cpp); it inherits the cpp GdbDapAdapter
  behavior with the OCaml env/error additions.

The profile builder picks the adapter from the detected flavor
(native → lldb-dap, bytecode → ocamlearlybird);
`--adapter ocamlearlybird|lldb-dap|gdb` overrides.

## Detection (registry.detect, first hit wins)

1. `.ml`/`.mli` extension → actionable error mirroring the existing
   compiled-source-extension error: "build with dune (dev profile, which
   keeps debug info) and run `tdb ./_build/default/.../main.exe`, or pass
   --lang explicitly".
2. Bytecode: shebang mentioning `ocamlrun`, or the OCaml bytecode trailer
   magic (`Caml1999X...` in the file's tail) → `ocaml` (earlybird).
3. Native: the existing ELF/Mach-O magic check currently returns `cpp`;
   before returning cpp, scan the binary for an OCaml runtime marker
   (`caml_program` / `caml_startup` strings) → `ocaml` (lldb-dap), else
   `cpp` as today. A stripped binary may miss detection and land in cpp;
   `--lang ocaml` overrides (documented).

`extensions_for("ocaml")` stays empty (like cpp): File → Open shows all
files, since the debug target is a binary but Code View opens `.ml`
sources.

## Domains as threads

**Free from existing machinery:** ThreadsModal (list, per-thread stack +
variables, jump-to-thread), breakpoints from any domain, all-stop,
`state.threads` refresh after stop.

**New `ProfileCapabilities` hook** (data/callable, consumers gate with
`is not None`, same pattern as `opaque_frame`).

*Amended at planning time (2026-08-22):* the originally sketched pair of
per-thread hooks (`thread_label`/`hide_thread`) is consolidated into ONE
list-in/list-out hook, because numbering domains ("Domain 0 (main)",
"Domain 1", ...) needs the whole thread list at once:

- `classify_threads: Callable[[list[Thread], dict[int, list[StackFrame]]],
  list[ThreadDecoration]]` — receives all threads plus per-thread stacks
  (the dict may be missing entries) and returns, in the same order,
  `ThreadDecoration(thread, label: str | None, hidden: bool)`.
  Labels: "Domain N" in creation order, first thread = "Domain 0 (main)".
  Hidden: runtime service threads (backup threads parked in a
  recognizable `sigwait`/`backup_thread_func` frame; tick thread) —
  dropped from ThreadsModal and never auto-selected on stop; a modal
  toggle (`a` = show all) reveals them. Exact frame/name signals
  confirmed by Task 0.

Consumers: ThreadsModal (labels + filter + toggle) and the controller's
stopped-thread selection (a stop landing on a hidden thread prefers the
nearest visible one, same spirit as `opaque_frame`). The hook tolerates
missing stacks: such threads stay visible under the raw adapter name —
the feature degrades to "all threads shown, adapter names", never breaks.

**Frame-name demangling.** New `Presentation` callable
`frame_name: Callable[[str], str] | None`, applied by
StackView/ThreadsModal rendering: regex-demangle
`camlModule__name_NNN` → `Module.name`; runtime C frames pass through
unchanged. Earlybird reports proper OCaml names, so the bytecode profile
leaves it None.

**Other capability gates:** `compute_step_units=None`,
`task_inspection=False`, `child_process_strategy=None`.
`pause_while_running`: True for lldb-dap/gdb (verified for cpp), False
for earlybird pending Task 0 — so `tdb --run` works on the native route.

## Variable inspection

**Native (lldb formatters).** New file
`src/tdb/adapters/ocaml/lldb_formatters.py`, imported into *lldb's*
Python via initCommands. Registers summary/synthetic providers for OCaml
`value`-typed variables:

- odd word → immediate: show the untagged int (plus the raw word, since
  DWARF cannot distinguish int/bool/char/constructor)
- even word → heap pointer: read the block header, decode by tag:
  `String_tag` → bytes, `Double_tag` → float, `Double_array_tag` → float
  array, `Custom_tag` → "custom block", closure tags → "fun", otherwise
  `block(tag=N, size=M)` with fields expandable, decoding recursively
  under depth/width caps.

Decode logic lives in pure functions (word + `read_memory` callable in,
description tree out); the lldb API is confined to a thin glue layer, so
decoding is unit-testable in plain pytest without lldb.

**Caveat:** the Variables view shows whatever lldb finds in DWARF for the
frame. If stock OCaml exposes few named locals, the view may show only
globals/arguments per frame; formatters make what is visible legible but
cannot invent variables DWARF doesn't describe. Task 0 measures reality;
the result is recorded in the docs, and rich-locals users are pointed at
the bytecode adapter.

**Bytecode.** Earlybird reports real OCaml locals with proper names and
structured values through standard `scopes`/`variables` — the existing
Variables view just works. No formatter work.

**Evaluate console.** Native: lldb expressions are C/machine-level
(documented as runtime spelunking, not OCaml evaluation). Bytecode:
earlybird supports DAP `evaluate` for OCaml expressions in scope.

## Fatal errors and lifecycle

**parse_ocaml_error** in `languages/errors.py`, registered as
`Presentation.parse_error` for both adapters (identical text either way):

- header: the `Fatal error: exception ...` line; message: the exception
  constructor + argument
- frames: from `Raised at` / `Called from` / `Re-raised at` lines,
  outermost-first per the existing convention; `detail`: verbatim text
- tolerates missing backtrace lines (program built without `-g`): modal
  shows header + message, no frames, plus a hint "compile with -g
  (dune's dev profile) for backtraces"
- `frame_placeholder`: `"<top>"`

Exceptions on spawned domains print the same form; the parser doesn't
care which domain died.

**Exception breakpoints.** `pick_exception_filters`: adapter defaults for
earlybird, empty for lldb-dap — but the lldb initCommands function
breakpoint on `caml_fatal_uncaught_exception` makes an uncaught exception
*stop in the debugger* with all domains inspectable before the process
dies.

**Failure hints:**
- lldb-dap/gdb missing → existing cpp `AdapterNotFoundError` hints
- ocamlearlybird missing → "opam install earlybird"
- no debug info → breakpoints never bind; on first breakpoint rejection,
  one console warning: "no debug info — build with dune's dev profile"

**Lifecycle.** stopOnEntry, restart (R), all quit paths, Ctrl-C, and
`--terminal` ride the existing cpp/lldb-dap code paths. Per CLAUDE.md,
the implementation plan includes an explicit audit task walking every
exit path with the OCaml profile rather than assuming cpp parity
transfers. Remote attach: both adapters raise the existing "not supported
yet" error.

## Testing

**Task 0 — validation harness (first, before any UI work).** A raw-DAP
script (the perl/ruby/cpp pattern) drives both adapters against
`test_targets/ocaml/domains.ml` (spawns 3 domains doing recognizable
work) and records:

1. exact earlybird launch-body field names + `debug` subcommand behavior
2. what DWARF locals lldb actually sees per frame (the inspection risk)
3. how domain and backup threads are named/identified (the hooks' signal)
4. whether earlybird honors `pause` while running
5. whether `caml_fatal_uncaught_exception` is a valid breakpoint target
   across OCaml 5.x versions

Findings are written back into this spec before dependent tasks run.
Protocol-level lldb-dap misbehavior triggers the proxy-shim escalation.

**Unit tests (no toolchain needed):** detection (trailer, shebang,
ELF+marker, plain ELF stays cpp, `.ml` error); `launch_body` for both
adapters (initCommands, `OCAMLRUNPARAM=b` merge, env list-vs-mapping);
`parse_ocaml_error` variants; demangling; formatter decode functions with
a fake `read_memory`; `classify_threads` against canned fixtures.

**Integration tests** (`tests/integration/test_ocaml_*.py`, skip-gated on
`ocamlfind`/`dune` + adapter availability, same pattern as
`test_cpp_pause.py`): fixtures compiled at test time; per adapter
launch → breakpoint → step → locals → quit; the multicore headline test
(breakpoint inside a `Domain.spawn` body hit, all domains visible,
per-domain stacks); pause-while-running; uncaught-exception stop + error
modal parse.

**CI:** extend the Dockerfile with opam/dune/earlybird/lldb — mindful of
the Alpine/musl history; if opam-on-Alpine fights back, the OCaml
integration job runs on the Debian image only rather than blocking.

## Documentation

README language-table row plus a "Debugging OCaml" section: the two
flavors and when to use each, dune `-g`/dev-profile requirement, the
native-locals caveat (as measured by Task 0), Windows unsupported.

## Risks / open implementation questions

1. **Native DWARF locals** (the big one): stock OCaml may expose few
   named locals to lldb. Mitigations: Task 0 measures before UI work;
   docs set expectations; bytecode adapter covers rich locals; formatters
   still make globals/args legible.
2. **Earlybird OCaml 5.x compatibility**: earlybird's support for recent
   OCaml 5 releases must be pinned by Task 0; the profile should error
   helpfully on an incompatible pairing.
3. **Thread-identification signal**: if neither stacks nor OS thread
   names reliably distinguish domains from backup threads, the fallback
   is "show all threads with adapter names" — functional, just noisier.
4. **`caml_fatal_uncaught_exception` stability**: symbol name could vary
   across OCaml versions; Task 0 checks; feature degrades to
   parse-on-exit error modal if absent.
5. **Detection marker scan cost**: scanning a large binary for
   `caml_program` should read bounded chunks (e.g. first + last few MB)
   rather than the whole file.

## Probe-verified facts (2026-08-22, ocaml 5.4.0 / earlybird 1.3.6 / lldb 21.1.8)

Ran via `tests/integration/ocaml_probe.py` against
`tests/integration/fixtures/ocaml_domains.ml` (native, lldb-dap) and
`tests/integration/fixtures/ocaml_fatal.ml` (bytecode, earlybird), plus a
few out-of-band checks noted inline. Toolchain: opam 2.5.0 bare-initialized
(`opam init --disable-sandboxing -y --bare`), switch
`opam switch create default --packages=ocaml-system` (reuses the system
5.4.0 compiler — no separate compiler build needed), then
`opam install -y earlybird`, which installed cleanly with no version
pinning or `--best-effort` required — **earlybird 1.3.6 supports OCaml
5.4.0 out of the box** (risk 2 is not a blocker on this toolchain
combination).

**Q1 — earlybird invocation + launch-body fields.** *Attribution: the
committed `tests/integration/ocaml_probe.py` deadlocks on `initialize`
against this earlybird build (see the Critical caveat below) and cannot
reproduce any of the facts in this paragraph. Every fact below was
observed out-of-band, not via the committed script: the `initialize`
capabilities and the full `launch`/`configurationDone`/`stopped`/
`threads` sequence came from an ad-hoc, uncommitted **Node.js**
`child_process.spawn` DAP session (interactive, stdin held open across
the whole exchange); a couple of individual responses (e.g. the raw
`initialize` capabilities body) were cross-checked via ad-hoc shell runs
that batch all requests into stdin up front and close it (`< file` / a
FIFO), which also get correct responses. Neither of those scripts is
checked in — treat this paragraph as validated protocol knowledge, not as
something the committed probe will reproduce if you run it.* `ocamlearlybird
debug` is the correct stdio-DAP subcommand (siblings: `serve`, a
socket-listening mode not used here). `initialize` response capabilities:
`supportsConfigurationDoneRequest`, `supportsValueFormattingOptions`,
`supportsDelayedStackTraceLoading`, `supportsLoadedSourcesRequest`,
`supportsTerminateRequest`, `supportsBreakpointLocationsRequest` — all
`true`, nothing else advertised (no `supportsEvaluateForHovers` etc. —
keep the evaluate-console capability check honest). The launch body the
plan assumed works **verbatim, no field-name corrections needed**:
```json
{
  "program": "<abs path to .byte>",
  "arguments": [],
  "cwd": "<abs path>",
  "stopOnEntry": true,
  "console": "internalConsole"
}
```
`launch` responds `{"success": true}` with an empty body; with
`stopOnEntry: true` a `stopped` event follows with
`{"reason": "entry", "threadId": 0}`. `threads` on the bytecode session
returns exactly one thread: `{"id": 0, "name": "main"}` (no domain
concept — bytecode is single-domain by construction, so this is expected,
not a bug).

**Critical caveat — Approach A is currently NOT viable for earlybird as
spawned by tdb.** `ocamlearlybird debug` deadlocks indefinitely (tested to
30s+, not just slow) when spawned via **Python's subprocess machinery** —
both plain `subprocess.Popen` (blocking reads, `os.write`/`.flush()`) and
`asyncio.create_subprocess_exec` with `start_new_session=True` (the exact
pattern `tdb.dap.client.DAPClient.start()` uses in production) — over a
plain pipe *and* over a pty. `strace -f` on the Python-spawned case shows
earlybird's internal reader thread genuinely `read()`s the full
`initialize` request bytes off fd 0 (`read(0, "Content-Length: 110\r\n\r\n{"...", 4096) = 133`),
signals its main thread via an `eventfd`, and then... nothing — no
`Message recv`/`state_initialized` log line, no response, ever. The exact
same byte sequence over the exact same kind of unnamed pipe **succeeds
instantly** when the parent is a shell (`< file`, a FIFO held open, or
`< <(...)` process substitution) or **Node.js** `child_process.spawn`
(confirmed working, full session incl. `launch`/`configurationDone`/
`stopped`/`threads`/`continue` all round-tripped normally — see Q4). Ruled
out as the cause: stdin EOF (FIFO-held-open case still works), "just
needs more time" (30s+ waits from Python never respond), pipe-vs-pty
(both hang from Python).

**Root cause (found by a follow-up investigation, after this report was
first written).** earlybird's DAP framing parser (the opam `dap`
library it's built on) misparses a message body containing the byte
sequence `": "` — the key/value separator `json.dumps` emits by default
between every JSON key and its value — consuming it as though it were
additional Content-Length-style header lines and blocking forever
waiting for a blank-line terminator that never comes. Emitting *compact*
JSON (`json.dumps(msg, separators=(",", ":"))`, which never produces
`": "`) round-trips instantly and interactively from Python — this
committed probe (`tests/integration/ocaml_probe.py`) was fixed to use
compact framing and now reproduces the earlybird session correctly (see
the Q1/Q4 findings above, re-run below). **Implication for Task 5/6**:
tdb's production DAP client already sends compact JSON
(`src/tdb/dap/protocol.py`), so the direct `EarlybirdAdapter`
stdio-spawn design (spawning `ocamlearlybird debug` the same way
`DebugpyAdapter` spawns `debugpy`) is unaffected by this bug in
production — no proxy shim is needed, and Task 5/6 proceeded with the
direct-spawn design as originally planned. The historical observations
above (strace behavior, the ruled-out causes, Node/shell working) remain
accurate as recorded; they were pointing at a JSON-formatting bug in the
client, not a process- or transport-level incompatibility.

*Follow-up controller-side experiments (post-report, attributed to
follow-up controller probes, not the committed script): the same
deadlock reproduces from Python over **TCP** against
`ocamlearlybird serve --port N` — `initialize` is never answered over a
plain TCP socket, across every framing variant tried (split header/body
writes, byte-by-byte writes, LF-only line endings) — and also reproduces
over a **socketpair**-based stdio spawn from Python. Meanwhile a Node.js
`child_process.spawn` with stdin held open gets an instant interactive
`initialize` response, and a shell file-redirect (stdin closed/EOF) gets
full batch responses. So the failure tracks the **client process**
(Python vs. Node/shell), not the transport or file-descriptor type
(pipe, pty, socketpair, and TCP all reproduce it from Python; pipe and
FIFO both work from Node/shell). **Root cause: the same framing-parser
bug identified above** — earlybird's `dap`-library parser misparses a
non-compact JSON body's `": "` separator regardless of transport
(pipe/pty/socketpair/TCP all carry the same bytes to the same broken
parser). Node's `JSON.stringify` and the hand-assembled shell/FIFO batch
files happened not to trigger it because neither emits that separator by
default — the failure tracked "client encodes non-compact JSON" the
whole time, which correlated with "Python" only because `json.dumps`'s
default separators are the culprit. Switching to compact JSON framing
resolves it uniformly across every transport tried here, not just the
original stdio-pipe case.*

**Q2 — DWARF locals lldb actually sees, per frame (native).** Stopped at
a breakpoint on `Atomic.incr counter` (ocaml_domains.ml line 5, hit inside
a spawned domain). Checked `scopes`/`variables` on every frame of the
breakpoint-hit domain's stack, the spawning-thread's stack (including its
`main`/entry frames), and one backup thread. Result, uniformly: **every
OCaml frame's `Locals` scope AND `Globals` scope are both empty lists**
(`[]`) — not "sparse", not "arguments only" — completely empty. Confirmed
for: `camlOcaml_domains.worker_297` (the actual breakpoint frame — the
one place a user would most want to see `n`), `camlStdlib__Domain.body_757`,
`camlStdlib__List.init_326`, `camlOcaml_domains.entry` (the toplevel `let
()`), and the C `main` wrapper frame itself. Only the `Registers` scope
returns data (`General Purpose Registers` / `Floating Point Registers` /
`Advanced Vector Extensions` groups — raw register file, not usable for
OCaml value inspection without the planned lldb formatter layer reading
memory directly). For contrast, ordinary glibc/pthread C frames on the
same stack (`create_thread`, `__pthread_create_2_1`,
`__GI___clone_internal`) DO show populated Locals/Globals with real
values — confirming lldb's variable machinery works correctly in general;
it is specifically OCaml-compiled code (and some OCaml-runtime C, e.g.
`caml_start_program`/`caml_callback_exn`, also empty) that exposes
nothing. **This is the strongest possible confirmation of Risk 1**: v1
should set user expectations at "native locals are effectively invisible,
not just sparse" and lean harder on the bytecode adapter for locals
inspection; the lldb formatter layer's value is entirely in the case a
user drops into raw register/memory inspection, not in decorating
DWARF-reported locals (there mostly aren't any).

**Q3 — domain/backup-thread naming + distinguishing frames.** The DAP
`name` field is **useless** for classification: every thread reports the
literal string `"ocaml_domains.e"` for every kind of thread (main,
domain workers, backup threads alike) — this is Linux's 15-byte
`TASK_COMM_LEN` truncation of the binary's own filename, not a
per-thread label. `classify_threads` must be 100% frame-based. Verbatim
frame signatures observed (top of stack, native lldb-dap, per role):
- **Main thread, while spawning a domain**: `install_backup_thread`,
  `caml_domain_spawn`, `caml_c_call`, `camlStdlib__Domain.spawn_752`,
  `camlStdlib__List.init_326`, `camlOcaml_domains.entry`, `caml_program`,
  `caml_start_program`, `caml_startup_common`, `caml_main`, `main`,
  `__libc_start_call_main`, `__libc_start_main_impl`, `_start` (19 frames
  total) — blocked in `caml_plat_wait`/`__pthread_cond_wait_common`
  further up when waiting on a spawn to finish initializing.
- **Domain worker thread (running user code, this is the one that hit our
  breakpoint)**: `camlOcaml_domains.worker_297`,
  `camlStdlib__Domain.body_757`, `caml_start_program`,
  `caml_callback_exn`, `caml_callback_res`, `domain_thread_func`,
  `start_thread`, `__clone3` (8 frames). Confirms the mangled
  `camlModule__name_NNN` / `camlModule.name_NNN` convention the design
  assumed for the demangler.
- **Backup thread** (the runtime service thread the design's hook
  explicitly names): `futex_wait`, `__GI___lll_lock_wait`,
  `lll_mutex_lock_optimized`, `___pthread_mutex_lock`,
  `backup_thread_func`, `start_thread`, `__clone3` (7 frames) — the
  literal frame name `backup_thread_func` is present and is the
  recognizable signal `classify_threads` should match on, exactly as
  planned.
- **A domain OS thread that has been `clone()`'d but hasn't yet reached
  either backup-thread or user code**: stack is just `['__clone3']` (1
  frame) — a transient state; `classify_threads` must tolerate a
  single-frame stack (neither domain nor backup signal present yet) by
  leaving it visible under the raw name rather than misclassifying it.

  **Thread-list enumeration is racy right after a stop**: `threads()`
  called immediately when the `stopped` event fires returns an
  *incomplete* list (observed: 1 thread) that grows to its stable size
  over roughly 1–2 seconds of polling (observed sequence over one run:
  1 thread at t+0s, 5 threads stable from t+2s on). **Any code that reads
  the thread list right after a stop (Task 5's `classify_threads`
  consumer, ThreadsModal refresh) must poll/retry for a short window
  rather than trusting the first response.** Total thread count for 3
  spawned domains varied 4–5 across runs depending on exactly when the
  breakpoint fired relative to domain-creation progress (main + up to 3
  domain workers + 1–2 backup threads) — this is inherent scheduling
  nondeterminism, not a bug; the hook must tolerate a partial/still-
  growing set.

**Q4 — earlybird `pause` support.** *Attribution: like Q1, this finding
comes from the same ad-hoc, uncommitted Node.js interactive DAP session,
not from the committed probe (which deadlocks before it can reach
`launch`). The program used was a throwaway busy-loop `.byte`
(`let () = let i = ref 0 in while true do incr i; if !i mod 100_000_000
= 0 then Printf.printf "tick %d\n%!" !i done`) written ad-hoc for this
one check — it is not a committed fixture.* Confirmed via a full DAP
session (launch the busy-loop `.byte` with `stopOnEntry: false`, let it
run, `pause`): the `pause` request returns `{"success": true}`
immediately,
but **no `stopped` event ever follows** — the program keeps running and
producing output past the pause request (observed continuing `tick N`
stdout events for 15s+ after `pause` was ack'd). **`pause` is a no-op
against a running bytecode program.** This confirms
`pause_while_running=False` for the earlybird profile is correct as
planned — `tdb --run` cannot rely on pause-and-inspect against earlybird;
only the native lldb-dap/gdb routes get that (already verified for cpp in
Task 9's `test_cpp_pause.py`).

**Q5 — `caml_fatal_uncaught_exception` breakpoint validity.** `nm` on the
native binary shows both `caml_fatal_uncaught_exception` (global, `T`)
and `caml_fatal_uncaught_exception.cold` (local, `t`) present. Actually
setting the breakpoint — both via `lldb -b -o 'b
caml_fatal_uncaught_exception' ./ocaml_domains.exe` and via the probe's
lldb-dap session — binds cleanly: `Breakpoint 1: where =
ocaml_domains.exe\`caml_fatal_uncaught_exception, address =
0x00000000000b5430`. **Valid breakpoint target on OCaml 5.4.0**; no
fallback needed for this version. (Risk 4's cross-version concern is
still open for older/newer OCaml 5.x releases — not testable on this
machine, which only has 5.4.0.)

**Bonus finding — native vs. bytecode fatal-error text is only
*approximately* identical (Task 3 parser implication).** Both forms share
the header/message line, but backtrace depth differs:
- Bytecode (`OCAMLRUNPARAM=b ./ocaml_fatal.byte`):
  ```
  Fatal error: exception Failure("boom")
  Raised at Stdlib.failwith in file "stdlib.ml", line 29, characters 17-33
  Called from Ocaml_fatal in file "ocaml_fatal.ml", line 3, characters 9-18
  ```
  (exit code 2) — only **one** `Called from` line; the `middle`/`boom`
  tail calls collapse into the toplevel module frame (`Ocaml_fatal`, the
  bare module name, not a function name).
- Native (`OCAMLRUNPARAM=b ./ocaml_fatal.exe`, same source):
  ```
  Fatal error: exception Failure("boom")
  Raised at Stdlib.failwith in file "stdlib.ml", line 29, characters 17-33
  Called from Ocaml_fatal.boom in file "ocaml_fatal.ml" (inlined), line 1, characters 14-29
  Called from Ocaml_fatal.middle in file "ocaml_fatal.ml" (inlined), line 2, characters 16-23
  Called from Ocaml_fatal in file "ocaml_fatal.ml", line 3, characters 9-18
  ```
  (exit code 2) — **three** `Called from` lines, two of them suffixed
  `(inlined)`. `parse_ocaml_error` (Task 3) must: (1) tolerate a variable
  number of `Called from`/`Raised at` lines, (2) strip/tolerate an
  optional trailing `" (inlined)"` marker before the `, line N, ...`
  suffix, and (3) accept that the outermost frame's "function name" may
  just be the bare module name (`Ocaml_fatal`), not `Module.function`.
  Native compiles at default settings — no explicit `-O3`/flambda flags
  were used — so this inlining is stock `ocamlopt` behavior, not an
  opt-in choice users must be warned about.

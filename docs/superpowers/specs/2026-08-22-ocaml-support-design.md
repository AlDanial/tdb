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

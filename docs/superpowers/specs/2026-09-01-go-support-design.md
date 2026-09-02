# Go language support with goroutine inspection

**Date:** 2026-09-01
**Status:** Approved design, pending implementation plan

## Goal

Add Go as a supported language in tdb, using Delve's native DAP server
(`dlv dap`), with goroutine inspection comparable in fidelity to the
Python async-task / thread / process support: a dedicated goroutine
workspace (list, states, stacks, variables, wait graph, findings) plus
goroutine-aware integration with the existing threads view.

## Decisions already made

- **Backend:** Delve DAP (`dlv dap`). Not gdb — gdb sees OS threads,
  not goroutines.
- **UI:** both paths — goroutines flow through the existing
  thread/stack machinery for switching and stepping, AND a dedicated
  goroutines modal for browsing the full population.
- **Modes:** all four Delve modes in v1 — `debug` (compile+run
  source), `exec` (prebuilt binary), `test` (test binary), and attach
  (local pid + remote).
- **CLI:** mode inference from the target plus small flags; no
  `--go-mode` and no subcommands.
- **Depth:** full concurrency workspace mirroring `rust_concurrency/`
  (Approach B), with the analyzer scoped conservatively: channel /
  WaitGroup / mutex-semaphore edges, findings only at stated
  confidence, no mutex-holder claims.

## Section 1: Language profile and adapter plumbing

### `src/tdb/languages/go.py`

- `DelveAdapter(AdapterSpec)`, shaped like `cpp.py`'s adapters:
  locate `dlv` via `shutil.which`, overridable through `config.json`'s
  `adapters` map; raise `AdapterNotFoundError` with the install hint
  `go install github.com/go-delve/delve/cmd/dlv@latest` when missing.
- `build_go_profile()` assembling adapter + presentation +
  capabilities, registered in `registry.py`.

### Core extension: `spawn_tcp` connect mode

`dlv dap` does not speak DAP over stdio — it prints
`DAP server listening at: 127.0.0.1:<port>` and serves one TCP
connection. Rather than bundling a byte-pump proxy (Ruby's proxy
exists for protocol massaging that Delve does not need), extend the
adapter contract minimally:

- `AdapterSpec.connect_mode: str = "stdio"` — new field; `"spawn_tcp"`
  for Delve. No other language changes.
- `DAPClient.start()` in `spawn_tcp` mode: spawn the adapter process,
  read the "listening at" line from its stdout with a timeout, then
  connect via the existing `connect()` TCP path. The existing adapter
  death-watcher covers the subprocess either way.

This is roughly a 40-line addition to `dap/client.py`.

### Launch and attach bodies

- Launch: `{"mode": "debug"|"exec"|"test", "program", "args", "cwd",
  "env", "stopOnEntry": true}` (plus `buildFlags` passthrough later if
  needed).
- Attach (local): `{"mode": "local", "processId": <pid>}`, with
  `dlv dap` spawned locally exactly as for launch.
- Attach (remote): direct TCP to a user-run `dlv dap --listen`;
  `{"mode": "remote"}`; no local spawn in this path.
- `AdapterQuirks` stay default unless integration testing reveals
  otherwise (debugpy's launch-response ordering quirk does not apply).

### Presentation

- `lexer = "go"`.
- `parse_error`: two shapes —
  - compile failures from `debug`/`test` mode (Delve returns the
    `go build` output in the failed launch response): parse
    `# package` headers and `file.go:line: message` lines into
    `ParsedError` frames;
  - runtime panics: parse `panic: ...` + `goroutine N [running]:`
    tracebacks (`pkg.func(...)` / `\tfile.go:12 +0x1d` pairs) into
    clickable frames.
- `frame_name`: tidy Delve frame naming if needed (expected to be
  minimal; Go symbols are not mangled).

## Section 2: CLI, detection, and the four modes

### Registry (`src/tdb/languages/registry.py`)

- `register("go", build_go_profile)` — the `.go → go` extension
  mapping already exists (currently raises "not supported yet").
- **Go binary sniffing** in the executable magic-byte fallback:
  compiled Go executables currently resolve to `cpp` via ELF/PE/Mach-O
  magic. Add a sub-check: if the executable embeds the Go build-info
  magic (`\xff Go buildinf:`, what `go version <binary>` reads),
  detect as `go` instead of `cpp`. This makes `tdb ./mybinary` work
  with zero flags.

### Mode inference (`src/tdb/cli.py`)

| Invocation | Result |
|---|---|
| `tdb main.go` | launch mode `debug` (file passed to Delve) |
| `tdb ./cmd/server` (directory containing `.go` files) | launch mode `debug` (package dir passed to Delve) |
| `tdb ./binary` (executable with Go buildinfo) | launch mode `exec` |
| `tdb --test ./pkg` | launch mode `test`; args after `--` go to the test binary (`-- -run TestFoo -v`) |
| `tdb -a <pid>` (with `--lang go`, or pid's exe sniffs as Go) | attach `{"mode": "local"}` |
| `tdb -r host:port --lang go` | attach `{"mode": "remote"}` over direct TCP |

- `-a` / `--attach` is **new CLI surface**: tdb today has only
  `-r/--remote-attach` and no local attach-by-pid at all. The flag is
  added generically but accepted only for Go in v1 (rejected for other
  languages with a clear message, like `--python` for non-Python).
  With `-a`, `dlv dap` is spawned locally exactly as for launch and
  sent the local-attach body. The pid's executable is sniffed with the
  same Go-buildinfo check to infer `--lang go` when omitted.
- `--test` is a new flag, rejected for non-Go languages the same way
  `--python` is rejected for non-Python.
- Add `"go"` to the `--remote-attach` language allowlist.

### Capabilities

| Flag | Value | Rationale |
|---|---|---|
| `pause_while_running` | `True` | Delve supports DAP `pause`; unlocks `--run` |
| `child_process_strategy` | `None` | goroutines are not processes; `exec.Command` children out of scope |
| `task_inspection` | `False` | that gate drives Python-only asyncio eval snippets |
| `concurrency_inspection` | `"go"` | routes to the goroutine workspace (Section 3/4) |
| `classify_threads` | Go-specific fn | labels/hides goroutines in the plain ThreadsModal (Section 4) |
| `compute_step_units` | `None` | statement-stepper is Python-only today |
| `opaque_frame` | Go-specific fn if needed | skip runtime-internal frames on stop, pending testing |

- `--terminal` is **deferred for v1**: Delve DAP runs the debuggee
  itself and does not route it to a caller-provided tty. Add `go` to
  the `--terminal` rejection list with a clear message.
- `--record` / `--replay` are language-agnostic and work untouched.

## Section 3: The `go_concurrency` package

Mirrors `rust_concurrency/` in structure. Key simplification vs Rust:
Delve surfaces every goroutine as a DAP thread, so there are no
injected probe scripts — the collector uses ordinary DAP requests
only.

### `src/tdb/go_concurrency/models.py`

Immutable snapshot types, same discipline as the Rust models:

- `GoroutineInfo`: id, function, state, wait target (resource key +
  human label), frames, is-runtime flag.
- `GoroutineState` enum: `running`, `runnable`, `chan_send`,
  `chan_recv`, `select`, `mutex_wait`, `waitgroup_wait`, `sleep`,
  `syscall`, `runtime`, `unknown`.
- `WaitEdge` (goroutine → resource), `Finding` with `Confidence`.
- `GoroutineSnapshot` bundling the above plus counts of uncollected
  goroutines.

### `collector.py`

Bounded, stopped-state only:

1. DAP `threads`; parse Delve's `[Go N] pkg.func` thread names.
2. `stackTrace` per goroutine with a cap (default ~150;
   runtime-internal goroutines deprioritized).
3. The modal reports "N more not collected" — no silent truncation.

### `classifier.py`

State from stack shape. Top frame `runtime.gopark` (or
`goparkunlock`), then the caller identifies the park reason:

| Caller frames | State |
|---|---|
| `runtime.chanrecv` / `runtime.chansend` | `chan_recv` / `chan_send` |
| `runtime.selectgo` | `select` |
| `sync.runtime_SemacquireMutex` | `mutex_wait` |
| `sync.runtime_Semacquire` (via `WaitGroup.Wait`) | `waitgroup_wait` |
| `time.Sleep` | `sleep` |
| syscall frames | `syscall` |

**Wait-target identity:** a frame-scoped DAP `evaluate` of the channel
pointer (`c`, the `*hchan`) in the `chanrecv`/`chansend` frame yields
a stable address string — the wait-graph key. Mutex semaphore
addresses likewise from the semacquire frame. Select goroutines get a
state but **no edges in v1** (enumerating `scases` is deferred).

Any per-goroutine failure (evaluate fails, stack fetch fails) degrades
that entry to `state=unknown`; it never fails the snapshot.

### `analyzer.py`

Bipartite wait graph, like Python's task graph: goroutine → resource →
other waiters (never fabricated goroutine → goroutine edges).
Findings, conservative by design:

- **Stuck channel** — waiters on one side of a channel, no
  counterpart, and no runnable/running non-runtime goroutine; high
  confidence only when *all* non-runtime goroutines are blocked.
- **Mutex convoy** — many waiters on one semaphore address.
- **Likely leak** — large same-channel waiter cluster.
- **No mutex-holder claims** — Go mutexes do not record owners; the
  analyzer must not pretend otherwise.

### Service layer

`InspectService` gains `collect_go_concurrency()` alongside
`collect_rust_concurrency()`, behind the same
`_require_concurrency_inspection()` gate. Per-goroutine stacks,
scopes, and variables need **no new path**: goroutines are DAP
threads, so the existing `thread_frames` / `thread_stack` methods
already serve them.

## Section 4: UI

### `src/tdb/widgets/goroutines_modal.py`

`GoroutinesModal(_InspectableListModal[GoroutineInfo])` wrapped in
`TabbedContent`, following `rust_concurrency_modal.py`:

- **Tab 1 — Goroutines:** table `(ID, State, Function, Waiting on)`;
  runtime-internal goroutines hidden by default, `a` toggles (same key
  as ThreadsModal); goroutines implicated in a finding highlighted red
  (async-task deadlock-cycle pattern). Detail pane: full stack + lazy
  `VariableView` of the selected goroutine's top frame, via the
  existing thread-detail machinery. `Enter` switches the main
  Code/Stack/Variables views to that goroutine (existing
  `SelectThread`-style message), so stepping continues from it.
- **Tab 2 — Wait graph:** `Tree` of resource → waiters (channel
  `0xc0000123` → senders/receivers; WaitGroup → waiters), reusing the
  async-tasks graph-pane pattern.
- **Tab 3 — Findings:** analyzer findings with confidence labels,
  like Rust's.

### Wiring

- `app_handlers/inspection.py` `open_threads`: the existing
  `concurrency_inspection == "rust"` divert becomes a small dispatch
  (`"rust"` → Rust modal, `"go"` → goroutines modal), **falling back
  to the generic ThreadsModal if snapshot collection fails**.
- `UIPanels.goroutines` slot; routing entries; menu item
  **Goroutines (N)** with the count refreshed on stop (cheap —
  `len(threads)`, no stacks needed), mirroring `Async Tasks (N)`.

### Plain ThreadsModal

Remains functional (it is both the fallback and reachable via the
`a` toggle): the Go profile's `classify_threads` labels entries
`[Go 7] main.worker`, marks runtime goroutines hidden, and bolds the
current goroutine — the OCaml pattern.

### RPC / MCP

`action_goroutines` + `action_go_wait_graph` in
`server/handlers.py`, and matching MCP tools, same shape as the
rust/tasks ones. Per the founding multi-language design rule: gated
features stay in the RPC/MCP schema and return structured errors when
the language does not support them.

## Section 5: Error handling

- **Missing `dlv`:** `AdapterNotFoundError` with install hint at
  resolve time, before any UI starts.
- **`spawn_tcp` handshake:** timeout waiting for the "listening at"
  line (dlv crashed, incompatible version) → error surfaces dlv's
  stderr. Mid-session adapter death is covered by the existing
  death-watcher.
- **Compile errors** (`debug`/`test`): failed launch response carries
  the `go build` output; `parse_error` renders it with clickable
  `file.go:line` frames.
- **Panics:** stop-on-exception via Delve's exception filters
  (`pick_exception_filters`); panic text parsed into frames.
- **Collector degradation:** per-goroutine failures degrade to
  `state=unknown`; snapshot failure falls back to the plain
  ThreadsModal. Same fail-soft rule as Rust.

## Section 6: Testing

Three tiers, following the established pattern:

- **Unit:**
  - profile contract tests: registration, detection (Go-buildinfo
    magic vs ELF→cpp precedence), mode inference table above,
    `--test` / `--terminal` gating, remote-attach allowlist;
  - classifier tests on canned stack fixtures — at least one per
    `GoroutineState`;
  - analyzer tests on hand-built snapshots: stuck channel, mutex
    convoy, and no-false-positive cases (e.g. matched sender/receiver
    pair is not a finding);
  - `spawn_tcp` handshake against a fake adapter that prints the
    listen line (and one that never does → timeout path).
- **Integration:** `tests/integration/test_go_session.py` + a small
  Go fixture program with goroutines blocked on channels, a mutex,
  and a WaitGroup — breakpoints, conditional breakpoints, stepping,
  evaluate console, variables, goroutine snapshot end-to-end, all
  four modes where feasible; `skipif` when `go`/`dlv` are absent.
- **CI / docs:** Dockerfile gains the Go toolchain + `dlv`;
  README's language table and per-language limitations get a Go
  entry.

## Out of scope for v1 (documented in README limitations)

- `--terminal` for Go (Delve DAP does not route the debuggee tty).
- Select-case wait edges (`scases` enumeration).
- Mutex-holder identification (impossible without runtime help).
- Debugging children spawned via `exec.Command`.
- Delve `substitutePath` / remote path mapping beyond what the
  existing remote-attach path-mapping plumbing provides.

## Implementation notes

- Branch: `add-go-support`, cut from `main`.
- The founding design doc
  (`docs/superpowers/specs/2026-07-13-multi-language-dap-design.md`)
  names `GoProfile` as planned work; this spec fulfils it.
- Closest templates: `languages/cpp.py` (adapter shape),
  `rust_concurrency/` + its modal (workspace shape),
  `docs/superpowers/plans/2026-08-22-ocaml-support.md` Tasks 7–9
  (thread classification/labeling), and the Ruby plan for overall
  task sequencing of an external-DAP-server language.

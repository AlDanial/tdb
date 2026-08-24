# Rust Thread and Synchronization Support — Design

**Date:** 2026-08-22
**Status:** Approved design, pre-implementation
**Goal:** Add Rust debugging to tdb for already-built debug executables and provide best-effort inspection of `std::thread` and standard-library synchronization relationships on Linux and macOS.

## Scope and decisions

The first release supports unmodified programs built with the stable Rust toolchain current when tdb ships. At design time, that is Rust 1.98.0. Optimized and release binaries are outside the supported concurrency-inspection target; ordinary DAP debugging may still work with them.

Users invoke tdb against an existing executable and must select Rust explicitly:

```text
tdb --lang rust ./target/debug/app
```

ELF and Mach-O headers do not identify the source language, so native binary auto-detection remains mapped to C/C++. tdb does not run Cargo, choose a Cargo target, or build the program.

The concurrency inspector recognizes all of the following:

- `std::thread::JoinHandle::join`
- `std::sync::Mutex`
- `std::sync::RwLock`, including read and write waits
- `std::sync::Condvar`
- bounded and unbounded `std::sync::mpsc` send and receive operations
- `std::thread::park` and `unpark`

Programs are not linked with or modified by a tdb runtime. Relationships are therefore best-effort and may contain explicitly unknown owners or peers. Confirmed deadlocks and suspected stalls are both shown.

Linux supports GDB and LLDB. macOS supports LLDB. GDB is selectable on Linux from the first release, not deferred work.

## Architecture

Rust support uses the existing language-profile architecture and standard DAP session lifecycle. It adds one optional concurrency-inspection capability rather than making the profile stateful or teaching the generic thread UI about Rust internals.

At each stopped-state inspection:

1. The existing DAP client obtains threads, bounded stack traces, scopes, and variables.
2. A Rust snapshot collector normalizes thread identities, demangled frames, visible locals and arguments, object addresses, and platform wait frames.
3. A GDB or LLDB evidence probe performs additional fixed, read-only debugger queries.
4. A side-effect-free concurrency analyzer classifies Rust waits, links evidence, and finds cycles and whole-program stalls.
5. One immutable analysis result feeds the TUI, JSON-RPC, and MCP interfaces.

The units and their boundaries are:

- **Rust language profile:** declares adapters, presentation, pause support, and the concurrency-inspection capability. It contains no runtime state.
- **Snapshot collector:** coordinates DAP requests while the debuggee is stopped and produces a debugger-neutral snapshot.
- **Adapter evidence probes:** isolate GDB- and LLDB-specific command syntax and result parsing. A probe failure cannot prevent the base DAP snapshot.
- **Analyzer:** a pure transformation from snapshot plus evidence to threads, primitives, relationships, cycles, and stalls. It has no controller or widget dependency.
- **Rust Concurrency workspace:** renders the immutable result and delegates source, stack, and locals navigation to existing application services.

A future helper crate can implement the same evidence-provider contract. It can supply exact runtime relationships without changing the analyzer, public result model, or UI.

## Rust language profile

Add a registered `rust` profile with:

- `id="rust"`, display name `Rust`
- Rust source lexer
- line-granularity stepping; no Rust statement parser in this effort
- `pause_while_running=True`
- no Python task or child-process capability
- a new Rust concurrency inspector capability

The default adapter is GDB on Linux and `lldb-dap` on macOS. `--adapter gdb` and `--adapter lldb-dap` select explicitly where supported. Adapter executable overrides continue to use tdb's existing configuration mechanism.

The registry accepts `--lang rust` but does not add Rust binary auto-detection. Passing a `.rs` source file keeps the existing compiled-source error and should mention compiling with debuginfo before launching the resulting executable.

## Launch and operating modes

### Normal launch

GDB and LLDB reuse the native adapter lifecycle already used by the C/C++ profile. Rust receives a distinct profile so presentation, compatibility checks, and concurrency inspection are not attached to C/C++ sessions.

### `--run`

Both adapters support Rust run mode through the existing controller flow:

1. Launch without stopping on entry or installing CLI breakpoints.
2. Stream program output while tdb has no TUI.
3. On Ctrl-C or SIGUSR1, issue DAP `pause`.
4. Adopt the stopped session into the TUI.
5. Allow the Rust Concurrency workspace to collect a snapshot.
6. On detach-and-resume, continue the same adapter session.

Real GDB and LLDB pause integration tests in the repository validate the underlying native adapter behavior. Rust-specific run-mode tests must additionally exercise a program blocked inside standard-library synchronization calls.

### `--terminal`

`--terminal` is supported with LLDB through tdb's existing `runInTerminal` reverse-request handler. The debuggee receives the selected external terminal's controlling TTY, input, and output.

GDB terminal launch is not supported in the first release. `--lang rust --adapter gdb --terminal ...` fails before spawning a process and recommends `--adapter lldb-dap`. GDB `inferior-tty` integration remains possible follow-up work.

### `--remote-attach`

Rust remote attach means native remote debugging, not a direct connection to a remote DAP server. tdb spawns the selected DAP adapter locally and that adapter connects to `gdbserver`, `lldb-server`, or macOS `debugserver`.

The Rust adapters set `attach_via_adapter=True`. Their attach bodies translate the endpoint as follows:

- GDB: `target="HOST:PORT"`, which GDB DAP passes to `target remote`
- LLDB: `gdb-remote-host=HOST` and `gdb-remote-port=PORT`

Rust remote attach requires the positional `PROGRAM` to be an existing local, unstripped copy of the exact remote executable:

```text
tdb --lang rust --adapter gdb \
    --remote-attach host:2345 ./target/debug/app
```

The CLI resolves and validates this local path even in attach mode, then threads it through the controller into the adapter attach body. Existing `--local-root` and `--remote-root` pairs are translated into the chosen adapter's source-map representation.

Disconnect detaches without terminating an independently attached remote process. If the remote stub launched and owns the debuggee, termination follows the stub's reported ownership semantics. The remote-stub transport has no tdb authentication or encryption; documentation recommends localhost or an SSH tunnel.

The supported mode matrix is:

| Mode | LLDB | GDB |
|---|---:|---:|
| Normal launch | Yes | Yes |
| `--run` | Yes | Yes |
| `--terminal` | Yes | No |
| `--remote-attach` | Yes | Yes |

## Snapshot and evidence model

Collection runs only while the session is stopped. It never calls a function in the inferior. The normalized snapshot includes:

- DAP thread ID, adapter name, and any visible OS thread ID
- bounded, demangled stack frames for every thread
- source location and module for each frame
- selected frame arguments and locals needed for classification
- typed object addresses and primitive state when visible
- recognized OS wait frames, including Linux futex and macOS pthread or ulock paths
- compiler, target, adapter, and platform metadata

Each fact and inferred edge records:

- the observed value or relationship
- provenance, such as a frame argument, matched object address, OS thread ID, or adapter probe
- confidence: `confirmed`, `probable`, or `unknown`
- an optional compatibility or collection warning

Unknown evidence is a first-class result, not an error and not permission to guess.

## Primitive inference

The analyzer applies versioned rules for the supported stable Rust release.

- **Join:** recognize `JoinHandle::join` frames and link the target thread only when a native handle or OS thread ID can be recovered.
- **Mutex:** identify the mutex address from Rust frames or visible arguments. Report an owner only when debugger-visible evidence links the same primitive to a thread.
- **RwLock:** distinguish read and write waits. Report writer or reader ownership only to the extent the platform representation exposes it.
- **Condvar:** identify the condition variable and, when recoverable, the associated mutex. A wake source is not inferred from the mere existence of waiters.
- **MPSC:** identify the shared channel state and classify send, receive, and timeout waits. Peer threads are linked only when a matching shared-state address is visible.
- **Park/unpark:** recognize parked threads and visible token state. Historical callers of `unpark` are not reconstructed or guessed.

An OS wait frame corroborates a Rust classification but cannot identify a Rust primitive by itself.

## Deadlocks and suspected stalls

The analyzer constructs a directed graph of thread and primitive nodes with wait and ownership edges.

A confirmed deadlock is a closed wait cycle in which every edge is confirmed. A cycle containing probable or missing relationships appears as a suspected stall, with its evidence gaps listed.

A suspected whole-program stall also appears when every relevant application thread is blocked and no runnable application thread is visible, even when incomplete evidence prevents constructing a cycle. Known debugger, adapter, and runtime housekeeping threads are excluded. Exclusions are versioned and test-covered; an unfamiliar thread is retained rather than silently discarded.

## Rust Concurrency workspace

Rust routes its Threads action to a dedicated near-full-screen workspace with three tabs over the same snapshot:

1. **Threads:** state-first thread list plus selected wait evidence, stack, and locals.
2. **Wait Graph:** thread and primitive nodes, directional edges, confidence styling, keyboard traversal, and an accessible textual edge list.
3. **Deadlocks & Stalls:** confirmed cycles first, suspected cycles second, then whole-program stalls. Each finding explains uncertainty and links to affected threads.

Confidence is never encoded by color alone. Every relationship has a textual label and a short evidence explanation.

The workspace is available only while stopped. Opening it while running uses the existing pause-first interaction. Refresh atomically replaces the snapshot. Continue, step, restart, or termination closes the workspace so stale relationships cannot remain visible. Selecting a source frame uses the existing navigation flow to update the main code, stack, and locals views.

Other languages retain the existing Threads modal unchanged.

## RPC and MCP

JSON-RPC and MCP expose the same structured concurrency snapshot used by the TUI rather than formatted widget text. The schema includes metadata, threads, primitives, edges, confirmed deadlocks, suspected stalls, and warnings.

The existing generic thread list and inspection actions remain available. Rust adds a concurrency-snapshot action/tool; it does not change the meaning of Python tasks or wait graphs. Requests while running return the existing stopped-state gate error. Unsupported languages return a structured capability error.

## Compatibility and failure handling

The first release officially supports only the current stable Rust toolchain at release time. tdb reads the Rust compiler version from DWARF producer metadata when available.

If the version is absent, unsupported, or paired with an unknown standard-library layout:

- core DAP debugging continues
- generic thread stacks remain available
- stack-only Rust wait classification may run
- layout-specific probes do not run
- the workspace displays a compatibility warning

Collection has the following safety bounds:

- fixed, read-only debugger queries with no user-derived command fragments
- no inferior function calls
- per-probe and whole-snapshot timeouts
- bounded stack depth and variable traversal
- cancellation when execution resumes
- probe errors converted into evidence warnings

An individual probe timeout, parse failure, missing local, inaccessible page, or remote read failure never fails the debug session or discards evidence gathered by other providers.

## Testing

### Pure analyzer tests

Saved debugger-neutral snapshots and golden results cover every primitive, confidence transition, graph edge, confirmed cycle, suspected cycle, whole-program stall, and housekeeping-thread exclusion. These tests require no debugger or Rust toolchain.

### Adapter probe tests

Saved GDB and LLDB outputs cover successful extraction plus missing fields, renamed frames, unfamiliar layouts, malformed output, inaccessible memory, cancellation, and timeout. Contract tests assert that probes issue only their fixed read-only command set.

### Real Rust integrations

Debug fixtures built by the supported stable toolchain cover:

- join waits
- mutex contention
- read and write lock contention
- condition-variable waits
- bounded and unbounded MPSC send/receive waits
- park/unpark
- a fully confirmed cycle where platform evidence permits it
- incomplete cycles and whole-program stalls
- blocked but healthy programs that must not be labeled deadlocked

Linux runs the suite with GDB and LLDB. macOS runs it with LLDB. Tool-dependent tests skip with a clear reason when an adapter, remote stub, or Rust compiler is unavailable.

Mode integrations cover normal launch, run-mode pause/adopt/resume, LLDB external-terminal launch, and GDB/LLDB remote-stub attach with local symbols and source mapping.

### UI and transport tests

Tests cover all tabs, keyboard navigation, accessible confidence labels, refresh, resume-close behavior, and source/locals navigation. JSON-RPC and MCP tests assert that both expose the same immutable result and structured errors. Regression tests prove that existing language profiles and the generic Threads modal are unchanged.

## Future improvements

An opt-in Rust helper crate could record stable thread identities, lock ownership, channel peers, joins, parks, and wake operations directly in the debuggee. It would substantially increase relationship accuracy and reduce dependence on private standard-library layouts. It is deliberately deferred because the first release must work with unmodified programs.

Other deferred work includes optimized-build analysis, older Rust compatibility, GDB external-terminal support, automatic Rust binary detection, Cargo build/target selection, and Windows support.

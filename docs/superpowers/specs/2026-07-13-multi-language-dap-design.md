# Multi-Language DAP Support — Design

**Date:** 2026-07-13
**Status:** Approved design, pre-implementation
**Goal:** Make tdb work with any programming language that has a Debug Adapter
Protocol (DAP) implementation, starting with C++ (lldb-dap, then `gdb -i dap`)
and later Go (delve). Python remains the reference language with the fullest
feature set.

## Motivation and feasibility

tdb today is hardwired to Python/debugpy. A full audit of `src/tdb`
(~60 modules, 13.7k LOC) found the coupling concentrated in three tiers:

1. **Adapter lifecycle (EASY/MEDIUM).** `dap/client.py` hardcodes the adapter
   spawn command (`sys.executable -m debugpy.adapter`), `adapterID: "debugpy"`,
   and debugpy-specific launch/attach bodies (`python`, `justMyCode`,
   `subProcess`, `pythonArgs`, debugpy-shaped `pathMappings`).
   `controller.do_configure` hardcodes the `"userUnhandled"` exception filter.
   `code_view` hardcodes the `"python"` syntax lexer.
2. **Source-language analysis (MEDIUM, degrades gracefully).** Statement-
   granular stepping and `-k` breakpoint snapping use Python's `ast` module
   (`source_analysis.py`, `statement_stepper.py`). Both already no-op safely
   on unparseable source. `_decode_json_repr` / `unquote_dap_string` assume
   debugpy's repr-quoting of `evaluate` results; `inspection_full.py` tree
   heuristics are tuned to debugpy's variable rendering.
3. **Python runtime features (HARD, no DAP equivalent).** Post-mortem
   (`sys.excepthook`), `tdb.breakpoint()` (`debugpy.listen`), asyncio task
   inspection + wait-graph (Python source injected via DAP `evaluate`),
   multiprocessing inspection, evaluate-console `?` help, and
   `child_processes.py` (built on debugpy's proprietary `debugpyAttach`
   event).

The DAP wire layer (`dap/protocol.py`, `dap/messages.py`, `dap/types.py`),
state model, all widgets, the JSON-RPC server, and MCP transport are already
language-agnostic. **Verdict: feasible.** Parameterizing tier 1 alone yields
working breakpoints/stepping/stack/variables/eval for any DAP adapter; tiers
2–3 become per-language capabilities.

## Decisions made during brainstorming

- **Targets:** C++ first (lldb-dap default, `gdb -i dap` alternate), Go
  (delve) next. lldb-dap debugs GCC-built binaries (DWARF is
  compiler-neutral); libstdc++ pretty-printing is weaker in LLDB than GDB,
  which is why the GDB adapter is a first-class alternate.
- **Feature parity:** Python-specific extras become optional per-language
  capabilities (plugins), not requirements. Non-Python languages ship with
  core DAP debugging.
- **Invocation:** auto-detect language from the target, `--lang` / `--adapter`
  override.
- **Adapter supply:** user-installed, found on PATH, with config-file path
  overrides and a clear install hint when missing. tdb does not download or
  bundle adapters.
- **Architecture:** approach B — a `LanguageProfile` abstraction with a
  registry (entry-point plugin packaging deferred; the interface must not
  preclude it).

## Architecture

### The `LanguageProfile` interface

New package `src/tdb/languages/`. To prevent the profile from becoming a
god-object, it is a three-way split where **each member has exactly one
consumer**, and consumers are existing modules:

```python
class LanguageProfile:
    id: str  # "python", "cpp", "go"
    display_name: str  # title bar / status
    adapter: AdapterSpec  # default adapter; alternates selectable via --adapter
    presentation: Presentation
    capabilities: Capabilities
```

| Field | Sole consumer |
|---|---|
| `adapter.command`, `launch_body`, `attach_body`, `pick_exception_filters`, `quirks` | `dap/client.py` / `session/controller.py` |
| `presentation.lexer` | `widgets/code_view.py` |
| `presentation.decode_evaluate_result` | evaluate console / `session/inspect_service.py` |
| `capabilities.statement_stepper` | `session/statement_stepper.py` |
| `capabilities.task_inspector` | `session/inspect_service.py` |
| `capabilities.child_process_strategy` | `session/child_processes.py` |

(Post-mortem and `tdb.breakpoint()` deliberately do **not** appear as
capabilities — see Feature gating: they are producer-side hooks that exist
only inside a Python debuggee and need no gate.)

Rules that keep this compartmentalized:

- **One-way dependency:** modules read the profile; a profile never imports
  the controller, app, or widgets, and never holds runtime state. Profiles
  are data plus pure functions — unit-testable with no lifecycle.
- **Capability objects, not subclass overrides,** for optional features:
  UI/MCP layers feature-gate with `is not None`, and a future entry-point
  plugin system can compose capabilities without inheritance.
- **Language ≠ adapter.** A language (detection, lexer, source analysis) can
  be served by multiple adapters (`lldb-dap` or `gdb` for C++; debugpy is
  merely Python's default). `AdapterSpec` is a separate object referenced by
  the profile, so a second adapter for a language reuses the language side
  unchanged.

`DAPClient` and `DebugController` take the profile as a constructor parameter
and lose all hardcoded `"debugpy"` / `"python"` strings.

### AdapterSpec

```python
@dataclass
class AdapterSpec:
    id: str                                   # "debugpy", "lldb-dap", "gdb"
    def command(self, config) -> list[str]    # PATH lookup + config override
    transport: Literal["stdio"]               # all current targets speak DAP over stdio;
                                              # TCP remains for remote-attach/child sessions
    def launch_body(self, program, args, opts) -> dict
    def attach_body(self, host, port, opts) -> dict
    def pick_exception_filters(self, caps: Capabilities) -> list[str]
    quirks: AdapterQuirks
```

- **Spawn:** `DAPClient.start()` runs `spec.command()` instead of the
  hardcoded debugpy module invocation. A missing executable produces a
  one-line actionable error (e.g., "lldb-dap not found — install LLVM ≥ 17 or
  set `adapters.cpp` in config").
- **Launch bodies** are per-adapter builders: debugpy sends
  `python`/`justMyCode`/`subProcess`/`pythonArgs` (the `-Xfrozen_modules=off`
  workaround moves inside debugpy's builder); lldb-dap sends
  `program`/`args`/`env`/`cwd`/`stopOnEntry`; gdb-dap is close to lldb's.
- **Exception filters** are chosen from the adapter-advertised
  `Capabilities.exceptionBreakpointFilters` by `pick_exception_filters()`:
  debugpy → `userUnhandled`; lldb-dap → `cpp_throw`; unknown adapters →
  filters the adapter marks `default: true`. The hardcoded list in
  `do_configure` is removed.
- **AdapterQuirks** is a small flags struct that keeps `controller.py` clean:
  `pre_arm_pause_on_attach` (debugpy ignores stopOnEntry on attach — True
  only for debugpy) and `cold_start_timeout` (overrides `_timeouts.py`
  defaults). The deferred-launch-response handling needs no flag: the
  existing fire-and-forget launch future is DAP-spec-compliant and correct
  for all adapters. The `debugpyAttach` listener is registered only when the
  profile provides a child-process strategy.
- `runInTerminal` handling (`session/terminal.py`) is standard DAP and stays
  shared.

### Detection, CLI, and config

`languages/registry.py` holds `{profile_id: LanguageProfile}` and the
detection chain, tried in order:

1. Explicit `--lang cpp` (plus `--adapter gdb` to pick a non-default adapter
   within the language).
2. Extension map: `.py` → python; `.go` → go (errors "language 'go' not yet
   supported" until `GoProfile` lands); `.c/.cc/.cpp/.rs` → **error**
   with a hint ("compile with `-g`, then `tdb ./binary`" — you debug the
   executable, not the source file).
3. Binary magic bytes: ELF (`\x7fELF`), Mach-O, PE (`MZ`) → cpp profile.
4. No match → error listing supported languages and the `--lang` override.

CLI:

- New flags: `--lang`, `--adapter`.
- Python-specific flags stay but are profile-validated: `--python`, `--pv`,
  and `--no-subprocess` error clearly when the resolved profile is not
  python.
- `--remote-attach`, `--local-root`/`--remote-root`, `-k`, `--headless`,
  `--mcp` remain generic. Server and MCP modes gain multi-language support
  for free because the controller is profile-parameterized.

Config (`persist.py`): a new `[adapters]` section for executable overrides
(`cpp = /opt/llvm/bin/lldb-dap`) and per-language default adapter
(`cpp_adapter = gdb`). Per-program breakpoint persistence is already keyed by
program path and works for binaries unchanged.

**Missing source** is handled deliberately: C++ frames reference DWARF
compile-time paths, and system-library frames often have no source on disk.
`code_view` degrades to a "source not available: `<path>`" placeholder pane
while stack/variables/eval remain fully functional. (This also benefits
Python frames in zipped/frozen stdlib.)

### Feature gating

Principle: **gated features disappear from the UI and return structured
errors over RPC/MCP — never a crash, never a dead menu item.**

- **Statement stepping** — `capabilities.statement_stepper is None` forces
  line mode: the `t` toggle and `statement` footer hint are hidden,
  `step_mode="statement"` in config is treated as `line`, `-k` snapping
  passes lines through unchanged. (Makes the existing exception-driven
  degradation explicit.)
- **Async tasks / multiprocessing / wait graph** — hang off
  `capabilities.task_inspector`. When `None`: modal menu items and
  keybindings are not registered; MCP `tasks`/`processes`/`wait_graph` tools
  **stay in the tool list** (stable schema for agents) but return
  `{"error": "not supported for language 'cpp'"}`; same structured error
  over JSON-RPC.
- **Child processes** — no strategy → the `debugpyAttach` listener is never
  registered and pause-all/continue-all fan-out reduces to the existing
  single-client paths.
- **Post-mortem and `tdb.breakpoint()`** — need no gating: both are
  producer-side hooks that exist only inside a Python debuggee that does
  `import tdb`. The consumer side (`--post-mortem` snapshot loading) is
  already language-agnostic. Unchanged.
- **Small leaks** — evaluate-console `?` doc/signature help (injects Python
  `inspect` calls) hides for non-Python; the `breakpoint_hook.py`
  auto-step-out check in `app_handlers/dap_events.py` registers only for the
  python profile; `inspection_full.py` heuristics fall back to "expand only
  what the adapter marks expandable".

## Error handling

In order of likelihood:

- **Adapter missing** → one-line install hint naming the config override.
- **Adapter crashes at startup** → existing behavior carries over: `stop()`
  tolerates a dead process (`ProcessLookupError` guard) and the adapter's
  last stderr line is surfaced in the `ConnectionError`.
- **Binary built without `-g`** → breakpoints return `verified: false`; the
  breakpoint view already tracks verification and gains a status-bar hint:
  "breakpoints unbound — compiled without debug info?".
- **Missing source** → placeholder pane; session remains fully usable.

## Testing

Builds on the existing harnesses (FakeAdapter TCP DAP server, `_FakeDAP`
recording fake, real-subprocess integration fixtures):

- **Phase 1 is regression-gated:** extracting `PythonProfile` must leave all
  existing unit tests (641) and real-subprocess integration tests (12) green
  **unchanged** — the proof of zero behavior change.
- **Profile contract tests:** one parametrized suite runs against every
  registered profile: launch/attach bodies are well-formed,
  `pick_exception_filters` tolerates an adapter advertising zero filters,
  `command()` on a missing executable yields the install-hint error, quirks
  are read not guessed.
- **FakeAdapter reuse:** controller-level tests run with `CppProfile`
  against the language-neutral fake — no lldb needed for unit coverage.
- **Real-adapter integration tests:** a C++ fixture compiled in-test
  (`g++ -g -O0`), debugged via real lldb-dap: stop-on-entry, breakpoint hit,
  step, locals, evaluate, run-to-completion — mirroring the debugpy
  integration suite. A smaller suite for `gdb -i dap` (requires GDB ≥ 14).
  Both `skipif` when the toolchain is absent so CI without LLVM/GDB passes.
- **Gating tests:** MCP `tasks` against a cpp session returns the structured
  error; detection unit tests cover magic bytes, extensions, and override
  precedence.

## Implementation phases

Each phase independently shippable and verified by the test suite:

1. **Extract `PythonProfile`** and thread the profile through
   `DAPClient`/`DebugController`. Zero behavior change; existing tests are
   the regression net.
2. **Detection registry + `--lang`/`--adapter` CLI + `[adapters]` config.**
   Pure addition.
3. **`CppProfile` (lldb-dap)** + feature gating in UI/MCP + missing-source
   placeholder + C++ integration tests.
4. **`gdb -i dap` alternate adapter** — doubles as proof the
   language/adapter seam is real, since it reuses `CppProfile`'s language
   side with a different adapter side.

Later (out of scope for this spec): `GoProfile` (delve), entry-point plugin
packaging (approach C), child-process support for non-Python via the
standard `startDebugging` reverse request, per-language statement-stepper
providers (e.g., tree-sitter).

## Out of scope

- Downloading or bundling adapters.
- Feature parity for Python-only extras (async tasks, post-mortem,
  `tdb.breakpoint()`) in other languages.
- Non-DAP debuggers.
- Windows-specific adapter quirks beyond what the contract tests cover
  (PE detection is included; cppvsdbg is not).

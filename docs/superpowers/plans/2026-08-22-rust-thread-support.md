# Rust Thread and Synchronization Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Rust debugging for pre-built debug executables plus best-effort `std::thread` synchronization snapshots, wait graphs, confirmed deadlocks, and suspected stalls on Linux and macOS.

**Architecture:** A new Rust language profile reuses GDB/LLDB DAP lifecycle behavior. A stopped-state collector combines portable DAP stacks/variables with isolated GDB and LLDB evidence probes, then passes immutable records to a side-effect-free analyzer consumed by the TUI, JSON-RPC, and MCP.

**Tech Stack:** Python 3.11+, asyncio, DAP, Textual, Pydantic/FastAPI, FastMCP, pytest/pytest-asyncio, GDB >= 14, lldb-dap, Rust stable debug builds.

**Spec:** `docs/superpowers/specs/2026-08-22-rust-thread-support-design.md`

## Global Constraints

- Support unmodified debug binaries built by the stable Rust toolchain current when tdb ships; at plan creation that is Rust 1.98.0.
- Linux supports GDB and LLDB; macOS supports LLDB.
- Require `--lang rust`; do not auto-detect Rust binaries or invoke Cargo.
- Support normal launch and `--run` on both adapters, `--terminal` only on LLDB, and adapter-mediated `--remote-attach` on both.
- Recognize join, mutex, read/write lock, condition-variable, bounded/unbounded MPSC, and park/unpark waits.
- Never call functions in the inferior; debugger probes are fixed and read-only.
- Preserve uncertainty as `confirmed`, `probable`, or `unknown`; never guess missing owners or peers.
- Other languages retain the existing Threads modal and behavior.
- Keep generated visual-companion files under `.superpowers/` out of commits.

## File map

New production files:

- `src/tdb/languages/rust.py` — Rust profile and GDB/LLDB launch/attach bodies.
- `src/tdb/rust_concurrency/__init__.py` — public concurrency-inspection exports.
- `src/tdb/rust_concurrency/models.py` — immutable raw observations, evidence, graph, findings, and result serialization.
- `src/tdb/rust_concurrency/classifier.py` — version-independent stack/frame classification.
- `src/tdb/rust_concurrency/analyzer.py` — evidence linking, graph construction, cycle detection, and suspected-stall rules.
- `src/tdb/rust_concurrency/collector.py` — bounded, cancellable DAP snapshot orchestration.
- `src/tdb/rust_concurrency/probes/base.py` — probe protocol, JSON envelope validation, and safe command execution.
- `src/tdb/rust_concurrency/probes/gdb.py` — GDB command adapter and parser.
- `src/tdb/rust_concurrency/probes/lldb.py` — LLDB command adapter and parser.
- `src/tdb/rust_concurrency/probes/gdb_script.py` — script loaded inside GDB; registers fixed `tdb-rust-snapshot` command.
- `src/tdb/rust_concurrency/probes/lldb_script.py` — script loaded inside LLDB; registers fixed `tdb-rust-snapshot` command.
- `src/tdb/widgets/rust_concurrency_modal.py` — three-tab Rust concurrency workspace.

Existing production files modified:

- `src/tdb/languages/base.py`, `src/tdb/languages/registry.py` — capability and registration.
- `src/tdb/dap/client.py`, `src/tdb/session/controller.py`, `src/tdb/cli.py`, `src/tdb/server/runner.py`, `src/tdb/mcp/session.py` — native remote-attach program threading.
- `src/tdb/session/inspect_service.py` — snapshot service and stopped-state gate.
- `src/tdb/app_handlers/inspection.py`, `src/tdb/app_handlers/routing.py`, `src/tdb/app_handlers/ui_panels.py`, `src/tdb/app_handlers/dap_events.py` — workspace lifecycle and navigation.
- `src/tdb/server/handlers.py`, `src/tdb/server/rpc_types.py`, `src/tdb/mcp/server.py` — structured transport surface.
- `README.md`, `examples/README.md` — invocation, platform matrix, remote-stub setup, and limitations.

---

### Task 1: Rust language profile and local launch

**Files:**
- Create: `src/tdb/languages/rust.py`
- Modify: `src/tdb/languages/base.py`
- Modify: `src/tdb/languages/registry.py`
- Modify: `src/tdb/cli.py`
- Test: `tests/unit/test_rust_profile.py`
- Test: `tests/unit/test_registry_rust.py`
- Test: `tests/unit/test_cli.py`

**Interfaces:**
- Produces: `build_rust_profile(adapter: str | None = None, adapter_paths: dict[str, str] | None = None) -> LanguageProfile`.
- Produces: `ProfileCapabilities.concurrency_inspection: str | None`, with Rust set to `"rust"`.
- Produces: `RustGdbAdapter` and `RustLldbAdapter`, initially sharing local-launch behavior with the native C/C++ adapters.

- [ ] **Step 1: Write failing profile and registry tests**

```python
def test_rust_profile_defaults_by_platform(monkeypatch):
    monkeypatch.setattr("tdb.languages.rust.sys.platform", "linux")
    assert build_rust_profile().adapter.id == "gdb"
    monkeypatch.setattr("tdb.languages.rust.sys.platform", "darwin")
    assert build_rust_profile().adapter.id == "lldb-dap"


def test_rust_profile_capabilities():
    profile = build_rust_profile(adapter="lldb-dap")
    assert profile.id == "rust"
    assert profile.presentation.lexer == "rust"
    assert profile.capabilities.pause_while_running is True
    assert profile.capabilities.concurrency_inspection == "rust"


def test_rust_requires_explicit_language(tmp_path):
    binary = tmp_path / "app"
    binary.write_bytes(b"\x7fELF" + b"\0" * 60)
    assert registry.detect(str(binary)) == "cpp"
    assert registry.resolve("rust").id == "rust"
```

- [ ] **Step 2: Run the new tests and verify the missing module/capability failures**

Run: `PYTHONPATH=src /home/al/venvs/work/bin/python -m pytest tests/unit/test_rust_profile.py tests/unit/test_registry_rust.py -q`

Expected: FAIL because `tdb.languages.rust` and `concurrency_inspection` do not exist.

- [ ] **Step 3: Add the capability and profile**

Add to `ProfileCapabilities`:

```python
concurrency_inspection: str | None = None
```

Implement `rust.py` with small Rust-specific subclasses so later tasks can add attach/probe configuration without changing C/C++:

```python
class RustGdbAdapter(GdbDapAdapter):
    pass


class RustLldbAdapter(LldbDapAdapter):
    pass


def build_rust_profile(adapter=None, adapter_paths=None):
    default = "lldb-dap" if sys.platform == "darwin" else "gdb"
    adapter_id = adapter or default
    adapters = {"gdb": RustGdbAdapter, "lldb-dap": RustLldbAdapter}
    if adapter_id not in adapters:
        raise LanguageNotSupportedError(
            f"unknown adapter {adapter_id!r} for rust "
            f"(known: {', '.join(sorted(adapters))})"
        )
    executable = (adapter_paths or {}).get(adapter_id)
    return LanguageProfile(
        id="rust",
        display_name="Rust",
        adapter=adapters[adapter_id](executable=executable),
        presentation=Presentation(lexer="rust"),
        capabilities=ProfileCapabilities(
            pause_while_running=True,
            concurrency_inspection="rust",
        ),
    )
```

Register `rust` without adding it to `_MAGIC`. Generalize the CLI GDB terminal guard from `profile.id == "cpp"` to `profile.adapter.id == "gdb"` so Rust gets the same early error.

- [ ] **Step 4: Run focused tests**

Run: `PYTHONPATH=src /home/al/venvs/work/bin/python -m pytest tests/unit/test_rust_profile.py tests/unit/test_registry_rust.py tests/unit/test_cpp_profile.py tests/unit/test_cli.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/languages/base.py src/tdb/languages/rust.py src/tdb/languages/registry.py src/tdb/cli.py tests/unit/test_rust_profile.py tests/unit/test_registry_rust.py tests/unit/test_cli.py
git commit -m "feat: add Rust language profile"
```

### Task 2: Adapter-mediated Rust remote attach

**Files:**
- Modify: `src/tdb/languages/rust.py`
- Modify: `src/tdb/languages/base.py`
- Modify: `src/tdb/dap/client.py`
- Modify: `src/tdb/session/controller.py`
- Modify: `src/tdb/cli.py`
- Modify: `src/tdb/server/runner.py`
- Modify: `src/tdb/mcp/session.py`
- Test: `tests/unit/test_rust_profile.py`
- Test: `tests/unit/test_cli.py`
- Test: `tests/unit/test_remote_attach_via_adapter.py`
- Test: `tests/unit/test_dap_attach_pathmappings.py`

**Interfaces:**
- Consumes: `RustGdbAdapter`, `RustLldbAdapter`, and `AdapterQuirks.attach_via_adapter`.
- Produces: `DAPClient.attach(..., program: str | None = None)` and `DebugController.remote_attach(..., program: str | None = None)`.
- Produces attach bodies: GDB `{program, target}`; LLDB `{program, gdb-remote-host, gdb-remote-port, sourceMap?}`.
- Produces `AdapterSpec.pre_configuration_commands(path_mappings) -> tuple[str, ...]`, empty by default and GDB `set substitute-path` commands for Rust.

- [ ] **Step 1: Write failing attach-body and CLI tests**

```python
def test_rust_gdb_attach_body():
    body = RustGdbAdapter().attach_body(
        host="devbox",
        port=2345,
        opts={"program": "/local/app", "path_mappings": [("/src", "/remote/src")]},
    )
    assert body == {"program": "/local/app", "target": "devbox:2345"}


def test_rust_lldb_attach_body_with_source_map():
    body = RustLldbAdapter().attach_body(
        host="devbox",
        port=2345,
        opts={"program": "/local/app", "path_mappings": [("/src", "/remote/src")]},
    )
    assert body["gdb-remote-host"] == "devbox"
    assert body["gdb-remote-port"] == 2345
    assert body["program"] == "/local/app"
    assert body["sourceMap"] == [["/remote/src", "/src"]]


def test_rust_gdb_source_mapping_commands_escape_paths():
    adapter = RustGdbAdapter()
    assert adapter.pre_configuration_commands([("/local src", "/remote src")]) == (
        'set substitute-path "/remote src" "/local src"',
    )


def test_rust_remote_attach_requires_local_program(tmp_path):
    with pytest.raises(SystemExit):
        parse_args(["--lang", "rust", "-r", "host:2345"])
```

- [ ] **Step 2: Run tests to verify failures**

Run: `PYTHONPATH=src /home/al/venvs/work/bin/python -m pytest tests/unit/test_rust_profile.py tests/unit/test_cli.py::test_rust_remote_attach_requires_local_program tests/unit/test_remote_attach_via_adapter.py -q`

Expected: FAIL because Rust attach bodies and `program` threading are absent.

- [ ] **Step 3: Implement program-aware native attach**

Set both Rust adapters' quirks to `AdapterQuirks(attach_via_adapter=True)`. Extend `DAPClient.attach`, `DebugController.remote_attach`, headless/TUI callers, and MCP attach to pass `program` inside `opts`. For Rust only, CLI validation must require and resolve the positional program even in remote mode. Keep Python/Perl/Ruby behavior unchanged.

Use these exact attach-body rules:

```python
class RustGdbAdapter(GdbDapAdapter):
    quirks = AdapterQuirks(attach_via_adapter=True)

    def attach_body(self, *, host, port, opts):
        return {"program": _required_program(opts), "target": f"{host}:{port}"}


class RustLldbAdapter(LldbDapAdapter):
    quirks = AdapterQuirks(attach_via_adapter=True)

    def attach_body(self, *, host, port, opts):
        body = {
            "program": _required_program(opts),
            "gdb-remote-host": host,
            "gdb-remote-port": port,
        }
        mappings = opts.get("path_mappings") or []
        if mappings:
            body["sourceMap"] = [[remote, local] for local, remote in mappings]
        return body
```

Add `AdapterSpec.pre_configuration_commands(path_mappings)` with an empty default. `RustGdbAdapter` returns one `set substitute-path REMOTE LOCAL` command per mapping, using a small GDB-string encoder that wraps each path in double quotes and escapes backslash and double-quote characters. Store attach path mappings on the controller. In `do_configure`, after the adapter emits `initialized` but before sending breakpoints, issue these commands through `client.evaluate(command, context="repl")`. Treat a failed mapping command as attach configuration failure; otherwise breakpoints could silently bind against the wrong source. LLDB uses its attach-body `sourceMap` and returns no pre-configuration commands.

- [ ] **Step 4: Run attach and CLI suites**

Run: `PYTHONPATH=src /home/al/venvs/work/bin/python -m pytest tests/unit/test_rust_profile.py tests/unit/test_cli.py tests/unit/test_remote_attach_via_adapter.py tests/unit/test_dap_attach_pathmappings.py tests/unit/test_runner_adapter_not_found.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/languages/base.py src/tdb/languages/rust.py src/tdb/dap/client.py src/tdb/session/controller.py src/tdb/cli.py src/tdb/server/runner.py src/tdb/mcp/session.py tests/unit/test_rust_profile.py tests/unit/test_cli.py tests/unit/test_remote_attach_via_adapter.py tests/unit/test_dap_attach_pathmappings.py
git commit -m "feat: support Rust native remote attach"
```

### Task 3: Immutable concurrency data model and serialization

**Files:**
- Create: `src/tdb/rust_concurrency/__init__.py`
- Create: `src/tdb/rust_concurrency/models.py`
- Test: `tests/unit/test_rust_concurrency_models.py`

**Interfaces:**
- Produces frozen dataclasses `RawVariable`, `RawFrame`, `RawThread`, `RawSnapshot`, `Evidence`, `ProbeThread`, `ProbePrimitiveState`, `ProbeResult`, `Primitive`, `WaitEdge`, `ThreadAnalysis`, `Finding`, `ConcurrencySnapshot`.
- Produces enums `Confidence`, `ThreadState`, `PrimitiveKind`, `FindingKind`.
- Produces `ConcurrencySnapshot.to_dict() -> dict[str, Any]` with stable JSON-ready fields.

- [ ] **Step 1: Write failing model tests**

```python
def test_snapshot_serialization_is_stable():
    snapshot = ConcurrencySnapshot(
        rust_version="1.98.0",
        adapter="gdb",
        platform="linux",
        threads=(
            ThreadAnalysis(
                thread_id=1, name="main", state=ThreadState.BLOCKED, wait=None
            ),
        ),
        primitives=(),
        edges=(),
        confirmed_deadlocks=(),
        suspected_stalls=(),
        warnings=("stack-only classification",),
    )
    assert snapshot.to_dict()["threads"][0] == {
        "thread_id": 1,
        "name": "main",
        "state": "blocked",
        "wait": None,
    }
    assert snapshot.to_dict()["warnings"] == ["stack-only classification"]


def test_models_are_immutable():
    evidence = Evidence(Confidence.CONFIRMED, "frame-argument", "self=0x10")
    with pytest.raises(FrozenInstanceError):
        evidence.detail = "changed"
```

- [ ] **Step 2: Run tests and verify import failure**

Run: `PYTHONPATH=src /home/al/venvs/work/bin/python -m pytest tests/unit/test_rust_concurrency_models.py -q`

Expected: FAIL because the package does not exist.

- [ ] **Step 3: Implement the exact model surface**

Use `@dataclass(frozen=True)` and tuple collections. Important field signatures:

```python
class Confidence(str, Enum):
    CONFIRMED = "confirmed"
    PROBABLE = "probable"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Evidence:
    confidence: Confidence
    source: str
    detail: str


@dataclass(frozen=True)
class RawVariable:
    name: str
    value: str
    type_name: str
    evaluate_name: str | None = None


@dataclass(frozen=True)
class RawFrame:
    frame_id: int
    name: str
    source_path: str | None
    line: int
    variables: tuple[RawVariable, ...] = ()


@dataclass(frozen=True)
class RawThread:
    thread_id: int
    name: str
    frames: tuple[RawFrame, ...]
    os_thread_id: str | None = None


@dataclass(frozen=True)
class RawSnapshot:
    adapter: str
    platform: str
    rust_version: str | None
    threads: tuple[RawThread, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProbeThread:
    dap_thread_hint: str
    os_thread_id: str


@dataclass(frozen=True)
class ProbePrimitiveState:
    primitive_id: str
    owner_os_thread_ids: tuple[str, ...]
    raw_state: str
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class ProbeResult:
    rust_version: str | None
    threads: tuple[ProbeThread, ...] = ()
    primitive_states: tuple[ProbePrimitiveState, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class WaitEdge:
    waiter_thread_id: int
    primitive_id: str
    owner_thread_id: int | None
    operation: str
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class Primitive:
    primitive_id: str
    kind: PrimitiveKind
    address: str | None
    label: str
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class ThreadAnalysis:
    thread_id: int
    name: str
    state: ThreadState
    wait: WaitEdge | None


@dataclass(frozen=True)
class Finding:
    kind: FindingKind
    thread_ids: tuple[int, ...]
    summary: str
    evidence_gaps: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConcurrencySnapshot:
    rust_version: str | None
    adapter: str
    platform: str
    threads: tuple[ThreadAnalysis, ...]
    primitives: tuple[Primitive, ...]
    edges: tuple[WaitEdge, ...]
    confirmed_deadlocks: tuple[Finding, ...]
    suspected_stalls: tuple[Finding, ...]
    warnings: tuple[str, ...]
```

Implement explicit `to_dict` methods rather than `asdict` so enum values and schema names stay stable.

- [ ] **Step 4: Run model tests**

Run: `PYTHONPATH=src /home/al/venvs/work/bin/python -m pytest tests/unit/test_rust_concurrency_models.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/rust_concurrency tests/unit/test_rust_concurrency_models.py
git commit -m "feat: define Rust concurrency snapshot model"
```

### Task 4: Portable Rust stack classifier

**Files:**
- Create: `src/tdb/rust_concurrency/classifier.py`
- Test: `tests/unit/test_rust_concurrency_classifier.py`
- Test fixtures: `tests/fixtures/rust_concurrency/stacks.json`

**Interfaces:**
- Consumes: `RawThread`, `RawFrame`, and `RawVariable` from Task 3.
- Produces: `classify_thread(thread: RawThread) -> ThreadAnalysis`.
- Produces: `classify_snapshot(raw: RawSnapshot) -> tuple[tuple[ThreadAnalysis, ...], tuple[Primitive, ...], tuple[WaitEdge, ...]]`.

- [ ] **Step 1: Add table-driven failing tests for every primitive**

```python
@pytest.mark.parametrize(
    "frame_name,expected_operation,expected_kind",
    [
        ("std::thread::JoinHandle<T>::join", "join", PrimitiveKind.THREAD),
        ("std::sync::poison::mutex::Mutex<T>::lock", "mutex-lock", PrimitiveKind.MUTEX),
        (
            "std::sync::poison::rwlock::RwLock<T>::read",
            "rwlock-read",
            PrimitiveKind.RWLOCK,
        ),
        (
            "std::sync::poison::rwlock::RwLock<T>::write",
            "rwlock-write",
            PrimitiveKind.RWLOCK,
        ),
        (
            "std::sync::poison::condvar::Condvar::wait",
            "condvar-wait",
            PrimitiveKind.CONDVAR,
        ),
        ("std::sync::mpsc::Receiver<T>::recv", "mpsc-recv", PrimitiveKind.CHANNEL),
        ("std::sync::mpsc::SyncSender<T>::send", "mpsc-send", PrimitiveKind.CHANNEL),
        ("std::thread::park", "park", PrimitiveKind.PARKER),
    ],
)
def test_classifies_supported_waits(frame_name, expected_operation, expected_kind):
    analysis, primitives, edges = classify_snapshot(raw_with_top_frame(frame_name))
    assert edges[0].operation == expected_operation
    assert primitives[0].kind is expected_kind
```

In the same test module, define `raw_with_top_frame(name)` as a `RawSnapshot` containing one `RawThread` and one `RawFrame`. Add negative cases for application functions containing words like `park`, naked futex frames, and unfamiliar mangled names.

- [ ] **Step 2: Run classifier tests to verify failure**

Run: `PYTHONPATH=src /home/al/venvs/work/bin/python -m pytest tests/unit/test_rust_concurrency_classifier.py -q`

Expected: FAIL because classifier functions are absent.

- [ ] **Step 3: Implement ordered frame rules and conservative address extraction**

Define one anchored regex table, most-specific first. Normalize legacy and v0 demangled generic suffixes before matching. Accept an address only from a typed variable whose value contains a full hexadecimal token matching `0x[0-9a-fA-F]+`; never infer an address from arbitrary digits.

When a Rust frame is recognized but no primitive address exists, generate a stable per-thread primitive ID such as `unknown:mutex:<thread-id>` and attach `Evidence(UNKNOWN, "stack", frame.name)`. A platform futex/pthread/ulock frame may raise confidence only when a Rust frame already identified the primitive class.

- [ ] **Step 4: Run classifier and model tests**

Run: `PYTHONPATH=src /home/al/venvs/work/bin/python -m pytest tests/unit/test_rust_concurrency_classifier.py tests/unit/test_rust_concurrency_models.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/rust_concurrency/classifier.py tests/unit/test_rust_concurrency_classifier.py tests/fixtures/rust_concurrency/stacks.json
git commit -m "feat: classify Rust standard-library waits"
```

### Task 5: Wait graph, confirmed deadlocks, and suspected stalls

**Files:**
- Create: `src/tdb/rust_concurrency/analyzer.py`
- Test: `tests/unit/test_rust_concurrency_analyzer.py`

**Interfaces:**
- Consumes classifier output and supplemental `ProbeResult` evidence.
- Produces `analyze(raw: RawSnapshot, probe: ProbeResult | None = None) -> ConcurrencySnapshot`.
- Produces `find_confirmed_cycles(edges: tuple[WaitEdge, ...]) -> tuple[Finding, ...]`.
- Produces `find_suspected_stalls(threads, edges, confirmed) -> tuple[Finding, ...]`.

- [ ] **Step 1: Write failing graph tests**

```python
def test_confirmed_cycle_requires_every_confirmed_edge():
    snapshot = analyze(raw_two_mutex_cycle(confidences=("confirmed", "confirmed")))
    assert len(snapshot.confirmed_deadlocks) == 1
    assert snapshot.suspected_stalls == ()


def test_probable_cycle_is_suspected_not_confirmed():
    snapshot = analyze(raw_two_mutex_cycle(confidences=("confirmed", "probable")))
    assert snapshot.confirmed_deadlocks == ()
    assert snapshot.suspected_stalls[0].kind is FindingKind.SUSPECTED_CYCLE


def test_all_application_threads_blocked_is_whole_program_stall():
    snapshot = analyze(raw_all_blocked_with_unknown_owners())
    assert snapshot.suspected_stalls[0].kind is FindingKind.WHOLE_PROGRAM_STALL


def test_runtime_housekeeping_thread_does_not_prevent_stall():
    snapshot = analyze(
        raw_with_housekeeping_thread("lldb.process.internal-state-coordinator")
    )
    assert snapshot.suspected_stalls
```

Define the four `raw_*` helpers immediately above these tests using the Task 3 dataclasses; each helper must construct its owner/wait evidence explicitly rather than mocking analyzer internals.

- [ ] **Step 2: Run analyzer tests to verify failure**

Run: `PYTHONPATH=src /home/al/venvs/work/bin/python -m pytest tests/unit/test_rust_concurrency_analyzer.py -q`

Expected: FAIL because analyzer functions are absent.

- [ ] **Step 3: Implement deterministic graph analysis**

Build adjacency from thread → primitive wait edges and primitive → owner thread links. Use a deterministic depth-first search over sorted node IDs. A confirmed finding requires every traversed edge's strongest evidence to be `CONFIRMED`. Probable cycles and all-blocked snapshots become suspected findings with evidence-gap summaries.

Define housekeeping names in a versioned constant keyed by `(platform, adapter)` and exclude only exact or anchored matches. Unknown threads remain application threads.

- [ ] **Step 4: Run analyzer, classifier, and wait-graph regression tests**

Run: `PYTHONPATH=src /home/al/venvs/work/bin/python -m pytest tests/unit/test_rust_concurrency_analyzer.py tests/unit/test_rust_concurrency_classifier.py tests/unit/test_wait_graph.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/rust_concurrency/analyzer.py tests/unit/test_rust_concurrency_analyzer.py
git commit -m "feat: analyze Rust deadlocks and stalls"
```

### Task 6: Bounded stopped-state DAP collector

**Files:**
- Create: `src/tdb/rust_concurrency/collector.py`
- Modify: `src/tdb/session/inspect_service.py`
- Test: `tests/unit/test_rust_concurrency_collector.py`
- Test: `tests/unit/test_inspect_service.py`

**Interfaces:**
- Consumes: `DAPClient.threads`, `stack_trace`, `scopes`, and `variables`.
- Produces: `RustConcurrencyCollector.collect(controller: DebugController) -> RawSnapshot`.
- Produces: `InspectService.collect_rust_concurrency() -> ConcurrencySnapshot`.

- [ ] **Step 1: Write failing collector tests**

```python
async def test_collector_fetches_bounded_frames_for_every_thread(fake_controller):
    result = await RustConcurrencyCollector(max_frames=32, max_variables=128).collect(
        fake_controller
    )
    assert [t.thread_id for t in result.threads] == [1, 2]
    fake_controller.client.stack_trace.assert_any_await(1, start_frame=0, levels=32)


async def test_collector_discards_result_if_session_resumes(fake_controller):
    fake_controller.state.transition_to(SessionPhase.RUNNING)
    with pytest.raises(SessionGateError, match="running"):
        await InspectService(lambda: fake_controller).collect_rust_concurrency()


async def test_probe_timeout_preserves_base_snapshot(fake_controller, slow_probe):
    collector = RustConcurrencyCollector(probe=slow_probe, probe_timeout=0.01)
    snapshot = await collector.collect_and_analyze(fake_controller)
    assert snapshot.threads
    assert "probe timed out" in snapshot.warnings[0]
```

- [ ] **Step 2: Run tests to verify missing collector failures**

Run: `PYTHONPATH=src /home/al/venvs/work/bin/python -m pytest tests/unit/test_rust_concurrency_collector.py tests/unit/test_inspect_service.py -q`

Expected: FAIL because collection entry points are absent.

- [ ] **Step 3: Implement bounded concurrent collection**

Use `asyncio.TaskGroup` to fetch per-thread stacks with fixed ceilings. Fetch scopes/variables only for frames selected by the classifier's cheap frame-name prefilter; cap total expanded variables. Capture the controller session phase/generation before collection and re-check before returning. Convert per-thread failures to warnings; propagate running/terminated/capability gates.

Add:

```python
def _require_concurrency_inspection(self) -> None:
    if self._ctrl.profile.capabilities.concurrency_inspection != "rust":
        raise SessionGateError("unsupported")


async def collect_rust_concurrency(self) -> ConcurrencySnapshot:
    self._require_concurrency_inspection()
    self._gate()
    return await self._rust_collector.collect_and_analyze(self._ctrl)
```

- [ ] **Step 4: Run collector and inspection tests**

Run: `PYTHONPATH=src /home/al/venvs/work/bin/python -m pytest tests/unit/test_rust_concurrency_collector.py tests/unit/test_inspect_service.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/rust_concurrency/collector.py src/tdb/session/inspect_service.py tests/unit/test_rust_concurrency_collector.py tests/unit/test_inspect_service.py
git commit -m "feat: collect stopped Rust concurrency snapshots"
```

### Task 7: GDB evidence probe and Rust-version gate

**Files:**
- Create: `src/tdb/rust_concurrency/probes/__init__.py`
- Create: `src/tdb/rust_concurrency/probes/base.py`
- Create: `src/tdb/rust_concurrency/probes/gdb.py`
- Create: `src/tdb/rust_concurrency/probes/gdb_script.py`
- Modify: `src/tdb/languages/rust.py`
- Test: `tests/unit/test_rust_gdb_probe.py`
- Test fixtures: `tests/fixtures/rust_concurrency/gdb/*.json`

**Interfaces:**
- Consumes: `ProbeResult(rust_version, threads, primitive_states, warnings)` from Task 3.
- Produces: `EvidenceProbe.collect(client: DAPClient) -> ProbeResult`.
- Produces fixed debugger command `tdb-rust-snapshot --format json`.

- [ ] **Step 1: Write failing envelope and version tests**

```python
def test_gdb_probe_parses_marker_wrapped_json():
    raw = 'console noise\nTDB_RUST_JSON:{"rust_version":"1.98.0","threads":[]}\n'
    result = parse_probe_output(raw)
    assert result.rust_version == "1.98.0"


def test_unsupported_rust_version_disables_layout_evidence():
    result = parse_probe_output(load_fixture("gdb/rust-1.97.json"))
    gated = gate_supported_layout(result, supported="1.98.0")
    assert gated.primitive_states == ()
    assert "unsupported Rust 1.97.0" in gated.warnings[0]
```

Define `load_fixture(name)` in this test module as `(Path(__file__).parents[1] / "fixtures" / "rust_concurrency" / name).read_text()`.

- [ ] **Step 2: Run GDB probe tests to verify failure**

Run: `PYTHONPATH=src /home/al/venvs/work/bin/python -m pytest tests/unit/test_rust_gdb_probe.py -q`

Expected: FAIL because probe modules are absent.

- [ ] **Step 3: Implement fixed-command GDB integration**

`gdb_script.py` registers a `gdb.Command("tdb-rust-snapshot", ...)`. It reads `gdb.selected_inferior().threads()`, compile-unit `DW_AT_producer` metadata, thread PTIDs, frames, and values through GDB's Python API, then prints exactly one `TDB_RUST_JSON:` line. It must not resume the inferior or invoke inferior functions. `ProbeResult` and its nested records come from Task 3; this task parses into those types rather than defining a second envelope.

Add the script through `RustGdbAdapter.command()` using `-iex`, before `-i dap`:

```python
return [exe, "-iex", f"source {script_path}", "-i", "dap"]
```

The outer probe sends only `tdb-rust-snapshot --format json` with DAP `evaluate(context="repl")`, extracts the marker line, validates types and hexadecimal addresses, and gates layout evidence to the supported Rust version. Invalid envelopes return warnings plus empty supplemental evidence.

Add `probe_for_adapter("gdb") -> GdbEvidenceProbe` in `probes/__init__.py`; other IDs return `None`. Update `RustConcurrencyCollector` to use this factory when no probe was injected by a test.

- [ ] **Step 4: Run probe and profile tests**

Run: `PYTHONPATH=src /home/al/venvs/work/bin/python -m pytest tests/unit/test_rust_gdb_probe.py tests/unit/test_rust_profile.py tests/unit/test_cpp_profile.py -q`

Expected: PASS; C/C++ GDB argv remains unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/rust_concurrency/probes src/tdb/languages/rust.py tests/unit/test_rust_gdb_probe.py tests/fixtures/rust_concurrency/gdb
git commit -m "feat: add read-only GDB Rust evidence probe"
```

### Task 8: LLDB evidence probe

**Files:**
- Create: `src/tdb/rust_concurrency/probes/lldb.py`
- Create: `src/tdb/rust_concurrency/probes/lldb_script.py`
- Modify: `src/tdb/languages/rust.py`
- Test: `tests/unit/test_rust_lldb_probe.py`
- Test fixtures: `tests/fixtures/rust_concurrency/lldb/*.json`

**Interfaces:**
- Consumes: `ProbeResult`, envelope validation, and version gate from Task 7.
- Produces: `LldbEvidenceProbe.collect(client: DAPClient) -> ProbeResult` using the same JSON schema as GDB.

- [ ] **Step 1: Write failing LLDB parser and launch-body tests**

```python
def test_lldb_probe_uses_common_schema():
    result = parse_lldb_probe_output(load_fixture("lldb/rust-1.98.json"))
    assert result.rust_version == "1.98.0"
    assert result.os_thread_ids[0].dap_hint == "thread #1"


def test_rust_lldb_loads_probe_script_before_launch():
    body = RustLldbAdapter().launch_body(
        program="/app",
        args=[],
        cwd="/",
        env=None,
        stop_on_entry=True,
        console="internalConsole",
        opts={},
    )
    assert body["initCommands"] == [f"command script import {expected_script_path()}"]
```

Define `expected_script_path()` with `resources.files("tdb.rust_concurrency.probes").joinpath("lldb_script.py")`; reuse the same `load_fixture` path helper as the GDB test module.

- [ ] **Step 2: Run tests to verify failure**

Run: `PYTHONPATH=src /home/al/venvs/work/bin/python -m pytest tests/unit/test_rust_lldb_probe.py -q`

Expected: FAIL because LLDB probe support is absent.

- [ ] **Step 3: Implement LLDB script and adapter setup**

`lldb_script.py` registers `tdb-rust-snapshot` through `__lldb_init_module`. It uses only `SBTarget`, `SBProcess`, `SBThread`, `SBFrame`, `SBValue`, and module compile-unit APIs while stopped, and emits the exact Task 7 JSON envelope.

`RustLldbAdapter.launch_body` and `attach_body` add `initCommands` with `command script import <absolute packaged path>`. Preserve existing `runInTerminal` behavior. The outer parser delegates marker extraction and schema validation to `base.py`.

Extend `probe_for_adapter("lldb-dap") -> LldbEvidenceProbe` and add a collector test proving the factory selects each adapter-specific probe.

- [ ] **Step 4: Run both probe suites and profile tests**

Run: `PYTHONPATH=src /home/al/venvs/work/bin/python -m pytest tests/unit/test_rust_lldb_probe.py tests/unit/test_rust_gdb_probe.py tests/unit/test_rust_profile.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/rust_concurrency/probes/lldb.py src/tdb/rust_concurrency/probes/lldb_script.py src/tdb/languages/rust.py tests/unit/test_rust_lldb_probe.py tests/fixtures/rust_concurrency/lldb
git commit -m "feat: add read-only LLDB Rust evidence probe"
```

### Task 9: Structured JSON-RPC and MCP concurrency surface

**Files:**
- Modify: `src/tdb/server/rpc_types.py`
- Modify: `src/tdb/server/handlers.py`
- Modify: `src/tdb/mcp/server.py`
- Test: `tests/unit/test_rpc_types.py`
- Test: `tests/unit/test_rpc_handlers.py`
- Test: `tests/unit/test_mcp_server.py`

**Interfaces:**
- Consumes: `InspectService.collect_rust_concurrency()` and `ConcurrencySnapshot.to_dict()`.
- Produces RPC action `rust_concurrency` with `params: []`.
- Produces MCP tool `rust_concurrency() -> str` returning JSON.
- Produces `RpcResponse.data: dict[str, Any] | None` and `RpcResponse.ok_data(data)` while preserving `value` for existing clients.

- [ ] **Step 1: Write failing response and action tests**

```python
def test_rpc_response_ok_data_keeps_legacy_value():
    rsp = RpcResponse.ok_data({"threads": []})
    assert rsp.data == {"threads": []}
    assert json.loads(rsp.value) == {"threads": []}


async def test_rust_concurrency_action_returns_structured_snapshot(
    handlers, monkeypatch
):
    monkeypatch.setattr(
        handlers._inspect,
        "collect_rust_concurrency",
        AsyncMock(return_value=sample_snapshot()),
    )
    rsp = await handlers.action_rust_concurrency([])
    assert rsp.success is True
    assert rsp.data["threads"][0]["name"] == "main"


async def test_rust_concurrency_action_gates_non_rust(handlers):
    rsp = await handlers.action_rust_concurrency([])
    assert rsp.success is False
    assert "Not supported" in rsp.value
```

Define `sample_snapshot()` in the test module from the exact Task 3 constructors; do not mock `to_dict()`.

- [ ] **Step 2: Run transport tests to verify failure**

Run: `PYTHONPATH=src /home/al/venvs/work/bin/python -m pytest tests/unit/test_rpc_types.py tests/unit/test_rpc_handlers.py tests/unit/test_mcp_server.py -q`

Expected: FAIL because `data`, action, and tool are absent.

- [ ] **Step 3: Implement backwards-compatible structured transport**

Add optional `data` and:

```python
@classmethod
def ok_data(cls, data: dict[str, Any]) -> RpcResponse:
    return cls(
        timestamp=datetime.now(timezone.utc).isoformat(),
        success=True,
        value=json.dumps(data, sort_keys=True),
        data=data,
    )
```

Register `rust_concurrency` in `ACTIONS`, help, and `dispatch_table`. The handler rejects params, awaits the service, and returns `ok_data(snapshot.to_dict())`. Add an MCP wrapper whose `_format` returns `json.dumps(rsp.data, sort_keys=True)` when data exists. Do not rename or repurpose Python's existing `wait_graph` tool.

- [ ] **Step 4: Run RPC/MCP suites**

Run: `PYTHONPATH=src /home/al/venvs/work/bin/python -m pytest tests/unit/test_rpc_types.py tests/unit/test_rpc_handlers.py tests/unit/test_mcp_server.py tests/integration/test_rpc_basic.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/server/rpc_types.py src/tdb/server/handlers.py src/tdb/mcp/server.py tests/unit/test_rpc_types.py tests/unit/test_rpc_handlers.py tests/unit/test_mcp_server.py
git commit -m "feat: expose Rust concurrency snapshots over RPC and MCP"
```

### Task 10: Rust Concurrency workspace

**Files:**
- Create: `src/tdb/widgets/rust_concurrency_modal.py`
- Modify: `src/tdb/app_handlers/ui_panels.py`
- Modify: `src/tdb/app_handlers/inspection.py`
- Modify: `src/tdb/app_handlers/routing.py`
- Modify: `src/tdb/app_handlers/dap_events.py`
- Test: `tests/unit/test_rust_concurrency_modal.py`
- Test: `tests/unit/test_inspection_workflows.py`
- Test: `tests/unit/test_event_handler.py`

**Interfaces:**
- Consumes: `ConcurrencySnapshot` and existing stack/source/locals navigation.
- Produces: `RustConcurrencyModal(snapshot, current_thread_id)`.
- Produces messages `RefreshSnapshot`, `SelectThread(thread_id)`, and `SelectFrame(thread_id, frame_id)`.
- Produces `UIPanels.rust_concurrency: RustConcurrencyModal | None`.

- [ ] **Step 1: Write failing widget and routing tests**

```python
async def test_modal_has_three_tabs(app):
    modal = RustConcurrencyModal(sample_snapshot(), current_thread_id=1)
    await app.push_screen(modal)
    assert modal.query_one("#threads-tab")
    assert modal.query_one("#wait-graph-tab")
    assert modal.query_one("#findings-tab")


async def test_rust_threads_action_opens_concurrency_workspace(
    workflow, rust_controller
):
    workflow.app.controller = rust_controller
    await workflow.open_threads()
    assert isinstance(workflow.app.panels.rust_concurrency, RustConcurrencyModal)
    assert workflow.app.panels.threads is None


def test_continued_dismisses_rust_workspace(dap_handler, modal):
    dap_handler.app.panels.rust_concurrency = modal
    dap_handler.on_continued()
    modal.dismiss.assert_called_once()
```

Define `sample_snapshot()` in the widget test module from Task 3 model constructors. Build `rust_controller` with `build_rust_profile(adapter="lldb-dap")` and a stopped `SessionPhase`; do not detect Rust from a fake binary.

- [ ] **Step 2: Run UI tests to verify missing widget failures**

Run: `PYTHONPATH=src /home/al/venvs/work/bin/python -m pytest tests/unit/test_rust_concurrency_modal.py tests/unit/test_inspection_workflows.py -q`

Expected: FAIL because the modal and panel slot are absent.

- [ ] **Step 3: Implement the three-tab modal**

Use Textual `TabbedContent` with stable IDs `threads-tab`, `wait-graph-tab`, and `findings-tab`:

- Threads tab: `DataTable` with ID/name/state/wait columns; detail pane with evidence; existing `VariableView` for selected frame locals.
- Wait Graph tab: `Tree` for keyboard navigation plus a `Static` textual edge list. Render confidence as explicit words and styles.
- Findings tab: confirmed deadlocks, suspected cycles, then whole-program stalls; every row includes summary and evidence gaps.

Bindings: `escape` close, `r` refresh, `enter` select, arrow/tab navigation. Refresh replaces all three tab models in one `update_snapshot(snapshot)` call.

Route Rust's existing Threads action through `InspectService.collect_rust_concurrency`; preserve `ThreadsModal` for every other profile. On refresh, use the same service call. On continue/step/restart/termination, dismiss the live Rust modal and clear the panel reference before updating UI state.

- [ ] **Step 4: Run workspace and existing modal tests**

Run: `PYTHONPATH=src /home/al/venvs/work/bin/python -m pytest tests/unit/test_rust_concurrency_modal.py tests/unit/test_inspection_workflows.py tests/unit/test_event_handler.py tests/unit/test_app_helpers.py -q`

Expected: PASS, including existing Threads behavior for Python/C++.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/widgets/rust_concurrency_modal.py src/tdb/app_handlers/ui_panels.py src/tdb/app_handlers/inspection.py src/tdb/app_handlers/routing.py src/tdb/app_handlers/dap_events.py tests/unit/test_rust_concurrency_modal.py tests/unit/test_inspection_workflows.py tests/unit/test_event_handler.py
git commit -m "feat: add Rust concurrency workspace"
```

### Task 11: Real Rust adapter, mode, and concurrency integrations

**Files:**
- Create: `tests/integration/rust_adapter_harness.py`
- Create: `tests/integration/fixtures/rust_concurrency.rs`
- Create: `tests/integration/test_rust_adapter_launch.py`
- Create: `tests/integration/test_rust_concurrency.py`
- Create: `tests/integration/test_rust_run_mode.py`
- Create: `tests/integration/test_rust_terminal.py`
- Create: `tests/integration/test_rust_remote_attach.py`

**Interfaces:**
- Consumes all production interfaces from Tasks 1–10.
- Produces shared fixture `rust_debug_binary(case: str, adapter: str)` built with `rustc -C debuginfo=2 -C opt-level=0`.

- [ ] **Step 1: Add a deterministic multi-scenario Rust fixture**

The fixture accepts one argument selecting `join`, `mutex`, `rwlock-read`, `rwlock-write`, `condvar`, `mpsc-send`, `mpsc-recv`, `park`, `cycle`, `incomplete-cycle`, or `healthy-blocked`. Each scenario prints `READY:<scenario>` after all worker threads reach barriers, then blocks without timing races. Use `Barrier` and channels for readiness; do not use sleeps as correctness synchronization.

- [ ] **Step 2: Write real-adapter tests before completing missing integration behavior**

```python
@pytest.mark.parametrize("adapter", available_rust_adapters())
async def test_mutex_snapshot_identifies_wait(adapter, rust_debug_binary):
    ctrl = await launch_and_pause(rust_debug_binary("mutex"), adapter)
    snapshot = await InspectService(lambda: ctrl).collect_rust_concurrency()
    assert any(edge.operation == "mutex-lock" for edge in snapshot.edges)


@pytest.mark.parametrize("adapter", available_rust_adapters())
async def test_run_mode_pauses_blocked_rust_program(adapter, rust_debug_binary):
    assert await run_mode_pause_probe(rust_debug_binary("park"), adapter)
```

Implement `available_rust_adapters()` in `rust_adapter_harness.py` from `shutil.which("gdb")`, the parsed GDB major version, `shutil.which("lldb-dap")`, and `sys.platform`. Implement `launch_and_pause()` with the same initialize/configure/pause sequence as `tests/integration/test_cpp_pause.py`. Implement `run_mode_pause_probe()` by calling `tdb.run_mode.run(..., on_session_ready=...)` and triggering the controller pause through the supplied callback; no fixed sleep may determine readiness.

Add one test per fixture scenario, exact no-false-positive assertions for `healthy-blocked`, LLDB-only terminal launch through a fake `runInTerminal` client, and adapter-mediated remote tests using `gdbserver`/`lldb-server` when installed.

- [ ] **Step 3: Run integrations and record genuine unsupported evidence as warnings**

Run: `PYTHONPATH=src /home/al/venvs/work/bin/python -m pytest tests/integration/test_rust_adapter_launch.py tests/integration/test_rust_concurrency.py tests/integration/test_rust_run_mode.py tests/integration/test_rust_terminal.py tests/integration/test_rust_remote_attach.py -v`

Expected: Tests either PASS or SKIP with an explicit missing-tool reason. Any semantic failure must be resolved in the owning production task; do not weaken confirmed/probable assertions without saving the observed debugger evidence as a fixture and documenting why confidence is lower.

- [ ] **Step 4: Run native-language regression integrations**

Run: `PYTHONPATH=src /home/al/venvs/work/bin/python -m pytest tests/integration/test_cpp_session.py tests/integration/test_cpp_pause.py tests/integration/test_gdb_session.py tests/integration/test_rust_adapter_launch.py tests/integration/test_rust_concurrency.py -v`

Expected: PASS/SKIP only, zero failures.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/rust_adapter_harness.py tests/integration/fixtures/rust_concurrency.rs tests/integration/test_rust_adapter_launch.py tests/integration/test_rust_concurrency.py tests/integration/test_rust_run_mode.py tests/integration/test_rust_terminal.py tests/integration/test_rust_remote_attach.py
git commit -m "test: cover Rust debugging and concurrency modes"
```

### Task 12: Documentation, packaging, and full verification

**Files:**
- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `examples/README.md`
- Modify: `.gitignore`
- Test: `tests/unit/test_rust_profile.py`

**Interfaces:**
- Consumes the completed user-facing behavior.
- Produces packaged GDB/LLDB scripts and user documentation.

- [ ] **Step 1: Add a failing package-data test**

```python
def test_probe_scripts_are_package_resources():
    root = resources.files("tdb.rust_concurrency.probes")
    assert root.joinpath("gdb_script.py").is_file()
    assert root.joinpath("lldb_script.py").is_file()
```

- [ ] **Step 2: Run the package test against a wheel**

Run: `uv build && uv run python -c "import zipfile,glob; p=glob.glob('dist/*.whl')[-1]; z=zipfile.ZipFile(p); assert 'tdb/rust_concurrency/probes/gdb_script.py' in z.namelist(); assert 'tdb/rust_concurrency/probes/lldb_script.py' in z.namelist()"`

Expected: FAIL if package discovery/data does not include either probe script.

- [ ] **Step 3: Finalize packaging and documentation**

Ensure setuptools includes `tdb.rust_concurrency` packages and both scripts. Add `.superpowers/` to `.gitignore` so visual-companion artifacts cannot be committed accidentally.

Document:

```text
cargo build
tdb --lang rust target/debug/app
tdb --lang rust --adapter lldb-dap --run target/debug/app
tdb --lang rust --adapter lldb-dap --terminal xterm target/debug/app
tdb --lang rust --adapter gdb --remote-attach host:2345 target/debug/app
```

Include the platform/mode matrix, exact local-symbol requirement for remote attach, `rustc -C debuginfo=2 -C opt-level=0`, current-stable-only policy, confidence meanings, suspected-stall semantics, SSH-tunnel recommendation, and helper-crate future direction.

- [ ] **Step 4: Run focused and full verification**

Run:

```bash
PYTHONPATH=src /home/al/venvs/work/bin/python -m pytest tests/unit/test_rust_profile.py tests/unit/test_rust_concurrency_models.py tests/unit/test_rust_concurrency_classifier.py tests/unit/test_rust_concurrency_analyzer.py tests/unit/test_rust_concurrency_collector.py tests/unit/test_rust_gdb_probe.py tests/unit/test_rust_lldb_probe.py tests/unit/test_rust_concurrency_modal.py -q
PYTHONPATH=src /home/al/venvs/work/bin/python -m pytest -q
git diff --check
uv build
```

Expected: all tests pass, `git diff --check` emits no output, and wheel/sdist builds succeed.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml README.md examples/README.md .gitignore tests/unit/test_rust_profile.py
git commit -m "docs: document Rust debugger support"
```

- [ ] **Step 6: Request final code review**

Use `superpowers:requesting-code-review` against the complete diff. Address only verified findings, rerun the full verification commands, then use `superpowers:finishing-a-development-branch` to choose merge/PR/cleanup handling.

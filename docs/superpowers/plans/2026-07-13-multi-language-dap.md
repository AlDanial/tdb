# Multi-Language DAP Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make tdb debug any language with a DAP adapter — C++ via lldb-dap first, `gdb -i dap` as an alternate — by extracting a `LanguageProfile` abstraction, with Python remaining the reference profile at zero behavior change.

**Architecture:** A `LanguageProfile` (in new package `src/tdb/languages/`) bundles three sub-objects, each with exactly one consumer: `adapter: AdapterSpec` (spawn command, launch/attach bodies, exception filters, quirks — consumed by `dap/client.py` + `session/controller.py`), `presentation` (lexer — consumed by `code_view`), and `capabilities` (statement stepping, task inspection, child processes — feature gates). A registry auto-detects the language from the target (extension / binary magic bytes) with `--lang`/`--adapter` overrides. Spec: `docs/superpowers/specs/2026-07-13-multi-language-dap-design.md`.

**Tech Stack:** Python 3.11+, textual, debugpy, lldb-dap (LLVM), gdb ≥ 14, pytest + pytest-asyncio (auto mode).

## Global Constraints

- Repo root for all commands: `/home/al/projects/tdbg/work`. Only search under `/home/al/projects/tdbg`.
- Use `uv pip install` (never bare `pip install`) if a dependency is ever needed — none should be.
- Run unit tests with `pytest tests/unit -q`; integration with `pytest tests/integration -q`. Full suite must pass before every commit.
- Tasks 1–5 are the Phase-1 refactor: **all existing tests must pass unmodified**. From Task 11 on, updating existing tests is allowed when a signature deliberately changes.
- Profiles are data + pure functions: a module under `src/tdb/languages/` must never import `tdb.app`, `tdb.widgets.*`, or `tdb.session.controller`.
- Conventional-commit messages (`feat:`, `test:`, `refactor:`). One commit per task minimum.
- A PostToolUse hook reformats files after Write/Edit — do not fight formatting differences; re-read the file if an Edit fails to match.
- Cross-platform: no POSIX-only assumptions in new path handling (magic-byte detection must include PE `MZ`).

---

### Task 1: `languages/base.py` — the profile datatypes

**Files:**
- Create: `src/tdb/languages/__init__.py`
- Create: `src/tdb/languages/base.py`
- Test: `tests/unit/test_languages_base.py`

**Interfaces:**
- Consumes: `tdb.dap.types.Capabilities` (existing dataclass).
- Produces: `AdapterNotFoundError(hint)`, `LanguageNotSupportedError`, `AdapterQuirks(pre_arm_pause_on_attach: bool = False)`, `AdapterSpec` (base class: `id: str`, `quirks`, `command() -> list[str]`, `launch_body(*, program, args, cwd, env, stop_on_entry, console, opts) -> dict`, `attach_body(*, host, port, opts) -> dict`, `pick_exception_filters(caps) -> list[str]`), `Presentation(lexer: str = "text")`, `ProfileCapabilities(compute_step_units: Callable[[str], list[tuple[int, int]]] | None = None, child_process_strategy: str | None = None, task_inspection: bool = False)`, `LanguageProfile(id, display_name, adapter, presentation, capabilities)`. Every later task depends on these exact names.
- Note: the spec's `presentation.decode_evaluate_result` is deliberately **omitted** — every consumer of debugpy repr-decoding (`?` help, task collection) is itself a Python-gated feature, so no generic decode hook is needed (YAGNI). The spec's `AdapterQuirks.cold_start_timeout` is likewise omitted: lldb-dap and gdb start faster than debugpy, so the existing `_timeouts.py` defaults already cover them; add the knob only if a future adapter needs it.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_languages_base.py
"""LanguageProfile datatypes: defaults, immutability, filter picking."""

import pytest

from tdb.dap.types import Capabilities
from tdb.languages.base import (
    AdapterNotFoundError,
    AdapterQuirks,
    AdapterSpec,
    LanguageProfile,
    Presentation,
    ProfileCapabilities,
)


class _StubAdapter(AdapterSpec):
    id = "stub"

    def command(self):
        return ["stub-adapter"]

    def launch_body(self, *, program, args, cwd, env, stop_on_entry, console, opts):
        return {"request": "launch", "program": program}

    def attach_body(self, *, host, port, opts):
        return {"request": "attach"}


def _profile() -> LanguageProfile:
    return LanguageProfile(
        id="stub",
        display_name="Stub",
        adapter=_StubAdapter(),
        presentation=Presentation(),
        capabilities=ProfileCapabilities(),
    )


def test_capability_defaults_are_all_off():
    caps = ProfileCapabilities()
    assert caps.compute_step_units is None
    assert caps.child_process_strategy is None
    assert caps.task_inspection is False


def test_quirks_default_off():
    assert AdapterQuirks().pre_arm_pause_on_attach is False
    assert AdapterSpec.quirks.pre_arm_pause_on_attach is False


def test_presentation_default_lexer_is_text():
    assert Presentation().lexer == "text"


def test_profile_is_frozen():
    profile = _profile()
    with pytest.raises(AttributeError):
        profile.id = "other"


def test_default_exception_filters_picks_adapter_defaults():
    caps = Capabilities()
    caps.exception_breakpoint_filters = [
        {"filter": "throw", "label": "On throw", "default": False},
        {"filter": "uncaught", "label": "Uncaught", "default": True},
    ]
    assert _StubAdapter().pick_exception_filters(caps) == ["uncaught"]


def test_default_exception_filters_empty_when_none_advertised():
    assert _StubAdapter().pick_exception_filters(Capabilities()) == []


def test_adapter_not_found_error_carries_hint():
    err = AdapterNotFoundError("install LLVM")
    assert err.hint == "install LLVM"
    assert "install LLVM" in str(err)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/al/projects/tdbg/work && pytest tests/unit/test_languages_base.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tdb.languages'` (and later, `Capabilities` has no `exception_breakpoint_filters` — that field arrives in Task 2; for now set it as a plain attribute in the test as written above, which works on any dataclass without `slots`).

- [ ] **Step 3: Write the implementation**

`src/tdb/languages/__init__.py`:

```python
"""Language profiles: everything language- or adapter-specific in one place."""

from tdb.languages.base import (
    AdapterNotFoundError,
    AdapterQuirks,
    AdapterSpec,
    LanguageNotSupportedError,
    LanguageProfile,
    Presentation,
    ProfileCapabilities,
)

__all__ = [
    "AdapterNotFoundError",
    "AdapterQuirks",
    "AdapterSpec",
    "LanguageNotSupportedError",
    "LanguageProfile",
    "Presentation",
    "ProfileCapabilities",
]
```

`src/tdb/languages/base.py`:

```python
"""Core datatypes for multi-language support.

A LanguageProfile bundles three sub-objects, each with exactly ONE consumer:

  - ``adapter``:      AdapterSpec        -> dap/client.py + session/controller.py
  - ``presentation``: Presentation       -> widgets/code_view.py
  - ``capabilities``: ProfileCapabilities -> per-feature gates (statement
                      stepping, task inspection, child processes)

Rules that keep this compartmentalized (see the design spec):
  - one-way dependency: modules read the profile; a profile never imports
    the controller, app, or widgets, and never holds runtime state.
  - capability values are data/callables, not subclass overrides, so
    consumers feature-gate with ``is not None`` / truthiness checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from tdb.dap.types import Capabilities


class AdapterNotFoundError(Exception):
    """A debug-adapter executable could not be located.

    ``hint`` is one user-facing line: what to install or which config
    key to set. Raised by AdapterSpec.command(); surfaced by the CLI.
    """

    def __init__(self, hint: str) -> None:
        super().__init__(hint)
        self.hint = hint


class LanguageNotSupportedError(Exception):
    """Language detection or --lang/--adapter resolution failed."""


@dataclass(frozen=True)
class AdapterQuirks:
    """Per-adapter workarounds, read only by session/controller.py."""

    # debugpy ignores `stopOnEntry` for attach requests; the controller
    # pre-arms a `pause` before configurationDone instead. True only
    # for debugpy. (The deferred launch/attach response needs no flag:
    # holding the response until configurationDone is DAP-spec behavior
    # and the controller's fire-and-forget launch future handles it for
    # every adapter.)
    pre_arm_pause_on_attach: bool = False


class AdapterSpec:
    """How to spawn and speak to one debug adapter. Subclass per adapter.

    Instances are stateless: pure data + pure functions.
    """

    id: str = ""
    quirks: AdapterQuirks = AdapterQuirks()

    def command(self) -> list[str]:
        """Argv for the adapter subprocess (DAP over stdio).

        Raises AdapterNotFoundError with an install hint when the
        executable cannot be found.
        """
        raise NotImplementedError

    def launch_body(
        self,
        *,
        program: str,
        args: list[str],
        cwd: str,
        env: dict[str, str] | None,
        stop_on_entry: bool,
        console: str,
        opts: dict[str, Any],
    ) -> dict[str, Any]:
        """Arguments for the DAP `launch` request.

        ``opts`` carries adapter-specific extras the generic client
        signature doesn't know about (debugpy: just_my_code, python,
        sub_process).
        """
        raise NotImplementedError

    def attach_body(
        self, *, host: str, port: int, opts: dict[str, Any]
    ) -> dict[str, Any]:
        """Arguments for the DAP `attach` request."""
        raise NotImplementedError

    def pick_exception_filters(self, caps: Capabilities) -> list[str]:
        """Choose exception-breakpoint filters from what the adapter
        advertised in its initialize response. Default: the adapter's
        own defaults."""
        return [
            f["filter"] for f in caps.exception_breakpoint_filters if f.get("default")
        ]


@dataclass(frozen=True)
class Presentation:
    """Language-specific display knobs, consumed by widgets."""

    # Rich/pygments lexer name for the Code View.
    lexer: str = "text"


@dataclass(frozen=True)
class ProfileCapabilities:
    """Optional features. None/False means "hidden for this language"."""

    # Map a source path to statement step-units [(start_line, end_line)].
    # None -> no statement-granularity stepping; line mode only.
    compute_step_units: Callable[[str], list[tuple[int, int]]] | None = None

    # "debugpy" -> controller registers ChildProcessManager's
    # debugpyAttach listener. None -> no child-process debugging.
    # (A standard `startDebugging`-based strategy is future work.)
    child_process_strategy: str | None = None

    # True -> the asyncio-task / multiprocessing inspection snippets
    # (tdb.inspection) may be evaluated in the debuggee.
    task_inspection: bool = False


@dataclass(frozen=True)
class LanguageProfile:
    """One debuggable language: its default adapter + presentation + gates."""

    id: str
    display_name: str
    adapter: AdapterSpec
    presentation: Presentation = field(default_factory=Presentation)
    capabilities: ProfileCapabilities = field(default_factory=ProfileCapabilities)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_languages_base.py -q`
Expected: 7 passed. (If `test_default_exception_filters_*` fails with `AttributeError: exception_breakpoint_filters`, `Capabilities` uses `slots` — in that case swap the two filter tests to construct a `types.SimpleNamespace(exception_breakpoint_filters=[...])` and revisit in Task 2.)

- [ ] **Step 5: Full suite + commit**

Run: `pytest tests/unit -q` — expected: all pass (641 + 7).

```bash
git add src/tdb/languages tests/unit/test_languages_base.py
git commit -m "feat: add LanguageProfile datatypes for multi-language support"
```

---

### Task 2: `Capabilities.exception_breakpoint_filters` in dap/types.py

**Files:**
- Modify: `src/tdb/dap/types.py` (the `Capabilities` dataclass and its `from_dict`)
- Test: `tests/unit/test_dap_types.py` (append) — if that file doesn't exist, create it.

**Interfaces:**
- Produces: `Capabilities.exception_breakpoint_filters: list[dict]` — raw filter dicts as advertised by the adapter (`{"filter": str, "label": str, "default": bool}`), default `[]`. Consumed by `AdapterSpec.pick_exception_filters` (Task 1) and `controller.do_configure` (Task 5).

- [ ] **Step 1: Write the failing test**

Append to (or create) `tests/unit/test_dap_types.py`:

```python
from tdb.dap.types import Capabilities


def test_capabilities_parses_exception_breakpoint_filters():
    caps = Capabilities.from_dict(
        {
            "supportsConfigurationDoneRequest": True,
            "exceptionBreakpointFilters": [
                {"filter": "userUnhandled", "label": "User Uncaught", "default": False},
                {"filter": "raised", "label": "Raised", "default": True},
            ],
        }
    )
    assert caps.exception_breakpoint_filters == [
        {"filter": "userUnhandled", "label": "User Uncaught", "default": False},
        {"filter": "raised", "label": "Raised", "default": True},
    ]


def test_capabilities_filters_default_empty():
    assert Capabilities().exception_breakpoint_filters == []
    assert Capabilities.from_dict({}).exception_breakpoint_filters == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_dap_types.py -q`
Expected: FAIL — `TypeError`/`AttributeError` for the unknown field.

- [ ] **Step 3: Implement**

In `src/tdb/dap/types.py`, open the `Capabilities` dataclass. Add a field (with `field(default_factory=list)`, importing `field` from dataclasses if not already):

```python
    # Raw exceptionBreakpointFilters dicts from the initialize response:
    # [{"filter": str, "label": str, "default": bool}, ...]. Kept raw —
    # the only consumer is AdapterSpec.pick_exception_filters.
    exception_breakpoint_filters: list[dict] = field(default_factory=list)
```

In `Capabilities.from_dict`, alongside the existing key mappings, add:

```python
exception_breakpoint_filters = (data.get("exceptionBreakpointFilters", []),)
```

(Match the existing `from_dict` style exactly — it maps camelCase keys to snake_case kwargs.)

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_dap_types.py tests/unit/test_languages_base.py -q`
Expected: all pass. If Task 1 used a SimpleNamespace fallback for the filter tests, restore them to plain `Capabilities()` now.

- [ ] **Step 5: Full suite + commit**

Run: `pytest tests/unit -q` — all pass.

```bash
git add src/tdb/dap/types.py tests/unit/test_dap_types.py tests/unit/test_languages_base.py
git commit -m "feat: parse exceptionBreakpointFilters into Capabilities"
```

---

### Task 3: `languages/python.py` — DebugpyAdapter + PYTHON_PROFILE

**Files:**
- Create: `src/tdb/languages/python.py`
- Test: `tests/unit/test_python_profile.py`

**Interfaces:**
- Consumes: Task 1 datatypes; `tdb.source_analysis.compute_step_units`.
- Produces: `DebugpyAdapter` (AdapterSpec, `id="debugpy"`), `build_python_profile(adapter: str | None = None, adapter_paths: dict[str, str] | None = None) -> LanguageProfile`, module constant `PYTHON_PROFILE = build_python_profile()`. Tasks 4–5 default to these. (`adapter_paths` is the config's adapter-id → executable-path mapping; Python ignores it — the debugpy adapter always runs on tdb's own interpreter — but every builder shares this signature so the registry can pass it blindly.)
- The launch/attach bodies must be **byte-identical** to what `dap/client.py` builds today (see `client.py:291-303` and `client.py:336-349`) — that identity is what keeps Phase 1 at zero behavior change.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_python_profile.py
"""DebugpyAdapter must reproduce the exact launch/attach bodies the
client hardcoded before the LanguageProfile extraction."""

import sys

import pytest

from tdb.dap.types import Capabilities
from tdb.languages.base import LanguageNotSupportedError
from tdb.languages.python import PYTHON_PROFILE, DebugpyAdapter, build_python_profile


def test_command_runs_own_interpreter_with_frozen_modules_off():
    assert DebugpyAdapter().command() == [
        sys.executable,
        "-Xfrozen_modules=off",
        "-m",
        "debugpy.adapter",
    ]


def test_launch_body_matches_legacy_client_body():
    body = DebugpyAdapter().launch_body(
        program="/tmp/prog.py",
        args=["a", "b"],
        cwd="/tmp",
        env={"K": "V"},
        stop_on_entry=True,
        console="internalConsole",
        opts={
            "just_my_code": False,
            "python": "/usr/bin/python3",
            "sub_process": False,
        },
    )
    assert body == {
        "type": "debugpy",
        "request": "launch",
        "program": "/tmp/prog.py",
        "args": ["a", "b"],
        "cwd": "/tmp",
        "console": "internalConsole",
        "redirectOutput": True,
        "justMyCode": False,
        "stopOnEntry": True,
        "subProcess": False,
        "pythonArgs": ["-Xfrozen_modules=off"],
        "env": {"K": "V"},
        "python": "/usr/bin/python3",
    }


def test_launch_body_defaults():
    body = DebugpyAdapter().launch_body(
        program="p.py",
        args=[],
        cwd=".",
        env=None,
        stop_on_entry=False,
        console="externalTerminal",
        opts={},
    )
    assert body["justMyCode"] is True
    assert body["subProcess"] is True
    assert body["redirectOutput"] is False  # externalTerminal
    assert "env" not in body
    assert "python" not in body


def test_attach_body_matches_legacy_client_body():
    body = DebugpyAdapter().attach_body(
        host="10.0.0.1",
        port=5678,
        opts={
            "sub_process_id": 42,
            "just_my_code": False,
            "path_mappings": [("/local", "/remote")],
        },
    )
    assert body == {
        "type": "debugpy",
        "request": "attach",
        "connect": {"host": "10.0.0.1", "port": 5678},
        "justMyCode": False,
        "subProcess": True,
        "subProcessId": 42,
        "pathMappings": [{"localRoot": "/local", "remoteRoot": "/remote"}],
    }


def test_attach_body_minimal():
    body = DebugpyAdapter().attach_body(host="127.0.0.1", port=1, opts={})
    assert "subProcessId" not in body
    assert "pathMappings" not in body
    assert body["justMyCode"] is True


def test_exception_filters_are_user_unhandled_regardless_of_caps():
    assert DebugpyAdapter().pick_exception_filters(Capabilities()) == ["userUnhandled"]


def test_quirks_pre_arm_pause_on_attach():
    assert DebugpyAdapter().quirks.pre_arm_pause_on_attach is True


def test_profile_shape():
    p = PYTHON_PROFILE
    assert p.id == "python"
    assert p.display_name == "Python"
    assert p.presentation.lexer == "python"
    assert p.capabilities.task_inspection is True
    assert p.capabilities.child_process_strategy == "debugpy"
    from tdb.source_analysis import compute_step_units

    assert p.capabilities.compute_step_units is compute_step_units


def test_build_rejects_unknown_adapter():
    with pytest.raises(LanguageNotSupportedError):
        build_python_profile(adapter="gdb")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_python_profile.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tdb.languages.python'`.

- [ ] **Step 3: Implement**

`src/tdb/languages/python.py`:

```python
"""The Python language profile (debugpy adapter) — tdb's reference profile."""

from __future__ import annotations

import sys
from typing import Any

from tdb.dap.types import Capabilities
from tdb.languages.base import (
    AdapterQuirks,
    AdapterSpec,
    LanguageNotSupportedError,
    LanguageProfile,
    Presentation,
    ProfileCapabilities,
)


class DebugpyAdapter(AdapterSpec):
    id = "debugpy"
    quirks = AdapterQuirks(pre_arm_pause_on_attach=True)

    def command(self) -> list[str]:
        # Always tdb's own interpreter (which has debugpy installed).
        # The user's --python selects the *debuggee* interpreter and is
        # threaded through launch_body's "python" key instead. Running
        # the adapter on a Python without debugpy would die immediately
        # with ModuleNotFoundError.
        return [sys.executable, "-Xfrozen_modules=off", "-m", "debugpy.adapter"]

    def launch_body(
        self,
        *,
        program: str,
        args: list[str],
        cwd: str,
        env: dict[str, str] | None,
        stop_on_entry: bool,
        console: str,
        opts: dict[str, Any],
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "type": "debugpy",
            "request": "launch",
            "program": program,
            "args": args,
            "cwd": cwd,
            "console": console,
            "redirectOutput": console == "internalConsole",
            "justMyCode": opts.get("just_my_code", True),
            "stopOnEntry": stop_on_entry,
            "subProcess": opts.get("sub_process", True),
            # Frozen stdlib modules break debugpy's tracing.
            "pythonArgs": ["-Xfrozen_modules=off"],
        }
        if env:
            arguments["env"] = env
        if opts.get("python"):
            # "python" sets the debuggee interpreter; the adapter defaults
            # "debugLauncherPython" from it.
            arguments["python"] = opts["python"]
        return arguments

    def attach_body(
        self, *, host: str, port: int, opts: dict[str, Any]
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "type": "debugpy",
            "request": "attach",
            "connect": {"host": host, "port": port},
            "justMyCode": opts.get("just_my_code", True),
            "subProcess": True,
        }
        if opts.get("sub_process_id") is not None:
            # subProcessId (not processId) routes to a child session
            # without triggering ptrace injection.
            arguments["subProcessId"] = opts["sub_process_id"]
        if opts.get("path_mappings"):
            arguments["pathMappings"] = [
                {"localRoot": local, "remoteRoot": remote}
                for local, remote in opts["path_mappings"]
            ]
        return arguments

    def pick_exception_filters(self, caps: Capabilities) -> list[str]:
        # "userUnhandled" avoids spurious stops on internal exceptions
        # (e.g. GeneratorExit in traceback.walk_stack).
        return ["userUnhandled"]


def build_python_profile(
    adapter: str | None = None, adapter_paths: dict[str, str] | None = None
) -> LanguageProfile:
    """Registry builder. `adapter`/`adapter_paths` exist for signature
    parity with other languages; Python has exactly one adapter and it
    always runs on tdb's own interpreter."""
    if adapter not in (None, "debugpy"):
        raise LanguageNotSupportedError(
            f"unknown adapter {adapter!r} for python (known: debugpy)"
        )
    from tdb.source_analysis import compute_step_units

    return LanguageProfile(
        id="python",
        display_name="Python",
        adapter=DebugpyAdapter(),
        presentation=Presentation(lexer="python"),
        capabilities=ProfileCapabilities(
            compute_step_units=compute_step_units,
            child_process_strategy="debugpy",
            task_inspection=True,
        ),
    )


PYTHON_PROFILE = build_python_profile()
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_python_profile.py -q` — 10 passed.

- [ ] **Step 5: Full suite + commit**

Run: `pytest tests/unit -q` — all pass.

```bash
git add src/tdb/languages/python.py tests/unit/test_python_profile.py
git commit -m "feat: add Python language profile (debugpy adapter)"
```

---

### Task 4: Thread AdapterSpec through DAPClient

**Files:**
- Modify: `src/tdb/dap/client.py` (`__init__`, `start`, `_watch_adapter_death`, `initialize`, `launch`, `attach`)
- Test: `tests/unit/test_dap_client.py` (append new tests; existing 32 must pass **unmodified**)

**Interfaces:**
- Produces: `DAPClient(adapter: AdapterSpec | None = None)` — `None` defaults to `DebugpyAdapter()`, so every existing constructor call keeps today's behavior. New signatures:
  - `launch(program, args=None, cwd=None, env=None, stop_on_entry=False, console="internalConsole", **adapter_opts) -> Future[Response]`
  - `attach(host="127.0.0.1", port=0, **adapter_opts) -> Future[Response]`
  Existing keyword callers (`just_my_code=`, `python=`, `sub_process=`, `sub_process_id=`, `path_mappings=`) land in `**adapter_opts` and are read by `DebugpyAdapter` — call sites need no edits.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_dap_client.py` (reuse the module's existing `FakeAdapter` fixture pattern):

```python
class _RecordingSpec:
    """Minimal AdapterSpec double that records what the client asked for."""

    id = "recording"

    from tdb.languages.base import AdapterQuirks as _Q

    quirks = _Q()

    def __init__(self):
        self.launch_calls = []
        self.attach_calls = []

    def command(self):
        return ["true"]  # never actually spawned in these tests

    def launch_body(self, **kwargs):
        self.launch_calls.append(kwargs)
        return {"request": "launch", "program": kwargs["program"]}

    def attach_body(self, **kwargs):
        self.attach_calls.append(kwargs)
        return {"request": "attach", "port": kwargs["port"]}

    def pick_exception_filters(self, caps):
        return []


async def test_initialize_sends_adapter_id_from_spec(dap):
    client, adapter = dap
    client._adapter = _RecordingSpec()
    await client.initialize()
    req = adapter.requests_for("initialize")[-1]
    assert req["arguments"]["adapterID"] == "recording"


async def test_launch_body_comes_from_adapter_spec(dap):
    client, adapter = dap
    spec = _RecordingSpec()
    client._adapter = spec
    fut = await client.launch(
        program="p.py", args=["x"], stop_on_entry=True, just_my_code=False
    )
    assert spec.launch_calls == [
        {
            "program": "p.py",
            "args": ["x"],
            "cwd": ".",
            "env": None,
            "stop_on_entry": True,
            "console": "internalConsole",
            "opts": {"just_my_code": False},
        }
    ]
    req = adapter.requests_for("launch")[-1]
    assert req["arguments"] == {"request": "launch", "program": "p.py"}
    fut.cancel()


async def test_attach_body_comes_from_adapter_spec(dap):
    client, adapter = dap
    spec = _RecordingSpec()
    client._adapter = spec
    fut = await client.attach(host="h", port=9, sub_process_id=3)
    assert spec.attach_calls == [
        {"host": "h", "port": 9, "opts": {"sub_process_id": 3}}
    ]
    fut.cancel()


def test_default_adapter_is_debugpy():
    from tdb.dap.client import DAPClient
    from tdb.languages.python import DebugpyAdapter

    assert isinstance(DAPClient()._adapter, DebugpyAdapter)
```

Note: if the existing `FakeAdapter` helper exposes received requests under a different name than `requests_for`, use its actual accessor (check the top of `tests/unit/test_dap_client.py`); the assertions stay the same.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_dap_client.py -q`
Expected: the 4 new tests FAIL (`AttributeError: _adapter`), the existing 32 PASS.

- [ ] **Step 3: Implement in `src/tdb/dap/client.py`**

`__init__` — add the parameter (lazy import to avoid import cycles at module load):

```python
    def __init__(self, adapter: "AdapterSpec | None" = None) -> None:
        if adapter is None:
            from tdb.languages.python import DebugpyAdapter

            adapter = DebugpyAdapter()
        self._adapter = adapter
        ...  # existing body unchanged
```

Add to the TYPE_CHECKING imports at the top: `from tdb.languages.base import AdapterSpec` (inside an `if TYPE_CHECKING:` block; add the block if absent).

`start()` — replace the hardcoded exec (keep the docstring, reworded to point at `AdapterSpec.command`):

```python
        argv = self._adapter.command()
        self._process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # isolate from terminal so it can't interfere with TUI
        )
```

`_watch_adapter_death()` — generalize the two message strings:

```python
log.error(
    "%s adapter exited with code %s%s",
    self._adapter.id,
    rc,
    f"\nstderr:\n{stderr_text}" if stderr_text else "",
)
err = ConnectionError(
    f"{self._adapter.id} adapter died (exit {rc}): "
    f"{stderr_text.splitlines()[-1] if stderr_text else 'no output'}"
)
```

(The default spec id is `"debugpy"`, so the existing adapter-death test asserting on the message still passes.)

`initialize()` — `"adapterID": self._adapter.id,`.

`launch()` — replace wholesale:

```python
    async def launch(
        self,
        program: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        stop_on_entry: bool = False,
        console: str = "internalConsole",
        **adapter_opts: Any,
    ) -> asyncio.Future[Response]:
        """Send launch request. Returns a Future for the response.

        Adapters may delay the launch response until configurationDone
        (DAP-spec behavior; debugpy does this), so callers must NOT await
        this directly — await the returned Future after configurationDone.

        ``**adapter_opts`` carries adapter-specific keys (debugpy:
        just_my_code, python, sub_process) into AdapterSpec.launch_body.
        """
        arguments = self._adapter.launch_body(
            program=program,
            args=args or [],
            cwd=cwd or ".",
            env=env,
            stop_on_entry=stop_on_entry,
            console=console,
            opts=adapter_opts,
        )
        return await self._send_raw("launch", arguments)
```

`attach()` — replace wholesale (move the debugpy-specific notes into `DebugpyAdapter`, already done in Task 3):

```python
async def attach(
    self,
    host: str = "127.0.0.1",
    port: int = 0,
    **adapter_opts: Any,
) -> asyncio.Future[Response]:
    """Send attach request. Returns a Future (same pattern as launch)."""
    arguments = self._adapter.attach_body(host=host, port=port, opts=adapter_opts)
    return await self._send_raw("attach", arguments)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_dap_client.py tests/unit/test_python_profile.py -q`
Expected: all pass — including the pre-existing launch-arg-encoding and attach tests, because `DebugpyAdapter` reproduces the legacy bodies exactly.

- [ ] **Step 5: Full suite (incl. integration) + commit**

Run: `pytest tests/unit -q && pytest tests/integration -q`
Expected: all pass (integration exercises the real debugpy adapter through the new path).

```bash
git add src/tdb/dap/client.py tests/unit/test_dap_client.py
git commit -m "refactor: route DAPClient spawn/launch/attach through AdapterSpec"
```

---

### Task 5: Thread LanguageProfile through DebugController

**Files:**
- Modify: `src/tdb/session/controller.py` (`__init__`, `_setup_event_handlers`, `do_configure`)
- Test: `tests/unit/test_controller_actions.py` (append; existing 59 pass unmodified)

**Interfaces:**
- Produces: `DebugController(event_handler, profile: LanguageProfile | None = None)` — `None` → `PYTHON_PROFILE`. Public attribute `self.profile` (read by app/widgets/services in later tasks). `self.client = DAPClient(self.profile.adapter)`.
- `do_configure` now: filters via `self.profile.adapter.pick_exception_filters(self.client.capabilities)` (skipped when empty); pre-arm pause additionally gated on `self.profile.adapter.quirks.pre_arm_pause_on_attach`.
- `_setup_event_handlers`: `self._children.register_on(self.client)` only when `self.profile.capabilities.child_process_strategy == "debugpy"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_controller_actions.py` (reuse the module's `_FakeDAP`, `_RecordingHandler`, `_make()` helpers):

```python
from tdb.languages.base import (
    AdapterQuirks,
    AdapterSpec,
    LanguageProfile,
    Presentation,
    ProfileCapabilities,
)


class _NullSpec(AdapterSpec):
    id = "null"

    def command(self):
        return ["true"]

    def launch_body(self, **kw):
        return {}

    def attach_body(self, **kw):
        return {}

    def pick_exception_filters(self, caps):
        return []


def _bare_profile(**cap_kwargs) -> LanguageProfile:
    return LanguageProfile(
        id="bare",
        display_name="Bare",
        adapter=_NullSpec(),
        presentation=Presentation(),
        capabilities=ProfileCapabilities(**cap_kwargs),
    )


def test_default_profile_is_python():
    ctrl, _dap, _handler = _make()
    assert ctrl.profile.id == "python"


async def test_do_configure_skips_exception_bps_when_no_filters():
    ctrl, dap, _handler = _make(profile=_bare_profile())
    ...  # drive do_configure the same way the existing
    ...  # test_do_configure_* tests in this file do
    assert dap.calls_to("setExceptionBreakpoints") == []


async def test_do_configure_uses_profile_filters():
    ctrl, dap, _handler = _make()  # python profile
    ...  # same driving pattern
    assert dap.calls_to("setExceptionBreakpoints")[0]["filters"] == ["userUnhandled"]


def test_children_not_registered_without_strategy():
    ctrl, dap, _handler = _make(profile=_bare_profile())
    ctrl._setup_event_handlers()
    assert "debugpyAttach" not in ctrl.client._event_handlers


def test_children_registered_for_python():
    ctrl, dap, _handler = _make()
    ctrl._setup_event_handlers()
    assert "debugpyAttach" in ctrl.client._event_handlers
```

Concretely: extend the file's `_make()` builder with a `profile=None` keyword that is forwarded to `DebugController(...)`, and copy the do_configure driving pattern from the existing `test_do_configure_sends_breakpoints_before_configuration_done` test (set up `_launch_future`, then `await ctrl.do_configure()`). Replace the `...` lines with that pattern — it is already written in this file; do not invent a new one.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_controller_actions.py -q`
Expected: new tests FAIL (`TypeError: unexpected keyword 'profile'`), existing 59 PASS.

- [ ] **Step 3: Implement in `src/tdb/session/controller.py`**

`__init__` (first lines of the body):

```python
    def __init__(
        self,
        event_handler: DebugEventHandler,
        profile: "LanguageProfile | None" = None,
    ) -> None:
        if profile is None:
            from tdb.languages.python import PYTHON_PROFILE

            profile = PYTHON_PROFILE
        self.profile = profile
        self.event_handler = event_handler
        self.client = DAPClient(profile.adapter)
        ...  # rest unchanged
```

Add `LanguageProfile` to a `TYPE_CHECKING` import from `tdb.languages.base`.

`_setup_event_handlers` — wrap the child registration:

```python
        # Child process debugging is a debugpy-proprietary mechanism
        # (the `debugpyAttach` reverse event); only wire it up when the
        # profile opts in.
        if self.profile.capabilities.child_process_strategy == "debugpy":
            self._children.register_on(self.client)
```

`do_configure` — replace the hardcoded filter call:

```python
# Exception-breakpoint filters are adapter-specific; the spec
# picks them from what the adapter advertised at initialize.
filters = self.profile.adapter.pick_exception_filters(self.client.capabilities)
if filters:
    await self.client.set_exception_breakpoints(filters)
```

and gate the pre-arm pause block (keep the existing comment):

```python
        if (
            self._is_remote_attach
            and self.profile.adapter.quirks.pre_arm_pause_on_attach
            and self.state.phase != SessionPhase.STOPPED
        ):
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_controller_actions.py -q` — all pass.

- [ ] **Step 5: Full suite + integration + commit**

Run: `pytest tests/unit -q && pytest tests/integration -q` — all pass. **This completes Phase 1: zero behavior change, verified.**

```bash
git add src/tdb/session/controller.py tests/unit/test_controller_actions.py
git commit -m "refactor: thread LanguageProfile through DebugController"
```

---

### Task 6: `languages/registry.py` — registration + detection

**Files:**
- Create: `src/tdb/languages/registry.py`
- Test: `tests/unit/test_language_registry.py`

**Interfaces:**
- Consumes: `build_python_profile` (Task 3); later `build_cpp_profile` (Task 10) self-registers here.
- Produces:
  - `register(lang_id: str, builder: Callable[..., LanguageProfile]) -> None`
  - `detect(program: str | None) -> str` — returns a language id; raises `LanguageNotSupportedError` with actionable messages.
  - `resolve(lang_id: str, adapter: str | None = None, adapter_paths: dict[str, str] | None = None) -> LanguageProfile` — `adapter_paths` is `TdbConfig.adapters`, forwarded verbatim to the language builder (which looks up the executable by the adapter id it actually chose — this is why the lookup can't happen in cli.py: with `--adapter` omitted, only the builder knows the default adapter's id).
  - `known_languages() -> list[str]`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_language_registry.py
import pytest

from tdb.languages.base import LanguageNotSupportedError
from tdb.languages import registry


def test_detect_py_extension():
    assert registry.detect("/x/prog.py") == "python"


def test_detect_none_defaults_python():
    # remote-attach mode has no program; --lang overrides upstream
    assert registry.detect(None) == "python"


@pytest.mark.parametrize(
    "magic",
    [
        b"\x7fELF\x02\x01\x01" + b"\x00" * 9,  # ELF
        b"MZ\x90\x00" + b"\x00" * 12,  # PE
        b"\xcf\xfa\xed\xfe" + b"\x00" * 12,  # Mach-O 64 LE
        b"\xca\xfe\xba\xbe" + b"\x00" * 12,  # Mach-O universal
    ],
)
def test_detect_native_binaries_as_cpp(tmp_path, magic):
    binary = tmp_path / "prog"
    binary.write_bytes(magic)
    assert registry.detect(str(binary)) == "cpp"


def test_detect_python_shebang(tmp_path):
    script = tmp_path / "tool"
    script.write_text("#!/usr/bin/env python3\nprint('hi')\n")
    assert registry.detect(str(script)) == "python"


def test_compiled_source_extension_gets_build_hint(tmp_path):
    src = tmp_path / "main.cpp"
    src.write_text("int main() {}\n")
    with pytest.raises(LanguageNotSupportedError, match="compile.*-g.*tdb ./binary"):
        registry.detect(str(src))


def test_unknown_target_errors_with_lang_hint(tmp_path):
    f = tmp_path / "data.txt"
    f.write_text("hello\n")
    with pytest.raises(LanguageNotSupportedError, match="--lang"):
        registry.detect(str(f))


def test_go_maps_to_unregistered_language(tmp_path):
    with pytest.raises(LanguageNotSupportedError, match="go.*not supported"):
        registry.resolve(registry.detect("/x/main.go"))


def test_resolve_python():
    assert registry.resolve("python").id == "python"


def test_resolve_unknown_language_lists_known():
    with pytest.raises(LanguageNotSupportedError, match="python"):
        registry.resolve("cobol")


def test_resolve_passes_adapter_through():
    with pytest.raises(LanguageNotSupportedError, match="gdb"):
        registry.resolve("python", adapter="gdb")
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_language_registry.py -q`
Expected: FAIL — no module `tdb.languages.registry`.

- [ ] **Step 3: Implement**

`src/tdb/languages/registry.py`:

```python
"""Language registration and target detection.

Detection chain (first hit wins):
  1. caller-supplied --lang (handled upstream in cli.py; detect() is
     only called when --lang was not given)
  2. file extension (.py -> python, .go -> go, compiled-language source
     extensions -> actionable error)
  3. binary magic bytes (ELF / PE / Mach-O -> cpp)
  4. shebang mentioning python -> python
  5. no match -> error naming --lang
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from tdb.languages.base import LanguageNotSupportedError, LanguageProfile
from tdb.languages.python import build_python_profile

_BUILDERS: dict[str, Callable[..., LanguageProfile]] = {}


def register(lang_id: str, builder: Callable[..., LanguageProfile]) -> None:
    _BUILDERS[lang_id] = builder


def known_languages() -> list[str]:
    return sorted(_BUILDERS)


def resolve(
    lang_id: str,
    adapter: str | None = None,
    adapter_paths: dict[str, str] | None = None,
) -> LanguageProfile:
    """Build the profile for a detected/requested language id.

    ``adapter_paths`` (TdbConfig.adapters: adapter id -> executable
    path) is forwarded to the builder, which resolves the override for
    whichever adapter it actually selects.
    """
    builder = _BUILDERS.get(lang_id)
    if builder is None:
        raise LanguageNotSupportedError(
            f"language '{lang_id}' is not supported yet "
            f"(supported: {', '.join(known_languages())})"
        )
    return builder(adapter=adapter, adapter_paths=adapter_paths)


_EXTENSION_MAP = {".py": "python", ".pyw": "python", ".go": "go"}

# Source files for compiled languages: debugging the source is a user
# error — you debug the built executable.
_COMPILED_SOURCE_EXTS = {".c", ".cc", ".cpp", ".cxx", ".c++", ".rs"}

_MAGIC = [
    (b"\x7fELF", "cpp"),  # Linux
    (b"MZ", "cpp"),  # Windows PE
    (b"\xcf\xfa\xed\xfe", "cpp"),  # Mach-O 64-bit LE
    (b"\xce\xfa\xed\xfe", "cpp"),  # Mach-O 32-bit LE
    (b"\xca\xfe\xba\xbe", "cpp"),  # Mach-O universal
]


def detect(program: str | None) -> str:
    """Infer the language id from the debug target.

    Called only when --lang was not given. `None` (remote-attach mode,
    no local program) defaults to python — tdb's historical behavior.
    """
    if program is None:
        return "python"
    path = Path(program)
    ext = path.suffix.lower()
    if ext in _COMPILED_SOURCE_EXTS:
        raise LanguageNotSupportedError(
            f"{program!r} is source for a compiled language — compile "
            f"with debug info (e.g. `g++ -g -O0`) and run `tdb ./binary`, "
            f"or pass --lang explicitly"
        )
    if ext in _EXTENSION_MAP:
        return _EXTENSION_MAP[ext]
    head = b""
    try:
        with open(path, "rb") as f:
            head = f.read(64)
    except OSError:
        pass
    for magic, lang_id in _MAGIC:
        if head.startswith(magic):
            return lang_id
    if head.startswith(b"#!") and b"python" in head.splitlines()[0]:
        return "python"
    raise LanguageNotSupportedError(
        f"cannot determine the language of {program!r} — pass --lang "
        f"(supported: {', '.join(known_languages())})"
    )


register("python", build_python_profile)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_language_registry.py -q` — 12 passed.

- [ ] **Step 5: Full suite + commit**

```bash
pytest tests/unit -q
git add src/tdb/languages/registry.py tests/unit/test_language_registry.py
git commit -m "feat: language registry with extension/magic-byte detection"
```

---

### Task 7: Adapter overrides in TdbConfig

**Files:**
- Modify: `src/tdb/persist.py` (`TdbConfig` dataclass, `from_dict`)
- Test: `tests/unit/test_persist.py` (append; create if absent)

**Interfaces:**
- Produces: `TdbConfig.adapters: dict[str, str]` (adapter id → executable path, e.g. `{"lldb-dap": "/opt/llvm/bin/lldb-dap"}`) and `TdbConfig.default_adapters: dict[str, str]` (language id → adapter id, e.g. `{"cpp": "gdb"}`). Both default `{}`; both round-trip through `to_dict` (which already iterates `fields(self)` — no change needed there). Consumed by `cli._resolve_language` (Task 8).

- [ ] **Step 1: Write the failing test**

```python
from tdb.persist import TdbConfig


def test_config_adapter_fields_default_empty():
    cfg = TdbConfig()
    assert cfg.adapters == {}
    assert cfg.default_adapters == {}


def test_config_adapter_fields_from_dict():
    cfg = TdbConfig.from_dict(
        {
            "adapters": {"lldb-dap": "/opt/llvm/bin/lldb-dap"},
            "default_adapters": {"cpp": "gdb"},
        }
    )
    assert cfg.adapters == {"lldb-dap": "/opt/llvm/bin/lldb-dap"}
    assert cfg.default_adapters == {"cpp": "gdb"}


def test_config_adapter_fields_reject_bad_shapes():
    cfg = TdbConfig.from_dict({"adapters": "nope", "default_adapters": [1]})
    assert cfg.adapters == {}
    assert cfg.default_adapters == {}


def test_config_adapter_fields_round_trip():
    cfg = TdbConfig(adapters={"gdb": "/usr/bin/gdb"})
    assert TdbConfig.from_dict(cfg.to_dict()).adapters == {"gdb": "/usr/bin/gdb"}
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/unit/test_persist.py -q` → FAIL (unknown field).

- [ ] **Step 3: Implement** — in `TdbConfig` add (importing `field` from dataclasses; already imported as `fields` — add `field`):

```python
    # Debug-adapter executable overrides: adapter id -> path
    # (e.g. {"lldb-dap": "/opt/llvm/bin/lldb-dap"}).
    adapters: dict[str, str] = field(default_factory=dict)

    # Preferred adapter per language: language id -> adapter id
    # (e.g. {"cpp": "gdb"}).
    default_adapters: dict[str, str] = field(default_factory=dict)
```

In `from_dict`, following the established defensive style:

```python
        for key in ("adapters", "default_adapters"):
            value = data.get(key)
            if isinstance(value, dict) and all(
                isinstance(k, str) and isinstance(v, str) for k, v in value.items()
            ):
                kwargs[key] = value
```

- [ ] **Step 4: Run** — `pytest tests/unit/test_persist.py -q` → pass.

- [ ] **Step 5: Full suite + commit**

```bash
pytest tests/unit -q
git add src/tdb/persist.py tests/unit/test_persist.py
git commit -m "feat: adapter path/preference overrides in TdbConfig"
```

---

### Task 8: CLI `--lang` / `--adapter` + profile resolution

**Files:**
- Modify: `src/tdb/cli.py` (`build_parser`, new `_resolve_language`, `parse_args`)
- Test: `tests/unit/test_cli.py` (append; create if absent — check for an existing CLI test module first with `ls tests/unit | grep cli`)

**Interfaces:**
- Consumes: `registry.detect/resolve` (Task 6), `TdbConfig.adapters/default_adapters` (Task 7).
- Produces: `args.profile: LanguageProfile` set by `parse_args` for all run modes except `--doc/--doc-text/--post-mortem/--mcp`. Python-only flags (`--python`/`--pv`, `--no-subprocess`, `--remote-attach`) are `parser.error`ed for non-python profiles. `_snap_breakpoints` no-ops when `args.profile.capabilities.compute_step_units is None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_cli.py (append or create)
import pytest

from tdb.cli import parse_args


def _write_elf(tmp_path):
    binary = tmp_path / "prog"
    binary.write_bytes(b"\x7fELF\x02\x01\x01" + b"\x00" * 9)
    return binary


def test_python_program_resolves_python_profile(tmp_path):
    prog = tmp_path / "p.py"
    prog.write_text("pass\n")
    args = parse_args([str(prog)])
    assert args.profile.id == "python"


def test_elf_binary_resolves_cpp_profile_or_errors_before_task10(tmp_path):
    # Until Task 10 registers cpp, this errors with "not supported";
    # after Task 10 it resolves. Written to pass in both states:
    binary = _write_elf(tmp_path)
    try:
        args = parse_args([str(binary)])
    except SystemExit:
        return  # pre-Task-10: parser.error path exercised
    assert args.profile.id == "cpp"


def test_lang_flag_overrides_detection(tmp_path):
    binary = _write_elf(tmp_path)
    args = parse_args(["--lang", "python", str(binary)])
    assert args.profile.id == "python"


def test_python_flag_rejected_for_non_python(tmp_path, capsys):
    binary = _write_elf(tmp_path)
    with pytest.raises(SystemExit):
        parse_args(["--lang", "cpp", "--python", "/usr/bin/python3", str(binary)])


def test_no_subprocess_rejected_for_non_python(tmp_path):
    binary = _write_elf(tmp_path)
    with pytest.raises(SystemExit):
        parse_args(["--lang", "cpp", "--no-subprocess", str(binary)])


def test_remote_attach_rejected_for_non_python():
    with pytest.raises(SystemExit):
        parse_args(["--lang", "cpp", "-r", "5678"])


def test_breakpoints_not_snapped_for_non_python(tmp_path, monkeypatch):
    binary = _write_elf(tmp_path)
    called = []
    import tdb.source_analysis as sa

    monkeypatch.setattr(sa, "snap_breakpoint", lambda *a: called.append(a) or a[1])
    try:
        args = parse_args(["--lang", "cpp", "-k", f"{binary}:3", str(binary)])
    except SystemExit:
        pytest.skip("cpp profile not yet registered (pre-Task-10)")
    assert called == []
    assert args.breakpoint == [(str(binary), 3)]
```

Note the two tests tolerant of the not-yet-registered cpp profile — they harden automatically once Task 10 lands. The `--lang cpp` rejection tests pass either way because `parser.error` also raises SystemExit for an unknown language.

- [ ] **Step 2: Run to verify failure** — `pytest tests/unit/test_cli.py -q` → new tests FAIL (`--lang` unrecognized).

- [ ] **Step 3: Implement in `src/tdb/cli.py`**

In `build_parser()` add after the `--pv` argument:

```python
    parser.add_argument(
        "--lang",
        default=None,
        metavar="LANGUAGE",
        help="Debuggee language (default: auto-detect from the program "
        "target — .py -> python, native executables -> cpp).",
    )
    parser.add_argument(
        "--adapter",
        default=None,
        metavar="ADAPTER",
        help="Debug adapter to use within the language (e.g. `--lang cpp "
        "--adapter gdb`). Default: the language's standard adapter.",
    )
```

New post-processing helper (place after `_resolve_program_path`):

```python
def _resolve_language(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> None:
    """Resolve args.profile from --lang/--adapter, detection, and config.

    Also enforces that Python/debugpy-only flags aren't combined with a
    non-Python profile — failing fast beats a confusing adapter error.
    """
    from tdb.languages import registry
    from tdb.languages.base import LanguageNotSupportedError
    from tdb.persist import load_config

    config = load_config()
    try:
        lang_id = args.lang or registry.detect(args.program)
        adapter = args.adapter or config.default_adapters.get(lang_id)
        profile = registry.resolve(lang_id, adapter=adapter)
    except LanguageNotSupportedError as e:
        parser.error(str(e))
    args.profile = profile

    if profile.id != "python":
        if args.python is not None:
            parser.error(
                f"--python/--pv apply only to Python debuggees "
                f"(detected language: {profile.id})"
            )
        if args.no_subprocess:
            parser.error(
                f"--no-subprocess is debugpy-specific (detected language: {profile.id})"
            )
        if args.remote_attach:
            parser.error("--remote-attach currently supports Python debuggees only")
```

Executable overrides ride along in the same call — the `profile = registry.resolve(...)` line is:

```python
profile = registry.resolve(lang_id, adapter=adapter, adapter_paths=config.adapters)
```

(The builder looks up the override by the adapter id it actually chooses — passing the whole mapping is what makes a configured `lldb-dap` path work even when `--adapter` was omitted and cpp fell back to its default adapter.)

In `parse_args()`, insert `_resolve_language(args, parser)` between `_resolve_program_path(args, parser)` and `_parse_breakpoints(args, parser)`.

Guard `_snap_breakpoints` (first lines):

```python
    if args.profile.capabilities.compute_step_units is None:
        return  # no source-statement model for this language; lines pass through
```

Update the parser description to `"A multi-language DAP debugger with a textual TUI (Python via debugpy, C/C++ via lldb-dap/gdb)."` and the `program` help to `"Program to debug (Python script, or a native executable built with -g)"`.

- [ ] **Step 4: Run** — `pytest tests/unit/test_cli.py -q` → pass.

- [ ] **Step 5: Full suite + commit**

```bash
pytest tests/unit -q
git add src/tdb/cli.py tests/unit/test_cli.py
git commit -m "feat: --lang/--adapter flags with language auto-detection"
```

---

### Task 9: Thread the profile into TdbApp, headless runner, MCP, and the lexer

**Files:**
- Modify: `src/tdb/app.py` (`TdbApp.__init__` signature + controller construction + on_mount lexer wiring), `src/tdb/cli.py` (`_run_tui`, `_run_headless`), `src/tdb/server/runner.py` (`run_headless`), `src/tdb/widgets/code_view.py` (`_highlight_source`), `src/tdb/mcp/server.py` + `src/tdb/mcp/session.py` (forward lang/adapter)
- Test: `tests/unit/test_code_view_lexer.py` (create), `tests/unit/test_cli.py` (append)

**Interfaces:**
- Consumes: `args.profile` (Task 8), `controller.profile` (Task 5).
- Produces: `TdbApp(..., profile: LanguageProfile | None = None)`; `run_headless(..., profile: LanguageProfile | None = None)`; `CodeView.lexer_name: str = "python"` (class attribute, set per-instance from the profile); MCP `debug_launch` gains optional `lang: str | None = None` and `adapter: str | None = None` params appended to the headless argv as `--lang`/`--adapter`.

- [ ] **Step 1: Write the failing lexer test**

```python
# tests/unit/test_code_view_lexer.py
from rich.text import Text

from tdb.widgets.code_view import CodeView


def test_default_lexer_is_python():
    assert CodeView.lexer_name == "python"


def test_highlight_uses_instance_lexer():
    view = CodeView()
    view.lexer_name = "cpp"
    lines = view._highlight_source("int main() { return 0; }")
    assert isinstance(lines[0], Text)


def test_highlight_python_still_works():
    view = CodeView()
    lines = view._highlight_source("def f():\n    return 1\n")
    assert len(lines) >= 2
```

(If `CodeView()` requires constructor args, mirror however existing widget tests construct it — check `grep -rn "CodeView(" tests/`; if no test constructs one, `CodeView(id="x")` matches the app's usage.)

- [ ] **Step 2: Run to verify failure** — `pytest tests/unit/test_code_view_lexer.py -q` → FAIL (`lexer_name` missing / staticmethod signature).

- [ ] **Step 3: Implement**

`src/tdb/widgets/code_view.py` — convert `_highlight_source` (line ~729) from staticmethod to instance method and add the class attribute:

```python
# Rich lexer for syntax highlighting; set from
# LanguageProfile.presentation.lexer by TdbApp at startup.
lexer_name: str = "python"


def _highlight_source(self, source: str) -> list[Text]:
    syntax = Syntax(source, self.lexer_name, theme="monokai", line_numbers=False)
    ...  # rest of the body unchanged
```

Check call sites with `grep -n "_highlight_source" src/tdb/widgets/code_view.py` — they already call via `self.`, so removing `@staticmethod` is the only change needed.

`src/tdb/app.py`:
- Add `profile: "LanguageProfile | None" = None` as the last `TdbApp.__init__` parameter; store `self._profile = profile` (leave None handling to the controller default). Where `self.controller = DebugController(` is constructed in `__init__`, pass `profile=self._profile`.
- In `on_mount` (or wherever `#code-view` is first configured — the same place `code_view.load_file(self._program)` happens at line ~318), add before the load:

```python
        code_view.lexer_name = self.controller.profile.presentation.lexer
```

- Update the window/app title strings that say "Python Debugger" (`grep -n "Python Debugger" src/tdb/app.py`) to use `f"tdb — {self.controller.profile.display_name}"` or equivalent.

`src/tdb/cli.py` `_run_tui`: pass `profile=args.profile` to `TdbApp(...)`. `_run_headless`: pass `profile=args.profile` to `run_headless(...)`.

`src/tdb/server/runner.py` `run_headless`: add `profile: "LanguageProfile | None" = None` parameter; pass `profile=profile` where its `DebugController(` is constructed.

MCP: in `src/tdb/mcp/session.py`, find where the headless argv is built (`grep -n "headless" src/tdb/mcp/session.py`) and append `--lang`/`--adapter` when provided; in `src/tdb/mcp/server.py`, add `lang: str | None = None, adapter: str | None = None` params to the `debug_launch` tool signature + docstring and pass through to the session.

- [ ] **Step 4: Run** — `pytest tests/unit/test_code_view_lexer.py tests/unit -q` → all pass.

- [ ] **Step 5: End-to-end sanity + commit**

Run: `pytest tests/integration -q` (real debugpy still works through all the new threading).
Run: `timeout 20 python -m tdb --headless --server-port 8199 work/examples/sales_report_buggy.py & sleep 6; curl -s localhost:8199/rpc -d '{"method":"status"}' ; wait` — adjust to the RPC smoke pattern used in existing integration tests if this exact form doesn't match; expected: a JSON status response, proving the headless path still boots.

```bash
git add src/tdb/app.py src/tdb/cli.py src/tdb/server/runner.py src/tdb/widgets/code_view.py src/tdb/mcp/server.py src/tdb/mcp/session.py tests/unit/test_code_view_lexer.py
git commit -m "feat: thread LanguageProfile into app, headless server, MCP, and code-view lexer"
```

---

### Task 10: `languages/cpp.py` — LldbDapAdapter + CppProfile + contract tests

**Files:**
- Create: `src/tdb/languages/cpp.py`
- Test: `tests/unit/test_cpp_profile.py`, `tests/unit/test_profile_contract.py` (parametrized over all registered languages)

**Interfaces:**
- Consumes: Task 1 base classes, Task 6 registry.
- Produces: `LldbDapAdapter(executable: str | None = None)` (`id="lldb-dap"`), `build_cpp_profile(adapter: str | None = None, adapter_paths: dict[str, str] | None = None) -> LanguageProfile` registered as `"cpp"` (the builder resolves `executable = (adapter_paths or {}).get(adapter_id)` for the adapter it selects). Capabilities: all off (core DAP only). `attach_body` raises `LanguageNotSupportedError` (remote attach is Python-only for now; CLI already blocks it in Task 8).
- **Registration note:** registration lives IN `registry.py` (import order stays one-way: registry → language modules) — add `from tdb.languages.cpp import build_cpp_profile` + `register("cpp", build_cpp_profile)` at the bottom, mirroring the python registration.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_cpp_profile.py
import shutil

import pytest

from tdb.dap.types import Capabilities
from tdb.languages.base import AdapterNotFoundError, LanguageNotSupportedError
from tdb.languages.cpp import LldbDapAdapter, build_cpp_profile
from tdb.languages import registry


def test_profile_shape():
    p = build_cpp_profile()
    assert p.id == "cpp"
    assert p.adapter.id == "lldb-dap"
    assert p.presentation.lexer == "cpp"
    assert p.capabilities.compute_step_units is None
    assert p.capabilities.child_process_strategy is None
    assert p.capabilities.task_inspection is False
    assert p.adapter.quirks.pre_arm_pause_on_attach is False


def test_registered_in_registry():
    assert "cpp" in registry.known_languages()
    assert registry.resolve("cpp").id == "cpp"


def test_command_uses_explicit_executable():
    assert LldbDapAdapter(executable="/opt/lldb-dap").command() == ["/opt/lldb-dap"]


def test_adapter_paths_override_reaches_default_adapter():
    p = build_cpp_profile(adapter_paths={"lldb-dap": "/opt/lldb-dap"})
    assert p.adapter.command() == ["/opt/lldb-dap"]


def test_command_missing_executable_hints_install(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(AdapterNotFoundError) as exc:
        LldbDapAdapter().command()
    assert "lldb-dap" in exc.value.hint
    assert "LLVM" in exc.value.hint


def test_launch_body_shape():
    body = LldbDapAdapter().launch_body(
        program="/x/prog",
        args=["-n", "3"],
        cwd="/x",
        env={"A": "1"},
        stop_on_entry=True,
        console="internalConsole",
        opts={},
    )
    assert body == {
        "type": "lldb-dap",
        "request": "launch",
        "program": "/x/prog",
        "args": ["-n", "3"],
        "cwd": "/x",
        "stopOnEntry": True,
        "env": ["A=1"],  # lldb-dap takes KEY=VALUE strings
    }


def test_attach_not_supported():
    with pytest.raises(LanguageNotSupportedError):
        LldbDapAdapter().attach_body(host="h", port=1, opts={})


def test_exception_filters_use_adapter_defaults():
    caps = Capabilities.from_dict(
        {
            "exceptionBreakpointFilters": [
                {"filter": "cpp_throw", "label": "C++ Throw", "default": False},
                {"filter": "cpp_catch", "label": "C++ Catch", "default": False},
            ]
        }
    )
    # Neither is marked default -> no exception breakpoints (crashes
    # still stop the debuggee via signal handling).
    assert LldbDapAdapter().pick_exception_filters(caps) == []


def test_unknown_cpp_adapter_rejected():
    with pytest.raises(LanguageNotSupportedError, match="codelldb"):
        build_cpp_profile(adapter="codelldb")
```

```python
# tests/unit/test_profile_contract.py
"""Contract every registered language profile must honor."""

import pytest

from tdb.languages import registry
from tdb.dap.types import Capabilities


@pytest.fixture(params=registry.known_languages())
def profile(request):
    return registry.resolve(request.param)


def test_identity_fields(profile):
    assert profile.id
    assert profile.display_name
    assert profile.adapter.id


def test_launch_body_is_well_formed(profile):
    body = profile.adapter.launch_body(
        program="/x/prog",
        args=[],
        cwd="/x",
        env=None,
        stop_on_entry=False,
        console="internalConsole",
        opts={},
    )
    assert body["request"] == "launch"
    assert body["program"] == "/x/prog"


def test_filters_tolerate_empty_capabilities(profile):
    filters = profile.adapter.pick_exception_filters(Capabilities())
    assert isinstance(filters, list)


def test_capability_types(profile):
    caps = profile.capabilities
    assert caps.compute_step_units is None or callable(caps.compute_step_units)
    assert caps.child_process_strategy in (None, "debugpy")
    assert isinstance(caps.task_inspection, bool)


def test_profile_modules_never_import_ui(profile):
    import sys

    mod = sys.modules[type(profile.adapter).__module__]
    source = open(mod.__file__).read()
    for forbidden in ("tdb.app", "tdb.widgets", "session.controller"):
        assert forbidden not in source, f"{mod.__name__} imports {forbidden}"
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/unit/test_cpp_profile.py tests/unit/test_profile_contract.py -q` → FAIL (no `tdb.languages.cpp`).

- [ ] **Step 3: Implement**

`src/tdb/languages/cpp.py`:

```python
"""The C/C++ language profile.

Default adapter: lldb-dap (ships with LLVM >= 17; debugs GCC- and
clang-built binaries alike — DWARF is compiler-neutral). Alternate:
`gdb -i dap` (GDB >= 14), added as GdbDapAdapter in a later task,
selected via `--adapter gdb`.

Core-DAP capabilities only: no statement stepping (no C++ source
model), no task inspection, no child-process tracking.
"""

from __future__ import annotations

import shutil
from typing import Any

from tdb.dap.types import Capabilities
from tdb.languages.base import (
    AdapterNotFoundError,
    AdapterSpec,
    LanguageNotSupportedError,
    LanguageProfile,
    Presentation,
    ProfileCapabilities,
)


class LldbDapAdapter(AdapterSpec):
    id = "lldb-dap"

    def __init__(self, executable: str | None = None) -> None:
        self._executable = executable

    def command(self) -> list[str]:
        exe = self._executable or shutil.which("lldb-dap")
        if exe is None:
            raise AdapterNotFoundError(
                "lldb-dap not found on PATH — install LLVM >= 17 "
                '(package `lldb`), or set {"adapters": {"lldb-dap": '
                '"/path/to/lldb-dap"}} in tdb\'s config.json'
            )
        return [exe]

    def launch_body(
        self,
        *,
        program: str,
        args: list[str],
        cwd: str,
        env: dict[str, str] | None,
        stop_on_entry: bool,
        console: str,
        opts: dict[str, Any],
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": "lldb-dap",
            "request": "launch",
            "program": program,
            "args": args,
            "cwd": cwd,
            "stopOnEntry": stop_on_entry,
        }
        if env:
            # lldb-dap wants ["KEY=VALUE", ...], not a mapping.
            body["env"] = [f"{k}={v}" for k, v in env.items()]
        return body

    def attach_body(
        self, *, host: str, port: int, opts: dict[str, Any]
    ) -> dict[str, Any]:
        raise LanguageNotSupportedError(
            "remote attach is not supported for lldb-dap yet"
        )


def build_cpp_profile(
    adapter: str | None = None, adapter_paths: dict[str, str] | None = None
) -> LanguageProfile:
    adapters: dict[str, type[AdapterSpec]] = {"lldb-dap": LldbDapAdapter}
    adapter_id = adapter or "lldb-dap"
    if adapter_id not in adapters:
        raise LanguageNotSupportedError(
            f"unknown adapter {adapter_id!r} for cpp "
            f"(known: {', '.join(sorted(adapters))}; note: codelldb is "
            f"not packaged standalone — use lldb-dap)"
        )
    executable = (adapter_paths or {}).get(adapter_id)
    return LanguageProfile(
        id="cpp",
        display_name="C/C++",
        adapter=adapters[adapter_id](executable=executable),
        presentation=Presentation(lexer="cpp"),
        capabilities=ProfileCapabilities(),
    )
```

`src/tdb/languages/registry.py` — at the bottom, after the python registration:

```python
from tdb.languages.cpp import build_cpp_profile  # noqa: E402

register("cpp", build_cpp_profile)
```

- [ ] **Step 4: Run** — `pytest tests/unit/test_cpp_profile.py tests/unit/test_profile_contract.py tests/unit/test_cli.py -q` → all pass (the two Task-8 tests that tolerated missing cpp now exercise the real path).

- [ ] **Step 5: Full suite + commit**

```bash
pytest tests/unit -q
git add src/tdb/languages/cpp.py src/tdb/languages/registry.py tests/unit/test_cpp_profile.py tests/unit/test_profile_contract.py
git commit -m "feat: C/C++ language profile with lldb-dap adapter"
```

---

### Task 11: Statement-stepper capability gating

**Files:**
- Modify: `src/tdb/session/statement_stepper.py`, `src/tdb/session/controller.py` (stepper construction), `src/tdb/app.py` (`action_step_mode`, line ~1192)
- Test: `tests/unit/test_statement_stepper.py` (append + one-line harness change), `tests/unit/test_controller_actions.py` (append)

**Interfaces:**
- Produces: `StatementStepper(state, issue_step, ensure_stack_loaded, compute_units: Callable[[str], list[tuple[int, int]]] | None = None)`. With `compute_units=None`: `mode` is `"line"` and `set_mode("statement")` is a no-op staying `"line"`. Controller passes `profile.capabilities.compute_step_units`.
- Existing-test change allowed here: the `_Harness` in `test_statement_stepper.py` gains `compute_units=compute_step_units` so its 26 tests keep exercising statement mode.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_statement_stepper.py`:

```python
def test_no_compute_units_forces_line_mode():
    h = _Harness()  # default harness passes compute_units
    from tdb.session.statement_stepper import StatementStepper

    stepper = StatementStepper(
        h.state, issue_step=h.issue, ensure_stack_loaded=h.ensure,
        compute_units=None,
    )
    assert stepper.mode == "line"
    stepper.set_mode("statement")
    assert stepper.mode == "line"


def test_line_mode_stepper_never_continues(...):
    # construct as above, drive maybe_continue with a stopped state the
    # way test_maybe_continue_inside_range does; assert it returns False
    # and issues no steps.
    ...
```

Replace the `...` by copying the arrange/act lines from this file's existing `test_maybe_continue_inside_range` — same state builder, same call — with the `compute_units=None` stepper.

Append to `tests/unit/test_controller_actions.py`:

```python
def test_step_mode_forced_line_without_capability():
    ctrl, _dap, _handler = _make(profile=_bare_profile())
    ctrl.step_mode = "statement"
    assert ctrl.step_mode == "line"
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/unit/test_statement_stepper.py -q` → new tests FAIL (`unexpected keyword 'compute_units'`).

- [ ] **Step 3: Implement**

`statement_stepper.py` — remove the module-level `compute_step_units` import from line 39 (keep `find_step_unit`), and:

```python
    def __init__(
        self,
        state: DebugState,
        issue_step: Callable[[str], Awaitable[None]],
        ensure_stack_loaded: Callable[[], Awaitable[None]],
        compute_units: Callable[[str], list[tuple[int, int]]] | None = None,
    ) -> None:
        ...
        self._compute_units = compute_units
        # No source model for this language -> statement mode impossible.
        self.mode = "statement" if compute_units is not None else "line"
```

In `set_mode`, first line:

```python
        if self._compute_units is None:
            mode = "line"
```

Find the internal call site of `compute_step_units(...)` (`grep -n "compute_step_units" src/tdb/session/statement_stepper.py`) and replace with `self._compute_units(...)` (that code path is only reachable in statement mode, where `_compute_units` is not None).

`controller.py` — the stepper construction in `__init__` gains:

```python
compute_units = (self.profile.capabilities.compute_step_units,)
```

`app.py` `action_step_mode` (line ~1192) — first lines:

```python
        if self.controller.profile.capabilities.compute_step_units is None:
            self.notify(
                f"Statement stepping is not available for "
                f"{self.controller.profile.display_name} — using line mode",
                severity="warning",
            )
            return
```

`test_statement_stepper.py` `_Harness` — pass `compute_units=compute_step_units` (import from `tdb.source_analysis`) in its `StatementStepper(...)` construction.

- [ ] **Step 4: Run** — `pytest tests/unit/test_statement_stepper.py tests/unit/test_controller_actions.py -q` → all pass.

- [ ] **Step 5: Full suite + commit**

```bash
pytest tests/unit -q
git add src/tdb/session/statement_stepper.py src/tdb/session/controller.py src/tdb/app.py tests/unit/test_statement_stepper.py tests/unit/test_controller_actions.py
git commit -m "feat: gate statement stepping on profile capability"
```

---

### Task 12: Inspection gating — service, RPC, TUI, menus, `?` help, hook check

**Files:**
- Modify: `src/tdb/session/inspect_service.py`, `src/tdb/server/handlers.py` (`_gate_error`, line ~171), `src/tdb/app_handlers/inspection.py` (each `except SessionGateError`), `src/tdb/app.py` (compose `action_labels` line ~282, `?`-help site ~1093), `src/tdb/app_handlers/dap_events.py` (`_stopped_inside_breakpoint_hook`, line ~102)
- Test: `tests/unit/test_inspect_service.py` (append; create if absent), `tests/unit/test_server_handlers.py` (append if it exists — check `ls tests/unit | grep handlers`)

**Interfaces:**
- Produces: `SessionGateError("unsupported")` raised by `collect_tasks`, `task_locals`, `collect_processes` when `not ctrl.profile.capabilities.task_inspection`. RPC maps it to `"Not supported when debugging <DisplayName>"`. MCP inherits the mapping for free (MCP tools call the RPC server). The `tasks`/`processes`/`wait_graph` MCP tools stay registered (stable schema).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_inspect_service.py (append or create)
import pytest

from tdb.session.inspect_service import InspectService, SessionGateError

# reuse the controller test doubles:
from tests.unit.test_controller_actions import _bare_profile, _make


async def test_collect_tasks_unsupported_for_gated_profile():
    ctrl, _dap, _handler = _make(profile=_bare_profile())
    svc = InspectService(lambda: ctrl)
    with pytest.raises(SessionGateError) as exc:
        await svc.collect_tasks()
    assert exc.value.reason == "unsupported"


async def test_collect_processes_unsupported_for_gated_profile():
    ctrl, _dap, _handler = _make(profile=_bare_profile())
    svc = InspectService(lambda: ctrl)
    with pytest.raises(SessionGateError, match="unsupported"):
        await svc.collect_processes()


async def test_thread_stack_still_allowed_for_gated_profile():
    ctrl, dap, _handler = _make(profile=_bare_profile())
    svc = InspectService(lambda: ctrl)
    frames, scopes, variables = await svc.thread_stack(1)
    assert isinstance(frames, list)  # generic DAP path not gated
```

(If importing from `tests.unit.test_controller_actions` fails because tests aren't a package, move `_bare_profile`/`_NullSpec` into a new `tests/unit/profile_doubles.py` helper module and import from there in both files.)

- [ ] **Step 2: Run to verify failure** — `pytest tests/unit/test_inspect_service.py -q` → FAIL (no error raised).

- [ ] **Step 3: Implement**

`inspect_service.py` — extend the docstring of `SessionGateError` (reason gains `"unsupported"`), add a helper below `_gate`:

```python
    def _require_task_inspection(self) -> None:
        """Task/process inspection injects language-specific code into
        the debuggee via DAP evaluate; only profiles that opt in
        (Python/asyncio today) support it."""
        if not self._ctrl.profile.capabilities.task_inspection:
            raise SessionGateError("unsupported")
```

Call `self._require_task_inspection()` as the first line of `collect_tasks`, `task_locals`, and `collect_processes` (before `self._gate()`).

`server/handlers.py` `_gate_error` — convert from `@staticmethod` to a regular method and add the branch:

```python
    def _gate_error(self, e: SessionGateError, doing: str) -> RpcResponse:
        """Map a SessionGateError onto this API's established wording."""
        if e.reason == "unsupported":
            lang = self.controller.profile.display_name
            return RpcResponse.error(f"Not supported when debugging {lang}")
        if e.reason == "terminated":
            return RpcResponse.error("Program has terminated")
        return RpcResponse.error(f"Cannot {doing} while program is running")
```

(All call sites already go through `self._gate_error(...)`; verify with `grep -n "_gate_error" src/tdb/server/handlers.py` — if any call it on the class, switch to `self.`.)

`app_handlers/inspection.py` — the `except SessionGateError` blocks branch on the reason to pick a notify message (see lines 80-96 pattern). In each, add an `unsupported` case first:

```python
if e.reason == "unsupported":
    self.app.notify(
        f"Not available for {self.app.controller.profile.display_name}",
        title="Async Tasks",  # match each block's existing title
        severity="warning",
    )
    return
```

(Blocks that catch without binding: change `except SessionGateError:` to `except SessionGateError as e:`.)

`app.py` compose (line ~282) — make the two labels conditional:

```python
        action_labels = {"threads-label": "Threads"}
        if self.controller.profile.capabilities.task_inspection:
            action_labels["processes-label"] = "Processes"
            action_labels["async-tasks-label"] = "Async Tasks"
        yield MenuBar(
            ...,
            action_labels=action_labels,
            ...,
        )
```

`app.py` `?`-help site (~1093, injects `inspect.signature` Python code): guard with

```python
        if self.controller.profile.id != "python":
            self.notify("? help is available for Python debuggees only")
            return
```

`app_handlers/dap_events.py` `_stopped_inside_breakpoint_hook` — first line of the body:

```python
        if self.app.controller.profile.id != "python":
            return False
```

- [ ] **Step 4: Run** — `pytest tests/unit/test_inspect_service.py tests/unit -q` → all pass.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/session/inspect_service.py src/tdb/server/handlers.py src/tdb/app_handlers/inspection.py src/tdb/app.py src/tdb/app_handlers/dap_events.py tests/unit/test_inspect_service.py
git commit -m "feat: gate task/process inspection on profile capability"
```

---

### Task 13: Missing-source placeholder pilot test

**Files:**
- Test only: `tests/unit/test_missing_source.py`

**Interfaces:** none produced — this locks in existing behavior the C++ path depends on: `CodeView.load_file` on an unreadable path shows a `<Could not read …>` placeholder instead of crashing (app.py already relies on it at lines ~443/693).

- [ ] **Step 1: Write the test** (post-mortem snapshots need no adapter, so this is a fast pure-TUI pilot — reuse the snapshot-dict shape from `tests/unit/test_post_mortem_loader.py`):

```python
# tests/unit/test_missing_source.py
"""A stack frame whose source file doesn't exist on disk must degrade to
a placeholder pane, not crash — C++ system-library frames hit this
constantly (DWARF compile-dir paths)."""

from tdb.app import TdbApp
from tdb.persist import TdbConfig
from tdb.widgets.code_view import CodeView

SNAPSHOT = {
    "version": 1,
    "exception": {"type": "X", "message": "m", "traceback_text": "tb"},
    "frames": [
        {
            "id": 1,
            "filename": "/nonexistent/path/lib.cpp",
            "lineno": 3,
            "funcname": "boom",
            "scopes": [{"name": "Locals", "variablesReference": 1001}],
        }
    ],
    "variables": {"1001": []},
}


async def test_missing_source_shows_placeholder():
    app = TdbApp(program="", config=TdbConfig(), post_mortem_snapshot=SNAPSHOT)
    async with app.run_test() as pilot:
        await pilot.pause()
        code_view = app.query_one("#code-view", CodeView)
        rendered = "\n".join(str(line) for line in code_view._lines)
        assert "Could not read" in rendered
```

- [ ] **Step 2: Run it** — `pytest tests/unit/test_missing_source.py -q`
Expected: PASS immediately (behavior exists). If it FAILS, the placeholder assumption is wrong — inspect `CodeView.load_file` (line ~653) and fix `load_file` to catch `OSError` and load `f"<Could not read {path}>"` as single-line content, then re-run.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_missing_source.py
git commit -m "test: lock in missing-source placeholder behavior"
```

---

### Task 14: Unbound-breakpoint warning

**Files:**
- Modify: `src/tdb/session/controller.py`
- Test: `tests/unit/test_controller_actions.py` (append)

**Interfaces:**
- Produces: `DebugController._warn_unbound_breakpoints(source_path, result: list[Breakpoint])` — emits a console-output warning via `self.event_handler.on_output` when a file's breakpoints all come back `verified=False` (the classic no-`-g` symptom). Called from every site that awaits `client.set_breakpoints(...)` in controller.py.

- [ ] **Step 1: Find the transmit sites**

Run: `grep -n "set_breakpoints(" src/tdb/session/controller.py`
Expected: 2–3 sites — one in `do_configure`, one in the runtime breakpoint-sync path (plus possibly run-to-cursor, which should be **excluded**: temporary run-to-cursor breakpoints failing to bind is handled by its own flow). Also confirm the handler signature: `grep -n "def on_output" src/tdb/session/event_bus.py` — expected `def on_output(self, category: str, output: str)`.

- [ ] **Step 2: Write the failing test**

Append to `tests/unit/test_controller_actions.py` (using this file's `_make()` + `_RecordingHandler`; set the fake's setBreakpoints result to unverified):

```python
async def test_unbound_breakpoints_warn_on_console():
    ctrl, dap, handler = _make()
    dap.breakpoint_results = [
        {"verified": False, "line": 3}
    ]  # adapt to _FakeDAP's config style
    await ctrl.set_breakpoint(
        "/x/prog.cpp", 3
    )  # use the real runtime-sync method name found in Step 1
    warnings = [o for o in handler.outputs if "debug info" in o[1]]
    assert warnings, "expected an unbound-breakpoint warning"


async def test_verified_breakpoints_do_not_warn():
    ctrl, dap, handler = _make()
    dap.breakpoint_results = [{"verified": True, "line": 3}]
    await ctrl.set_breakpoint("/x/prog.py", 3)
    assert not [o for o in handler.outputs if "debug info" in o[1]]
```

Adapt the two `dap.breakpoint_results` lines and the method name to `_FakeDAP`'s actual configuration surface and the controller's actual breakpoint-sync method (both visible in this test file's existing breakpoint CRUD tests — copy their arrange step).

- [ ] **Step 3: Implement** — in `controller.py`:

```python
def _warn_unbound_breakpoints(self, source_path: str, result: list[Breakpoint]) -> None:
    """A file whose breakpoints ALL failed to bind usually means the
    target has no debug info for it (native binary built without -g,
    or generated/stale source). Surface a hint on the console."""
    if result and all(not bp.verified for bp in result):
        self.event_handler.on_output(
            "console",
            f"warning: no breakpoints bound in {source_path} — "
            f"was the program compiled with debug info (-g)?\n",
        )
```

At each transmit site found in Step 1 (except run-to-cursor), capture the return value and call the helper:

```python
                result = await self.client.set_breakpoints(source_path, ...)
                self._warn_unbound_breakpoints(source_path, result)
```

Import `Breakpoint` from `tdb.dap.types` in controller.py if not present.

- [ ] **Step 4: Run** — `pytest tests/unit/test_controller_actions.py -q` → all pass.

- [ ] **Step 5: Full suite + commit**

```bash
pytest tests/unit -q
git add src/tdb/session/controller.py tests/unit/test_controller_actions.py
git commit -m "feat: warn when breakpoints fail to bind (missing debug info)"
```

---

### Task 15: C++ integration tests against real lldb-dap

**Files:**
- Create: `tests/integration/test_cpp_session.py`

**Interfaces:**
- Consumes: the `session`/`_launch`/`_resume_and_wait` patterns from `tests/integration/test_dap_session.py` — copy those fixtures and adapt (they take a controller; construct it with `profile=build_cpp_profile()`).

- [ ] **Step 1: Write the tests**

```python
# tests/integration/test_cpp_session.py
"""End-to-end: real lldb-dap debugging a real compiled C++ binary.

Skipped wholesale when lldb-dap or a C++ compiler is missing, so CI
without LLVM still passes.
"""

import shutil
import subprocess

import pytest

from tdb.languages import registry
from tdb.languages.cpp import build_cpp_profile

pytestmark = pytest.mark.skipif(
    shutil.which("lldb-dap") is None
    or (shutil.which("g++") is None and shutil.which("clang++") is None),
    reason="lldb-dap or C++ compiler not installed",
)

CPP_SRC = """\
#include <cstdio>

int add(int a, int b) {
    int result = a + b;
    return result;
}

int main() {
    int x = 5;
    int y = add(x, 7);
    printf("total=%d\\n", y);
    return 0;
}
"""
BP_LINE = 9  # int x = 5;


@pytest.fixture(scope="module")
def cpp_binary(tmp_path_factory):
    src = tmp_path_factory.mktemp("cppsrc") / "main.cpp"
    src.write_text(CPP_SRC)
    binary = src.parent / "main"
    cxx = shutil.which("g++") or shutil.which("clang++")
    subprocess.run([cxx, "-g", "-O0", "-o", str(binary), str(src)], check=True)
    return str(binary), str(src)


def test_registry_detects_compiled_binary_as_cpp(cpp_binary):
    binary, _src = cpp_binary
    assert registry.detect(binary) == "cpp"


# --- live-session tests: copy the `session` fixture + `_launch` +
# --- `_resume_and_wait` helpers from test_dap_session.py, with the
# --- controller constructed as:
# ---   DebugController(handler, profile=build_cpp_profile())
# --- and _launch() calling ctrl.start(program=binary, ...) with no
# --- python= kwarg.


async def test_breakpoint_hit_and_locals(session, cpp_binary):
    binary, src = cpp_binary
    ctrl, handler = session
    await _launch(ctrl, handler, binary, breakpoints=[(src, BP_LINE + 1)])
    # stopped at `int y = add(x, 7);` — x is already assigned
    frame = ctrl.state.stack_frames[0]
    assert frame.line == BP_LINE + 1
    result = await ctrl.evaluate("x")
    assert "5" in result


async def test_step_into_and_out(session, cpp_binary):
    binary, src = cpp_binary
    ctrl, handler = session
    await _launch(ctrl, handler, binary, breakpoints=[(src, BP_LINE + 1)])
    await _resume_and_wait(ctrl, handler, "step_in")
    assert ctrl.state.stack_frames[0].name.startswith("add")
    await _resume_and_wait(ctrl, handler, "step_out")
    assert ctrl.state.stack_frames[0].name.startswith("main")


async def test_run_to_completion_captures_output(session, cpp_binary):
    binary, _src = cpp_binary
    ctrl, handler = session
    await _launch(ctrl, handler, binary, stop_on_entry=True)
    await _continue_to_exit(ctrl, handler)
    assert any("total=12" in text for _cat, text in handler.outputs)


async def test_stop_terminates_debuggee(session, cpp_binary):
    binary, src = cpp_binary
    ctrl, handler = session
    await _launch(ctrl, handler, binary, breakpoints=[(src, BP_LINE)])
    await ctrl.stop()  # must not hang or raise
```

Replace the `# --- copy ...` comment block with the actual fixtures/helpers copied from `tests/integration/test_dap_session.py`, adjusted per the comment (profile kwarg, no `python=`, `_continue_to_exit` = the run-to-completion helper that file already has). lldb-dap sends the initialized event and holds launch like debugpy; the same `_launch` sequencing works. **Two adapter differences to expect while adapting:** (1) lldb-dap `evaluate` results for `int` are plain (`"5"`) — hence `assert "5" in result`; (2) stdout arrives as DAP `output` events with category `"stdout"` — the existing handler capture already records these.

- [ ] **Step 2: Verify environment + run**

Run: `which lldb-dap g++ || echo MISSING` — if MISSING, install (`sudo apt install lldb g++` or equivalent) or accept the suite will skip; at least one full local run against real lldb-dap is REQUIRED before commit — do not merge a skipped-only suite.
Run: `pytest tests/integration/test_cpp_session.py -v`
Expected: 5 passed (or all skipped only when tools genuinely absent — then install and re-run).

- [ ] **Step 3: Full integration suite + commit**

```bash
pytest tests/integration -q
git add tests/integration/test_cpp_session.py
git commit -m "test: C++ end-to-end integration against real lldb-dap"
```

---

### Task 16: `gdb -i dap` alternate adapter

**Files:**
- Modify: `src/tdb/languages/cpp.py`
- Test: `tests/unit/test_cpp_profile.py` (append), `tests/integration/test_gdb_session.py` (create)

**Interfaces:**
- Produces: `GdbDapAdapter(executable: str | None = None)` (`id="gdb"`), selectable via `build_cpp_profile(adapter="gdb")` → CLI `--lang cpp --adapter gdb` → config `{"default_adapters": {"cpp": "gdb"}}`. This task is the proof of the language≠adapter seam: zero changes outside `cpp.py` + tests.

- [ ] **Step 1: Write the failing unit tests** (append to `tests/unit/test_cpp_profile.py`):

```python
from tdb.languages.cpp import GdbDapAdapter


def test_gdb_adapter_selectable():
    p = build_cpp_profile(adapter="gdb")
    assert p.adapter.id == "gdb"
    assert p.id == "cpp"  # same language side


def test_gdb_command():
    assert GdbDapAdapter(executable="/usr/bin/gdb").command() == [
        "/usr/bin/gdb",
        "-i",
        "dap",
    ]


def test_gdb_command_missing_hints_gdb14(monkeypatch):
    import shutil as _sh

    monkeypatch.setattr(_sh, "which", lambda name: None)
    with pytest.raises(AdapterNotFoundError, match="GDB >= 14"):
        GdbDapAdapter().command()


def test_gdb_launch_body():
    body = GdbDapAdapter().launch_body(
        program="/x/prog",
        args=["a"],
        cwd="/x",
        env=None,
        stop_on_entry=True,
        console="internalConsole",
        opts={},
    )
    assert body == {
        "type": "gdb",
        "request": "launch",
        "program": "/x/prog",
        "args": ["a"],
        "cwd": "/x",
        "stopAtBeginningOfMainSubprogram": True,
    }
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/unit/test_cpp_profile.py -q` → new tests FAIL.

- [ ] **Step 3: Implement** — in `cpp.py` add:

```python
class GdbDapAdapter(AdapterSpec):
    """GDB's built-in DAP interpreter (`gdb -i dap`, GDB >= 14).

    Alternate C++ adapter: GDB's libstdc++ pretty-printers are more
    complete than LLDB's, which matters for heavily GCC codebases.
    """

    id = "gdb"

    def __init__(self, executable: str | None = None) -> None:
        self._executable = executable

    def command(self) -> list[str]:
        exe = self._executable or shutil.which("gdb")
        if exe is None:
            raise AdapterNotFoundError(
                "gdb not found on PATH — install GDB >= 14 (its DAP mode), "
                'or set {"adapters": {"gdb": "/path/to/gdb"}} in '
                "tdb's config.json"
            )
        return [exe, "-i", "dap"]

    def launch_body(
        self,
        *,
        program: str,
        args: list[str],
        cwd: str,
        env: dict[str, str] | None,
        stop_on_entry: bool,
        console: str,
        opts: dict[str, Any],
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": "gdb",
            "request": "launch",
            "program": program,
            "args": args,
            "cwd": cwd,
            # GDB's DAP name for stop-on-entry.
            "stopAtBeginningOfMainSubprogram": stop_on_entry,
        }
        if env:
            body["env"] = env  # GDB takes a mapping, unlike lldb-dap
        return body

    def attach_body(
        self, *, host: str, port: int, opts: dict[str, Any]
    ) -> dict[str, Any]:
        raise LanguageNotSupportedError(
            "remote attach is not supported for gdb -i dap yet"
        )
```

and in `build_cpp_profile` extend the adapters map:

```python
    adapters: dict[str, type[AdapterSpec]] = {
        "lldb-dap": LldbDapAdapter,
        "gdb": GdbDapAdapter,
    }
```

- [ ] **Step 4: Integration test** — create `tests/integration/test_gdb_session.py`, reusing Task 15's fixtures/helpers with `build_cpp_profile(adapter="gdb")` and a version-aware skip:

```python
def _gdb_supports_dap() -> bool:
    gdb = shutil.which("gdb")
    if gdb is None:
        return False
    out = subprocess.run([gdb, "--version"], capture_output=True, text=True).stdout
    m = re.search(r"(\d+)\.\d+", out)
    return bool(m) and int(m.group(1)) >= 14


pytestmark = pytest.mark.skipif(
    not _gdb_supports_dap()
    or (shutil.which("g++") is None and shutil.which("clang++") is None),
    reason="gdb >= 14 or C++ compiler not installed",
)
```

Two tests suffice (bp hit + evaluate; run to completion + output) — the shared machinery is already proven by Task 15. GDB difference to expect: `evaluate` may render ints as `"5"` or typed (`"(int) 5"`); keep the `"5" in result` containment assert.

- [ ] **Step 5: Run everything + commit**

Run: `pytest tests/unit -q && pytest tests/integration -q`
Expected: all pass (gdb suite runs if gdb ≥ 14 present, else skips — as with Task 15, one real local run is required before commit).

```bash
git add src/tdb/languages/cpp.py tests/unit/test_cpp_profile.py tests/integration/test_gdb_session.py
git commit -m "feat: gdb -i dap as alternate C++ adapter"
```

---

## Post-plan verification (after Task 16)

- [ ] Full suites: `pytest tests/unit -q && pytest tests/integration -q`.
- [ ] Manual smoke, Python regression: `tdb work/bug/main.py` — step, breakpoint, quit.
- [ ] Manual smoke, C++: compile the Task-15 fixture by hand, `tdb ./main`, verify: cpp lexer highlighting, breakpoint hit, locals shown, `t` shows the "not available" toast, menu bar has no Async Tasks / Processes entries. Also open the full-contents modal on a struct variable — `inspection_full.py`'s debugpy-tuned heuristics should degrade to expanding only what lldb-dap marks expandable (no code change expected; if it crashes, that's a bug to fix, not a plan gap).
- [ ] MCP smoke: `tdb --mcp` session with `debug_launch(program=<cpp binary>)`, then the `tasks` tool → expect the structured "Not supported when debugging C/C++" error.
- [ ] Update `README.md` and `work/SKILL.md` with the new flags and supported languages (small docs commit).

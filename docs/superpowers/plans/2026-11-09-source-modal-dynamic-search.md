# Source Modal Dynamic Search Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Resolve static imports with the debuggee interpreter, curate Source entries, and save reusable lists.

**Architecture:** Extend static discovery with optional import-request capture. Separate interpreter metadata and filesystem resolution from the Textual modal. Keep curation state in the app and share it with each modal instance.

**Tech Stack:** Python, subprocess, pathlib, Textual, pytest.

**Spec:** `docs/superpowers/specs/2026-11-09-source-modal-dynamic-search-design.md`

## Global Constraints

- Never import discovered modules or recurse into newly located source files.
- Use selected debuggee interpreter paths; skip nonexistent directories and default-filter stdlib.
- Preserve source-list loading compatibility, navigation, and unsaved-edit protection.
- Removals affect the list only and persist for the app session.

## Review Focus

- Project modules named like helper dependencies must not execute during metadata collection.
- Standard library roots can contain third-party site-packages, which must remain eligible.
- Closing the modal while discovery runs must not cause widget access after unmount.
- Empty lists, failed saves, and repeated searches must preserve curation.
- Run-mode and replay app construction must retain interpreter and filename settings.

### Task 1: Discovery and persistence primitives

**Files:** `src/tdb/source_discovery.py`, new `src/tdb/source_search.py`, `tests/unit/test_source_discovery.py`, new `tests/unit/test_source_search.py`.

**Interfaces:** `ImportRequest(name: str, roots: tuple[Path, ...], relative: bool = False)` captures local roots and relative context; `discover_sources(..., requests: list[ImportRequest] | None = None)` retains its path-list return. `search_module_files(requests, *, python=None, cwd=None, just_my_code=True) -> list[Path]` resolves only these requests. `write_source_list(filename: Path, paths: list[Path]) -> None` writes atomically. `SourceSelection` owns discovered paths, excluded paths, and optional save filename.

- [x] Write and run failing tests for unresolved-name capture, one-pass resolution, namespace/packages, selected interpreter metadata, stdlib/site filtering, shadowing protection, and atomic save round trips/errors.
- [x] Implement request capture, metadata query with timeout, filesystem resolution, and atomic serialization.
- [x] Run `pytest tests/unit/test_source_discovery.py tests/unit/test_source_search.py -q --no-cov`; expect all passing.

### Task 2: Modal curation and app wiring

**Files:** `src/tdb/widgets/source_modal.py`, `src/tdb/app.py`, `tests/unit/test_source_navigation.py`.

**Interfaces:** Add optional shared `selection: SourceSelection` and `dynamic_search: Callable[[], list[Path]]` to `SourceModal`. App captures static requests on open, merges refreshed static paths with session additions, and passes a search callback using its interpreter/cwd/just-my-code settings.

- [x] Add failing keyboard tests for d/x/s, empty-state recovery, deletion cursor, save prompt/cancel/failure, close during search, session persistence, and repeated searches respecting exclusions.
- [x] Implement asynchronous discovery, list refresh, removal, filename input and saving; retain current static refresh on reopen and apply exclusions afterward.
- [x] Run `pytest tests/unit/test_source_navigation.py -q --no-cov`; expect existing and new tests passing.

### Task 3: CLI propagation and documentation

**Files:** `src/tdb/cli.py`, `src/tdb/run_mode.py`, `tests/unit/test_cli_source.py`, `docs/using/keybindings.md`, source-navigation user documentation.

**Interfaces:** Add optional `source_filename: str | None` alongside `source_files` in app, replay builder, and run-mode functions. Run-mode TUI receives `python` and `just_my_code` along with source settings.

- [x] Extend CLI tests to assert filename propagation and run-mode/replay interpreter settings; run them before implementation and verify expected failures.
- [x] Wire parameters through all TUI paths and document d/x/s, one-pass search, stdlib filtering, and session/save semantics.
- [x] Run focused discovery/navigation/CLI regressions and relevant run-mode tests; expect all passing. Run lint and `git diff --check`.
- [x] Review the complete diff against the spec and report verification evidence and limitations.

## Execution record

- Execute inline in the existing feature checkout `source-modal-dynamic-discovery`; design approval authorizes implementation without another workflow approval.
- Preserve current refresh-on-open behavior by combining refreshed static paths with persistent dynamic additions and exclusions.

- Discovery and CLI tests first failed for missing APIs/filename propagation, then passed with implementation. UI tests first failed for missing actions/state.
- Test environment: `uv run pytest` lacks pytest; use `/home/al/venvs/work/bin/python -m pytest`. Asynchronous UI tests stall inside the sandbox; verification runs outside it.
- Independent review identified cancellation of modal-owned save workers while their write threads continued. Added a blocked-writer regression, moved saves to app-owned workers, and shared the busy flag across reopened modals. The regression passes.
- Added rejection of Unicode line separators in saved paths because the existing reader splits them into separate entries.
- Fixed the app-level test to await individual matching workers: Textual's `wait_for_complete([])` waits for all workers, including the source-navigation worker waiting for the modal to close.
- Final verification: 141 focused discovery, modal, CLI, replay, and run-mode tests passed (including run-mode integration). New/changed discovery and modal files pass Ruff; app/CLI/run-mode fatal-error lint and git diff --check pass. The integration run emitted an asyncio closed-loop child-process teardown message but exited successfully.

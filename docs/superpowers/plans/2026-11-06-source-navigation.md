# Source Navigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Browse statically discovered Python sources and explicit source files without changing the debug target.

**Architecture:** A standalone discovery module collects resolved paths. A dedicated Textual modal displays only those paths; the app handles safe navigation. CLI parsing validates the additive list and propagates it to interactive entry points.

**Tech Stack:** Python 3.11+, ast, pathlib, Textual, pytest.

**Spec:** `docs/superpowers/specs/2026-11-06-source-navigation-design.md`

## Global Constraints

- Static discovery never executes imports or searches PYTHONPATH/installed libraries.
- Explicit files supplement discovery, with paths relative to the list file.
- Source selection preserves the debug target, execution state and selected frame.
- Use the existing `source-navigation-modal` branch requested by the user.

## Review Focus

- Cyclic imports and namespace packages must terminate and resolve correctly.
- Files outside the working directory must remain distinguishable and selectable.
- Invalid list files must fail clearly; individual missing entries must not hide valid files.
- Unsaved edits must survive cancellation of navigation.
- Attach and --run sessions must retain source configuration without requiring restart support.

### Task 1: Static discovery and file lists

**Files:** Create `src/tdb/source_discovery.py` and `tests/unit/test_source_discovery.py`.

**Interfaces:** `read_source_list(filename: str) -> tuple[list[Path], list[str]]`; `discover_sources(program: str, cwd: str | None = None, additional: Iterable[Path] = (), *, python: bool = True) -> list[Path]`.

- [x] Write tests asserting exact source sets for direct, dotted, aliased, from and relative imports, package initializers, namespace packages, cycles, malformed source and absent modules. Test additive lists, literal filenames, duplicates, external paths and invalid list files.
- [x] Run `PYTHONPATH=src /home/al/venvs/work/bin/python -m pytest tests/unit/test_source_discovery.py --no-cov -q`; expect missing-module failure before implementation.
- [x] Implement path collection with iterative AST traversal and a visited set; resolve local imports without importing modules. Parse UTF-8 lists with warnings for invalid entries.
- [x] Run the same tests; expect all passing.

### Task 2: Source tree and safe app navigation

**Files:** Create `src/tdb/widgets/source_modal.py` and `tests/unit/test_source_navigation.py`; modify `src/tdb/app.py` and shortcut documentation in `src/tdb/widgets/modals.py` if applicable.

**Interfaces:** `SourceModal(paths: list[Path], cwd: Path)` returns a selected absolute path or None. `TdbApp(..., source_files: list[Path] | None = None)` retains additional source paths.

- [x] Write Textual tests for fully expanded sorted trees, directory toggling, up/down and j/k selection, Escape, empty and external paths; app tests assert Code View selection, preserved session state, refreshed files and dirty-edit cancellation.
- [x] Run `PYTHONPATH=src /home/al/venvs/work/bin/python -m pytest tests/unit/test_source_navigation.py --no-cov -q`; expect missing-feature failures.
- [x] Populate an eager Tree with directory nodes and file-path leaf data. Add Source after File, bind Alt-S, discover off the event loop and navigate through the existing unsaved-edits guard. Update breakpoint and execution-line markers for the selected file.
- [x] Run the same tests plus existing file-picker and edit-mode regression tests; expect all passing.

### Task 3: CLI integration and documentation

**Files:** Modify `src/tdb/cli.py`, `src/tdb/run_mode.py`, `docs/reference/cli.md`, `docs/using/navigation.md` if present; create `tests/unit/test_cli_source.py`.

**Interfaces:** `--source=FILE` populates `args.source_files`; TUI and run-mode entry points receive those paths. Replay and post-mortem TUI construction also carry explicit files.

- [x] Write tests for CLI validation, warning behavior and propagation to ordinary, attach, run, replay and post-mortem UI consumers.
- [x] Run `PYTHONPATH=src /home/al/venvs/work/bin/python -m pytest tests/unit/test_cli_source.py --no-cov -q`; expect unrecognized-option failures.
- [x] Add parsing before early-return entry modes and wire source paths through the relevant app constructors, preserving existing run-mode callback contracts.
- [x] Document discovery scope, file-list syntax, path resolution, Source shortcut and navigation behavior.
- [x] Run new tests, related CLI/run-mode regression suites, lint and `git diff --check`; expect all passing.
- [x] Obtain a fresh whole-change review and resolve significant findings with regression tests.
- [x] Complete final combined verification and commit on the requested branch.

## Verification results

- Final related regression suite: 244 passed (29.81s), including discovery, Source UI, CLI, editing, existing file pickers, attach, replay and run mode.
- New Python files pass Ruff; all eight changed Python files pass formatting checks. `git diff --check` passes.
- Independent review findings addressed: extensionless shebang scripts, regular-package precedence over namespace directories, and trees spanning Windows drives. Each has regression coverage.
- Alt-S takes priority in editing modes; tests cover default, Emacs and Vim, plus save/discard/cancel navigation.
- Tests requiring socket access or thread wakeups ran outside the sandbox after sandbox-specific failures were diagnosed.

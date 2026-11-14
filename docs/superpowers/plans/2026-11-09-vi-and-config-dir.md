# `--vi` and `--config-dir` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `--config-dir DIR` (all of tdb's files, and the write locations of every tool tdb drives, move into DIR) and `--vi FILE` (tdb becomes a full-screen vim-style editor).

**Architecture:** The config dir becomes a lazily resolved value (`TDB_CONFIG_DIR` env var > platform default) instead of import-time constants, so `cli.main()` can set it and every spawned process inherits it. Tool write redirects are filled into `os.environ` once at startup, which every child inherits. `--vi` is a small dedicated Textual `App` (`ViApp`) that reuses `CodeView`/`CodeEditor` with a "standalone" switch; `TdbApp` and the debug controller are untouched.

**Tech Stack:** Python, argparse, Textual (Pilot for UI tests), pytest via `uv run pytest`.

**Spec:** `docs/superpowers/specs/2026-11-09-vi-and-config-dir-design.md`

## Global Constraints

- Precedence for the config dir: `--config-dir` > `TDB_CONFIG_DIR` > platform default (`~/.config/tdb`, Windows `%APPDATA%\tdb`).
- Files live directly in DIR: `DIR/config.json`, `DIR/breakpoints.json`, `DIR/tdb.log`, `DIR/padwalker/`, `DIR/server-<port>.token`.
- `TDB_LOG_DIR` still wins for `tdb.log`; `TDB_PADWALKER_CACHE` still wins for the PadWalker cache.
- Every config-dir write tolerates `OSError` (read-only filesystem rule): warn, never crash.
- Tool redirects: write locations only, never install/lookup locations; only fill variables that are unset; only when a config dir override is in effect.
- `--vi`: exactly one file, not a directory; no adapter, debuggee, or `tdb` subprocess is ever started.
- Cross-platform: Windows paths tested by monkeypatching `sys.platform`; no POSIX-only assumptions in new code.
- Full-suite runs use the memcap-pytest wrapper (see memory: OOM incident).

## Review Focus

1. `--config-dir relative/path` run from one cwd, child `tdb` started with another cwd → child must use the same absolute dir (Task 2 test `test_config_dir_made_absolute`).
2. User already set `GOCACHE` / `XDG_CACHE_HOME` themselves → tdb must not overwrite it (Task 3 test `test_user_value_wins`).
3. `tdb --vi` on a symlink to a file in a read-only dir → direct-write fallback must write through to the real target, not replace the link (Task 4 test `test_fallback_writes_through_symlink`).
4. `tdb --vi newfile` then `:q` with nothing typed → no file created on disk (Task 6 test `test_new_file_quit_creates_nothing`).
5. `tdb --vi FILE` where FILE's parent dir does not exist → error at startup, not a TUI whose `:w` fails (Task 6 test `test_missing_parent_dir_rejected`).

---

### Task 1: Lazy config-dir resolution in `persist`

**Files:**
- Modify: `src/tdb/persist.py:18-38` and every use of `CONFIG_DIR`, `STATE_FILE`, `CONFIG_FILE`, `_LEGACY_STATE_FILE` in it
- Modify: `src/tdb/adapters/perl/padwalker.py:104-106`, `src/tdb/server/auth.py:54-56`, `src/tdb/info.py:216-224`, `src/tdb/cli.py` (any `CONFIG_DIR` use)
- Modify: `tests/unit/test_persist.py`, `tests/unit/test_info.py`, `tests/unit/test_cli_config.py`, `tests/conftest.py`

**Interfaces:**
- Produces (in `tdb.persist`):
  - `CONFIG_DIR_ENV = "TDB_CONFIG_DIR"`
  - `config_dir() -> Path` — `Path(os.environ[CONFIG_DIR_ENV])` when set and non-empty, else `_default_config_dir()`
  - `config_dir_overridden() -> bool` — True when `CONFIG_DIR_ENV` is set and non-empty
  - `state_file() -> Path`, `config_file() -> Path`, `legacy_state_file() -> Path` (`config_dir()` / `breakpoints.json`, `config.json`, `last_run.json`)
  - The module constants `CONFIG_DIR`, `STATE_FILE`, `CONFIG_FILE`, `_LEGACY_STATE_FILE` are **removed** (so a stale import fails loudly).

- [ ] **Step 1: Write failing tests** in `tests/unit/test_persist.py`

```python
def test_config_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("TDB_CONFIG_DIR", str(tmp_path / "cfg"))
    from tdb import persist

    assert persist.config_dir() == tmp_path / "cfg"
    assert persist.config_file() == tmp_path / "cfg" / "config.json"
    assert persist.state_file() == tmp_path / "cfg" / "breakpoints.json"
    assert persist.config_dir_overridden() is True


def test_config_dir_env_override_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    monkeypatch.setenv("TDB_CONFIG_DIR", str(tmp_path / "cfg"))
    from tdb import persist

    assert persist.config_dir() == tmp_path / "cfg"  # APPDATA bypassed


def test_empty_env_means_default(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TDB_CONFIG_DIR", "")
    from tdb import persist

    assert persist.config_dir() == tmp_path / ".config" / "tdb"
    assert persist.config_dir_overridden() is False


def test_log_file_follows_config_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("TDB_LOG_DIR", raising=False)
    monkeypatch.setenv("TDB_CONFIG_DIR", str(tmp_path))
    from tdb import persist

    assert persist.log_file() == tmp_path / "tdb.log"
```

Also in `tests/unit/test_perl_padwalker*.py` (whichever tests `cache_root`): `test_cache_root_follows_config_dir` — with `TDB_PADWALKER_CACHE` unset and `TDB_CONFIG_DIR=tmp`, `cache_root() == tmp / "padwalker"`. And in the auth tests: `token_file_path(5000) == tmp / "server-5000.token"`.

- [ ] **Step 2: Run** `uv run pytest tests/unit/test_persist.py -k "config_dir or log_file_follows" -v` — Expected: FAIL (`AttributeError: ... config_dir`).

- [ ] **Step 3: Implement** the Interfaces above; convert every internal use to the functions. Rewrite the `isolated_persist` fixture to `monkeypatch.setenv("TDB_CONFIG_DIR", str(tmp_path / ".config" / "tdb"))` (no reload needed) and replace constant assertions (`persist_module.CONFIG_DIR == ...`) with `config_dir()` calls; `_default_config_dir` tests keep testing the platform default directly. Add an autouse fixture in `tests/conftest.py` that `monkeypatch.delenv("TDB_CONFIG_DIR", raising=False)` so a developer's own setting never leaks into tests.

- [ ] **Step 4: Run** `uv run pytest tests/unit/test_persist.py tests/unit/test_info.py tests/unit/test_cli_config.py tests/unit -k "padwalker or auth or token" -v` — Expected: PASS. Then `grep -rn "CONFIG_DIR\b\|STATE_FILE\|CONFIG_FILE\|_LEGACY_STATE_FILE" src tests` — Expected: only `CONFIG_DIR_ENV` hits.

- [ ] **Step 5: Commit** — `refactor: resolve tdb config dir lazily via TDB_CONFIG_DIR`

### Task 2: `--config-dir` CLI switch

**Files:**
- Create: `src/tdb/config_dir.py`
- Modify: `src/tdb/cli.py` (`build_parser`, `main` before log setup), `src/tdb/info.py` (config dir row)
- Test: `tests/unit/test_cli_config_dir.py`

**Interfaces:**
- Consumes: `persist.CONFIG_DIR_ENV`, `persist.config_dir()`, `persist.config_dir_overridden()` (Task 1)
- Produces (in `tdb.config_dir`):
  - `apply_config_dir(raw: str | None) -> Path | None` — if `raw` is given: `Path(raw).expanduser()`, made absolute with `os.path.abspath` against the current cwd, stored in `os.environ[CONFIG_DIR_ENV]`. Then, if `config_dir_overridden()`, `mkdir(parents=True, exist_ok=True)`; on `OSError` print once to stderr `tdb: warning: cannot create config dir {dir}: {exc}` and continue; then call `tool_env.apply_tool_redirects(dir)` (Task 3 — in this task, leave the call out; Task 3 adds it). Returns the dir when overridden, else `None`.
- argparse: `parser.add_argument("--config-dir", metavar="DIR", default=None, help=...)`. It is **not** a short-circuit mode: it combines with everything.

- [ ] **Step 1: Write failing tests** in `tests/unit/test_cli_config_dir.py`

```python
def test_flag_sets_env(monkeypatch, tmp_path):
    monkeypatch.delenv("TDB_CONFIG_DIR", raising=False)
    assert apply_config_dir(str(tmp_path / "c")) == tmp_path / "c"
    assert os.environ["TDB_CONFIG_DIR"] == str(tmp_path / "c")
    assert (tmp_path / "c").is_dir()

def test_config_dir_made_absolute(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    apply_config_dir("rel")
    assert os.environ["TDB_CONFIG_DIR"] == str(tmp_path / "rel")

def test_tilde_expanded(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path)); monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert apply_config_dir("~/x") == tmp_path / "x"

def test_flag_beats_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TDB_CONFIG_DIR", str(tmp_path / "env"))
    assert apply_config_dir(str(tmp_path / "flag")) == tmp_path / "flag"

def test_env_alone_is_honored(monkeypatch, tmp_path):
    monkeypatch.setenv("TDB_CONFIG_DIR", str(tmp_path / "env"))
    assert apply_config_dir(None) == tmp_path / "env"

def test_no_override_returns_none(monkeypatch):
    monkeypatch.delenv("TDB_CONFIG_DIR", raising=False)
    assert apply_config_dir(None) is None

def test_unwritable_dir_warns_and_continues(monkeypatch, tmp_path, capsys):
    ro = tmp_path / "ro"; ro.mkdir(); ro.chmod(0o500)
    apply_config_dir(str(ro / "c"))          # must not raise
    assert "cannot create config dir" in capsys.readouterr().err
    # skip on win32 / root (chmod has no effect) — use the suite's existing skip helper

def test_write_default_goes_to_config_dir(monkeypatch, tmp_path):
    # `tdb --config-dir D --config write-default` via cli.main → D/config.json exists
    ...

def test_info_shows_config_dir(monkeypatch, tmp_path):
    # info_text() with TDB_CONFIG_DIR set contains a "config dir" row with the path
    # and "(from --config-dir/TDB_CONFIG_DIR)"

def test_child_process_inherits(monkeypatch, tmp_path):
    apply_config_dir(str(tmp_path / "c"))
    out = subprocess.run([sys.executable, "-c",
        "from tdb import persist; print(persist.config_dir())"],
        capture_output=True, text=True, check=True).stdout.strip()
    assert out == str(tmp_path / "c")
```

- [ ] **Step 2: Run** `uv run pytest tests/unit/test_cli_config_dir.py -v` — Expected: FAIL (module missing).

- [ ] **Step 3: Implement** `tdb/config_dir.py`, the argparse flag, `apply_config_dir(args.config_dir)` as the first statement after `parse_args` in `cli.main()` (before `log_file()`), and an `info.py` `row("config dir", ...)` that appends ` (from --config-dir/TDB_CONFIG_DIR)` when overridden. Add `--config-dir` to `docs/reference/cli.md` and a "Read-only home directory" paragraph to `docs/reference/configuration.md`.

- [ ] **Step 4: Run** `uv run pytest tests/unit/test_cli_config_dir.py tests/unit/test_info.py tests/unit/test_cli_config.py tests/unit/test_cli.py -v` — Expected: PASS.

- [ ] **Step 5: Commit** — `feat: add --config-dir switch`

### Task 3: Redirect tool write locations into DIR

**Files:**
- Create: `src/tdb/tool_env.py`
- Modify: `src/tdb/config_dir.py` (call `apply_tool_redirects`), plus any child-env site found by Step 3's audit that does not inherit `os.environ`
- Test: `tests/unit/test_tool_env.py`

**Interfaces:**
- Consumes: `apply_config_dir` (Task 2)
- Produces (in `tdb.tool_env`):
  - `tool_redirects(config_dir: Path) -> dict[str, str]` — the full table below, all paths under `config_dir / "tools"`, independent of the current environment.
  - `apply_tool_redirects(config_dir: Path, environ: MutableMapping[str, str] = os.environ) -> dict[str, str]` — sets each key only when absent or empty in `environ`, creates no directories (tools create their own), returns what it set.

Starting table (the builder verifies each against the adapter code and the tool's docs, and adds any write location found for lldb-dap and PSES; drop an entry rather than guess):

| Variable | Value under `DIR/tools` | Notes |
|---|---|---|
| `XDG_CACHE_HOME` | `cache` | covers pip, pwsh, debuginfod, lldb caches on Unix |
| `XDG_STATE_HOME` | `state` | |
| `GOCACHE` | `go/build` | |
| `GOMODCACHE` | `go/mod` | only when `GOPATH` unset |
| `DEBUGINFOD_CACHE_PATH` | `gdb/debuginfod` | |
| `GDBHISTFILE` | `gdb/history` | |
| `RUBY_DEBUG_HISTORY_FILE` | `ruby/rdbg_history` | |
| `DUNE_CACHE_ROOT` | `ocaml/dune` | |
| `PIP_CACHE_DIR` | `pip` | XDG doesn't apply on Windows |

Never set: `HOME`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME` (they hold things tools and the debuggee *read*), `GOROOT`, `GOPATH`, `CARGO_HOME`, `RUSTUP_HOME`, `GEM_HOME`, `OPAMROOT`.

- [ ] **Step 1: Write failing tests** in `tests/unit/test_tool_env.py`

```python
def test_sets_unset_vars(tmp_path):
    env = {}
    apply_tool_redirects(tmp_path, env)
    assert env["GOCACHE"] == str(tmp_path / "tools" / "go" / "build")
    assert env["XDG_CACHE_HOME"] == str(tmp_path / "tools" / "cache")


def test_user_value_wins(tmp_path):
    env = {"GOCACHE": "/mine", "XDG_CACHE_HOME": "/c"}
    apply_tool_redirects(tmp_path, env)
    assert env["GOCACHE"] == "/mine" and env["XDG_CACHE_HOME"] == "/c"


def test_never_touches_lookup_locations(tmp_path):
    env = {}
    apply_tool_redirects(tmp_path, env)
    for k in (
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "GOROOT",
        "GOPATH",
        "CARGO_HOME",
        "RUSTUP_HOME",
        "GEM_HOME",
        "OPAMROOT",
    ):
        assert k not in env


def test_gomodcache_skipped_when_gopath_set(tmp_path):
    env = {"GOPATH": "/g"}
    apply_tool_redirects(tmp_path, env)
    assert "GOMODCACHE" not in env


def test_not_applied_without_override(monkeypatch):
    monkeypatch.delenv("TDB_CONFIG_DIR", raising=False)
    monkeypatch.delenv("GOCACHE", raising=False)
    apply_config_dir(None)
    assert "GOCACHE" not in os.environ


def test_applied_with_override(monkeypatch, tmp_path):
    monkeypatch.delenv("GOCACHE", raising=False)
    apply_config_dir(str(tmp_path))
    assert os.environ["GOCACHE"].startswith(str(tmp_path))
```

Integration, `tests/integration/test_readonly_home.py`: for each language whose toolchain is installed (use the suites' existing skip markers), run a minimal headless session (`--run` or the existing harness used by that language's integration tests) on a packaged example with `HOME` set to a `chmod 0o500` temp dir and `--config-dir` set to a writable one; assert exit success and that nothing was created under `HOME` (snapshot `os.walk` before/after). Skip the whole module on win32 and when running as root.

- [ ] **Step 2: Run** `uv run pytest tests/unit/test_tool_env.py -v` — Expected: FAIL.

- [ ] **Step 3: Implement** `tool_env.py`; call it from `apply_config_dir`. Audit child envs: `grep -rn "env=" src/tdb` — every site must start from `os.environ` (e.g. `{**os.environ, ...}` or `env=None`). `python_attach.py:751` filters `os.environ` and is fine; fix any site building an env from scratch.

- [ ] **Step 4: Run** `uv run pytest tests/unit/test_tool_env.py tests/integration/test_readonly_home.py -v` — Expected: PASS (languages without toolchains SKIPPED). Fix any tool that still writes under `HOME` by adding its variable to the table.

- [ ] **Step 5: Commit** — `feat: redirect tool write locations into --config-dir`

### Task 4: Direct-write fallback for saves

**Files:**
- Modify: `src/tdb/source_edit.py` (add function next to `atomic_write_text`), `src/tdb/widgets/code_view.py:793-815` (`save_file` calls it)
- Test: `tests/unit/test_source_edit.py` (create if absent)

**Interfaces:**
- Produces: `save_text(path: str, text: str) -> None` in `tdb.source_edit` — calls `atomic_write_text`; on `OSError` whose `errno` is in `{EACCES, EPERM, EROFS, EBUSY, EXDEV}` (and `PermissionError` on Windows), retries as a direct write: `open(os.path.realpath(path), "w", encoding="utf-8", newline="")`, write, flush, `os.fsync`. Re-raises the **direct write's** error if it also fails. `atomic_write_text` already removes its temp file on failure; keep that.

- [ ] **Step 1: Write failing tests**

```python
def test_fallback_when_dir_readonly(tmp_path):      # skip win32/root
    f = tmp_path / "d" / "f.txt"; f.parent.mkdir(); f.write_text("old")
    f.parent.chmod(0o500)
    save_text(str(f), "new")
    assert f.read_text() == "new"
    assert [p.name for p in f.parent.iterdir()] == ["f.txt"]   # no temp left

def test_fallback_on_ebusy(tmp_path, monkeypatch):
    f = tmp_path / "hosts"; f.write_text("old")
    def busy(*a): raise OSError(errno.EBUSY, "busy")
    monkeypatch.setattr(os, "replace", busy)
    save_text(str(f), "new")
    assert f.read_text() == "new"

def test_fallback_writes_through_symlink(tmp_path):   # skip win32/root
    real = tmp_path / "real.txt"; real.write_text("old")
    d = tmp_path / "ro"; d.mkdir(); (d / "link").symlink_to(real); d.chmod(0o500)
    save_text(str(d / "link"), "new")
    assert real.read_text() == "new" and (d / "link").is_symlink()

def test_both_fail_raises(tmp_path):                 # skip win32/root
    f = tmp_path / "f"; f.write_text("x"); f.chmod(0o400); tmp_path.chmod(0o500)
    with pytest.raises(OSError):
        save_text(str(f), "y")

def test_other_errors_not_retried(tmp_path, monkeypatch):
    # os.replace raising ENOSPC → OSError propagates, file unchanged
```

Plus in `tests/unit/test_code_view_edit_mode.py`: `test_save_uses_fallback` — monkeypatch `os.replace` to raise EBUSY, `:w` in Edit mode, file content updated, buffer clean.

- [ ] **Step 2: Run** `uv run pytest tests/unit/test_source_edit.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** `save_text`; switch `CodeView.save_file` to it.
- [ ] **Step 4: Run** `uv run pytest tests/unit/test_source_edit.py tests/unit/test_code_view_edit_mode.py tests/unit/test_app_edit_mode.py -v` — Expected: PASS.
- [ ] **Step 5: Commit** — `feat: fall back to direct write when atomic save fails`

### Task 5: Standalone switches on `CodeEditor` and `CodeView`

**Files:**
- Modify: `src/tdb/widgets/code_editor.py` (`CodeEditor.__init__`, `check_consume_key`, `_on_key` Esc branch, `ViLayer._translate`/the `_VI_TDB_KEYS` check at ~576)
- Modify: `src/tdb/widgets/code_view.py` (`leave_edit_mode`, new methods/message)
- Test: `tests/unit/test_code_editor_vim_standalone.py`, `tests/unit/test_code_view_standalone.py`

**Interfaces:**
- Produces:
  - `CodeEditor(..., standalone: bool = False)`. When True: Esc with nothing pending is swallowed (no `LeaveRequested`); the only keys tdb keeps from the vi engine are `{"ctrl+q", "ctrl+s"}` (`_VI_STANDALONE_TDB_KEYS`); `check_consume_key` claims `ctrl+p` and `ctrl+c` so App priority bindings don't eat them.
  - `CodeView.standalone: bool = False` — passed to `CodeEditor` in `enter_edit_mode`.
  - `CodeView.EditClosed(Message)` — posted instead of tearing the editor down when `standalone` is True and Edit mode is left (clean, saved, or discarded).
  - `CodeView.confirm_unsaved(then: Callable[[], None]) -> None` — if editor clean or absent, calls `then()` now; else shows `_UnsavedChangesModal`: save → `save_file()` and `then()` only if the save succeeded; discard → `then()`; cancel → refocus editor. `leave_edit_mode` is rewritten on top of it (behavior unchanged when not standalone).
  - `CodeView.load_new_file(path: str) -> None` — installs an empty buffer with `source_path = path`, `is_local=True`, no trailing newline, without touching disk.

- [ ] **Step 1: Write failing tests** (Pilot, same harness style as `tests/unit/test_code_editor_vim.py`)

```python
async def test_standalone_escape_does_not_leave():   # Esc, Esc in normal mode → no LeaveRequested posted
async def test_standalone_ctrl_f_ctrl_b_page():      # 200-line buffer: ctrl+f moves cursor row > 20; ctrl+b returns toward 0
async def test_standalone_ctrl_b_not_app_binding():  # App with Binding("ctrl+b", ...) counter: counter stays 0
async def test_standalone_ctrl_s_still_saves():      # SaveRequested posted
async def test_non_standalone_unchanged():           # Esc → LeaveRequested; ctrl+b reaches App binding
async def test_view_standalone_q_clean_posts_editclosed():  # ":q\r" → EditClosed received, editor still mounted
async def test_view_standalone_q_dirty_prompts():    # edit, ":q\r" → _UnsavedChangesModal on screen; "escape" → still editing, no EditClosed
async def test_view_standalone_q_dirty_save():       # modal "s" → file written, EditClosed
async def test_view_standalone_q_bang_discards():    # ":q!\r" → EditClosed, file unchanged on disk
async def test_view_standalone_wq():                 # ":wq\r" → saved, EditClosed
async def test_load_new_file_touches_nothing(tmp_path):  # load_new_file(p); enter_edit_mode() is True; not p.exists()
async def test_confirm_unsaved_save_failure_does_not_continue():  # save raises → then() not called, still dirty
```

- [ ] **Step 2: Run** `uv run pytest tests/unit/test_code_editor_vim_standalone.py tests/unit/test_code_view_standalone.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** the Interfaces.
- [ ] **Step 4: Run** the two new files plus `tests/unit/test_code_editor_vim.py tests/unit/test_code_view_edit_mode.py tests/unit/test_app_edit_mode.py` — Expected: PASS.
- [ ] **Step 5: Commit** — `feat: standalone mode for CodeEditor/CodeView`

### Task 6: `ViApp` and the `--vi` switch

**Files:**
- Create: `src/tdb/vi_app.py`
- Modify: `src/tdb/cli.py` (`build_parser`, early short-circuit in `parse_args`, `main` dispatch)
- Modify: `docs/reference/cli.md`, `docs/using/vim-edit-mode.md` (a "Using tdb as an editor" section)
- Test: `tests/unit/test_vi_app.py`, `tests/unit/test_cli_vi.py`

**Interfaces:**
- Consumes: Task 5 (`standalone`, `EditClosed`, `confirm_unsaved`, `load_new_file`), Task 2 (`apply_config_dir`), `MenuBar`, `_OpenFileModal(initial_path, suffixes=())`, `_DocumentationModal`, `_AboutModal`, `persist.load_config()` (theme only)
- Produces:
  - `validate_vi_target(path: str) -> str | None` in `tdb.vi_app` — returns an error message or None. Errors: directory → `"{path} is a directory"`; parent dir missing → `"{parent}: no such directory"`; existing file with NUL in first `_BINARY_PROBE_BYTES` → `"{path} is a binary file"`; not valid UTF-8 → `"{path} is not valid UTF-8"`; unreadable → the `OSError` text.
  - `class ViApp(App)`: `__init__(self, path: str)`. Composes `MenuBar({"Help": ["Documentation", "About"]}, leading_action_labels={"open-file-label": "File"}, right_menus=["Help"], id="menu-bar")` and a `CodeView(id="code-view")` with `standalone=True`, filling the screen. `on_mount`: keybindings forced to vim (`KeybindingConfig.from_scheme("vim")`), theme from `load_config()`, `load_file` (or `load_new_file` when absent), `await enter_edit_mode()`. Bindings: `ctrl+q` → `code_view.leave_edit_mode()`; `alt+f` → open file; `alt+h` → Help menu (mirror `TdbApp`'s Alt-letter handling, including the `ctrl+right` alias for Alt+F per memory). `on_code_view_edit_closed` → `self.exit(0)`. File: `_OpenFileModal(os.getcwd())` with no suffix filter; on pick, `validate_vi_target` (notify and stay on error) then `confirm_unsaved(lambda: <tear down editor, load picked file, enter_edit_mode>)`.
  - argparse: `parser.add_argument("--vi", action="store_true", ...)`. In `parse_args`, immediately after `parser.parse_args(argv)` and before `_apply_flag_implications`: if `args.vi`, take the file list from `args.program` + `args.args`; zero → `parser.error("--vi expects a file name")`; more than one → `parser.error("--vi takes exactly one file name")`; `validate_vi_target` error → `parser.error(msg)`; set `args.vi_file` and return. `main`: right after `apply_config_dir`, `if args.vi: ViApp(args.vi_file).run(); sys.exit(0)` — logging setup still runs first.

- [ ] **Step 1: Write failing tests**

`tests/unit/test_cli_vi.py`:
```python
def test_vi_no_file_errors(capsys):            # parse_args(["--vi"]) → SystemExit(2), "--vi expects a file name" in stderr
def test_vi_two_files_errors(tmp_path):        # two names → SystemExit(2), "exactly one"
def test_vi_directory_errors(tmp_path):        # tmp_path → SystemExit(2), "is a directory"
def test_missing_parent_dir_rejected(tmp_path):# tmp_path/"nope"/"f" → SystemExit(2), "no such directory"
def test_vi_binary_rejected(tmp_path):         # b"\x00ELF" file → "binary"
def test_vi_ignores_debug_switches(tmp_path):  # ["--vi", f, "-k", "x:3", "--timing"] parses, args.vi_file == f
def test_vi_keeps_config_dir(tmp_path):        # ["--config-dir", d, "--vi", f] → args.config_dir == d
def test_vi_new_file_ok(tmp_path):             # nonexistent file in existing dir → accepted
def test_vi_starts_no_subprocess(monkeypatch, tmp_path):
    # monkeypatch subprocess.Popen and asyncio.create_subprocess_exec to raise;
    # run ViApp(f) under Pilot, press ":q\r" → app exits cleanly
```

`tests/unit/test_vi_app.py` (Pilot):
```python
async def test_layout_full_screen():           # only #menu-bar and #code-view; no ConsoleView/VariableView/StackView/BreakpointView/EvaluateConsole in DOM; code-view height == screen height - menu height
async def test_menu_labels():                  # menu bar shows exactly "File" and "Help"
async def test_starts_in_vim_normal_edit(tmp_path):   # config.json keybindings="emacs" under TDB_CONFIG_DIR → still vim; code_view.mode == Mode.EDIT
async def test_q_exits(tmp_path):              # ":q\r" → app.return_code == 0; no breakpoints.json written in TDB_CONFIG_DIR
async def test_q_dirty_dialog_then_cancel():   # "x", ":q\r", "escape" → still running
async def test_q_bang_exits_without_saving():
async def test_wq_saves_and_exits():
async def test_ctrl_q_dirty_dialog():          # ctrl+q with dirty buffer → modal
async def test_escape_does_not_exit():         # "escape" x3 → still running
async def test_new_file_quit_creates_nothing(tmp_path):
async def test_new_file_write_creates(tmp_path):      # "itext", escape, ":w\r" → file == "text\n"? (assert exact content the engine produces)
async def test_file_menu_switch_dirty_prompts(tmp_path):  # edit, open picker, pick other → modal; "d" → other file shown in editor
async def test_file_menu_rejects_binary(tmp_path):        # pick binary → notification, original buffer kept
```

- [ ] **Step 2: Run** `uv run pytest tests/unit/test_cli_vi.py tests/unit/test_vi_app.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** the Interfaces. Driving `_OpenFileModal` in tests: dismiss it programmatically (`app.screen.dismiss(path)`), as existing open-file tests do.
- [ ] **Step 4: Run** the two files plus `tests/unit/test_cli.py` — Expected: PASS. Then manually: `uv run tdb --vi /tmp/x.txt` in a real terminal; check paging, `:wq`, Alt+F, Alt+H.
- [ ] **Step 5: Commit** — `feat: add --vi standalone editor mode`

### Task 7: Full verification

- [ ] **Step 1:** Full suite with the memcap wrapper: `memcap-pytest` (or `uv run pytest` under it as in the memory note). Expected: no new failures vs. `main` (compare in a `main` worktree for any failure that looks pre-existing).
- [ ] **Step 2:** Container check: `docker run --rm -it -v $PWD:/src --read-only --tmpfs /tmp python:3.12-slim` style run: `pip install /src`, `HOME` read-only, `tdb --config-dir /tmp/tdb --vi /tmp/a.txt` edits and saves; `tdb --config-dir /tmp/tdb --info` shows `/tmp/tdb`.
- [ ] **Step 3:** Commit any fixes — `fix: ...`

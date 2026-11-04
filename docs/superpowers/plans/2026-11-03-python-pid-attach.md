# Python PID Attach Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `tdb -a PID` attaches to a running, unprepared CPython 3.11+ program, including a hung or idle one.

**Architecture:** tdb spawns its own debugpy adapter and sends `attach` with `listen`; code injected into the target connects back to it. Injection tries `sys.remote_exec` on 3.14 targets for 5 s, then `python -m debugpy --pid`. An `AdapterSpec` hook runs injection from `remote_attach`, so the TUI, `--headless` and `--server` share one path.

**Tech Stack:** Python, debugpy >= 1.8.20, gdb (Linux fallback), pytest + pytest-asyncio (`asyncio_mode = "auto"`).

**Spec:** `docs/superpowers/specs/2026-11-03-python-pid-attach-design.md`

## Global Constraints

- Target Python floor: CPython 3.11. No `debugpy` required in the target's environment.
- Timeouts: `remote_exec` wait 5 s; total injection 30 s; blocked-target notice after 3 s.
- Injection failures exit with code 2 and a message on stderr. No sudo handling, no system changes.
- Never use lldb on Linux for injection: pass no prefer-lldb flag.
- Attach request: `listen` on 127.0.0.1, `redirectOutput: true`, `subProcess: false`, no `connect`.
- Cross-platform: never call `os.kill(pid, 0)` on Windows (it terminates the process). Use `sys.platform` checks, not POSIX assumptions.
- Work on branch `python-pid-attach`, created from `main` in the `work` repo. Commit the spec and this plan first.
- Run single test files with `uv run pytest <path> --no-cov -v`. Run the full suite through the `memcap-pytest` wrapper when it is on PATH, otherwise `uv run pytest`.

## Review Focus

1. Target exits while injection is running: expect a prompt, clear failure, not a 30 s wait or a hang. (Task 2)
2. Quit or Ctrl-C during injection: helper processes are killed, so gdb cannot leave the target stopped. (Task 2)
3. `/proc/PID/exe` is a non-Python binary (uWSGI-style embedding) or `python3.12 (deleted)` after an upgrade: never execute it; fall back to debugpy injection. (Task 1)
4. tdb installed under a path with spaces, quotes or backslashes: the generated `remote_exec` script stays valid. (Task 2)
5. Target already has debugpy active, or a second tdb attaches: fails clearly or works within 30 s, and the target survives. (Task 4)

---

### Task 1: Target probing and language detection

**Files:**
- Create: `src/tdb/languages/python_attach.py`
- Modify: `src/tdb/languages/registry.py` (`detect_executable`)
- Test: `tests/unit/test_python_attach_probe.py`

**Interfaces:**
- Produces, in `python_attach.py` (no controller or UI imports):
  - `class PidAttachError(Exception)`: its `str()` is the user-facing message.
  - `@dataclass(frozen=True) class TargetInfo: pid: int; interpreter: str | None; has_remote_exec: bool`
  - `is_python_executable(path: str) -> bool`
  - `pid_exists(pid: int) -> bool`
  - `probe_target(pid: int, python: str | None = None) -> TargetInfo`, raises `LanguageNotSupportedError`

- [ ] **Step 1: Create the branch and commit the spec and plan**

```bash
git checkout -b python-pid-attach
git add docs/superpowers/specs/2026-11-03-python-pid-attach-design.md docs/superpowers/plans/2026-11-03-python-pid-attach.md
git commit -m "docs: design and plan for Python PID attach"
```

- [ ] **Step 2: Write the failing tests**

```python
@pytest.mark.parametrize("name", ["python", "python3", "python3.12", "python3.14t", "python.exe", "pypy3"])
def test_python_names_detected(name): assert is_python_executable(f"/usr/bin/{name}")

@pytest.mark.parametrize("name", ["uwsgi", "pythonista", "python3.12 (deleted)", "gdb"])
def test_other_names_rejected(name): assert not is_python_executable(f"/usr/bin/{name}")

def test_detect_executable_python_before_elf(tmp_path):
    exe = tmp_path / "python3.12"; exe.write_bytes(b"\x7fELF" + b"\0" * 60)
    assert registry.detect_executable(str(exe)) == "python"

def test_probe_reads_current_interpreter(monkeypatch):
    # sys.executable stands in for the target's interpreter
    monkeypatch.setattr("tdb.languages.python_attach._exe_of_pid", lambda pid: sys.executable)
    monkeypatch.setattr("tdb.languages.python_attach.pid_exists", lambda pid: True)
    t = probe_target(4242)
    assert t.interpreter == sys.executable
    assert t.has_remote_exec == hasattr(sys, "remote_exec")

def test_probe_never_runs_non_python_exe(monkeypatch):
    run = Mock(); monkeypatch.setattr("tdb.languages.python_attach.subprocess.run", run)
    monkeypatch.setattr("tdb.languages.python_attach._exe_of_pid", lambda pid: "/usr/bin/uwsgi")
    monkeypatch.setattr("tdb.languages.python_attach.pid_exists", lambda pid: True)
    assert probe_target(4242) == TargetInfo(4242, None, False)
    run.assert_not_called()

def test_probe_deleted_interpreter_is_unknown(monkeypatch):  # same asserts, exe "/usr/bin/python3.12 (deleted)"

def test_probe_explicit_python_wins(monkeypatch):            # python=sys.executable, _exe_of_pid not consulted

def test_probe_rejects_old_python(monkeypatch):
    # subprocess.run stub returns stdout "cpython 3 10 0"
    with pytest.raises(LanguageNotSupportedError, match="3.11"): probe_target(4242)

def test_probe_rejects_pypy(monkeypatch):
    # stub stdout "pypy 3 11 0"
    with pytest.raises(LanguageNotSupportedError, match="CPython"): probe_target(4242)

def test_probe_missing_pid(monkeypatch):
    monkeypatch.setattr("tdb.languages.python_attach.pid_exists", lambda pid: False)
    with pytest.raises(LanguageNotSupportedError, match="4242"): probe_target(4242)

def test_pid_exists_never_signals_on_windows(monkeypatch):
    monkeypatch.setattr("tdb.languages.python_attach.sys.platform", "win32")
    kill = Mock(); monkeypatch.setattr("tdb.languages.python_attach.os.kill", kill)
    assert pid_exists(4242) is True
    kill.assert_not_called()

def test_pid_exists_permission_error_means_alive(monkeypatch):  # os.kill raises PermissionError -> True
```

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest tests/unit/test_python_attach_probe.py --no-cov -v`
Expected: FAIL with ImportError on `tdb.languages.python_attach`.

- [ ] **Step 4: Implement the probe**

- `is_python_executable`: basename matches `^(python|pypy)(\d+(\.\d+)?t?)?(\.exe)?$`, case-insensitive. PyPy is detected so `probe_target` can reject it by name.
- `_exe_of_pid(pid) -> str | None`: on Linux `os.path.realpath(f"/proc/{pid}/exe")`, else `None`.
- `probe_target`: check `pid_exists` first. Use `python` if given, else `_exe_of_pid`. If that path is `None`, not `is_python_executable`, or not a file, return `TargetInfo(pid, None, False)` without running anything. Otherwise run `[path, "-I", "-c", ...]` with a 10 s timeout, printing `sys.implementation.name`, `version_info[0]`, `version_info[1]` and `hasattr(sys, "remote_exec")`. A probe that fails to run gives the same unknown result.
- `detect_executable`: add the `is_python_executable` check first, before the Go check (import lazily, as the function already does).

- [ ] **Step 5: Run to verify they pass**

Run: `uv run pytest tests/unit/test_python_attach_probe.py tests/unit/test_cli_native_attach.py --no-cov -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/tdb/languages/python_attach.py src/tdb/languages/registry.py tests/unit/test_python_attach_probe.py
git commit -m "feat(attach): probe target interpreter and detect Python by pid"
```

### Task 2: Injection engine

**Files:**
- Modify: `src/tdb/languages/python_attach.py`, `src/tdb/_timeouts.py`, `pyproject.toml`, `uv.lock`
- Test: `tests/unit/test_python_attach_inject.py`

**Interfaces:**
- Consumes: `TargetInfo`, `PidAttachError` (Task 1).
- Produces:
  - `_timeouts.py`: `PID_ATTACH_REMOTE_EXEC = 5.0`, `PID_ATTACH_TOTAL = 30.0`, `PID_ATTACH_BLOCKED_NOTICE = 3.0`
  - `build_remote_exec_script(server_dir: str, port: int, marker: str, claimed: str) -> str`
  - `classify_failure(*, inject_output: str, remote_exec_error: str, gdb_missing: bool, ptrace_scope: int | None, is_root: bool) -> str`
  - `read_ptrace_scope() -> int | None`
  - `async inject(target: TargetInfo, port: int, connected: asyncio.Event) -> str`: returns `"sys.remote_exec"` or `"debugpy injection"`; raises `PidAttachError`.

- [ ] **Step 1: Raise the debugpy floor**

Change `debugpy>=1.8.12` to `debugpy>=1.8.20` in `pyproject.toml` and run `uv lock`. `--disable-sys-remote-exec` is absent in 1.8.19 and present in 1.8.20, and older versions reject the unknown flag.

- [ ] **Step 2: Write the failing tests**

```python
def test_script_valid_with_awkward_paths():
    src = build_remote_exec_script(r"C:\it's a dir\server", 5678, r"C:\t\m", r'C:\t\"c')
    compile(src, "<payload>", "exec")

def test_script_exits_silently_when_marker_taken(tmp_path, capfd):
    # marker absent: exec() the script; attach_pid_injected must not be imported
    src = build_remote_exec_script("/nonexistent", 1, str(tmp_path/"m"), str(tmp_path/"c"))
    exec(compile(src, "p", "exec"), {})
    assert capfd.readouterr().err == ""

def test_script_removes_itself_and_dir_when_marker_taken(tmp_path):  # __file__ passed in globals; dir gone afterwards

def test_classify_ptrace_from_gdb():
    msg = classify_failure(inject_output="check the setting of /proc/sys/kernel/yama/ptrace_scope",
        remote_exec_error="", gdb_missing=False, ptrace_scope=1, is_root=False)
    assert "sysctl kernel.yama.ptrace_scope=0" in msg and "root" in msg

def test_classify_runtime_lookup_with_yama_is_permission():      # "PyRuntime address lookup failed", gdb_missing=True, scope 1 -> ptrace message
def test_classify_runtime_lookup_as_root_not_permission():       # is_root=True -> no "ptrace_scope" in msg
def test_classify_scope_3_mentions_reboot():
def test_classify_gdb_missing():                                 # "install gdb", says why it is needed
def test_classify_remote_debug_disabled_no_gdb():                # names PYTHON_DISABLE_REMOTE_DEBUG

async def test_inject_uses_remote_exec_when_it_connects(fake_helpers):
    # fake remote_exec helper exits 0 and sets `connected`
    assert await inject(TARGET_314, 5678, connected) == "sys.remote_exec"
    assert fake_helpers.debugpy_calls == []

async def test_inject_falls_back_when_blocked(fake_helpers, short_timeouts):
    # remote_exec exits 0, never connects; debugpy helper sets `connected`
    assert await inject(TARGET_314, 5678, connected) == "debugpy injection"
    assert "--disable-sys-remote-exec" in fake_helpers.debugpy_calls[0]
    assert fake_helpers.marker_claimed_by_tdb

async def test_inject_skips_remote_exec_below_314(fake_helpers):     # has_remote_exec False: no remote_exec call
async def test_inject_no_fallback_if_script_won_claim(fake_helpers, short_timeouts):
    # marker gone when tdb tries to claim: no debugpy helper, waits for connected
async def test_inject_fails_fast_on_nonzero_helper(fake_helpers):
    # debugpy helper exits 1 with ptrace text: PidAttachError well before PID_ATTACH_TOTAL
async def test_inject_total_timeout(fake_helpers, short_timeouts):   # nothing connects: PidAttachError mentioning the pid; helper killed
async def test_inject_target_exits_mid_injection(fake_helpers, short_timeouts):
    # pid_exists turns False while waiting: PidAttachError "exited", before the timeout
async def test_inject_cancel_kills_helper(fake_helpers):
    # cancel the inject task while the helper runs: helper was killed and reaped
async def test_inject_reaps_helper_after_connect(fake_helpers):      # helper still running when connected: no zombie, no lost task
```

`fake_helpers` replaces two private functions, `_spawn_remote_exec(target, script_path)` and `_spawn_debugpy(pid, port)`, each returning a process-like object. `short_timeouts` monkeypatches the `_timeouts` values to about 0.2 s and 1 s.

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest tests/unit/test_python_attach_inject.py --no-cov -v`
Expected: FAIL with ImportError on the new names.

- [ ] **Step 4: Implement the script, classifier and `inject`**

Script behaviour: `os.rename(marker, claimed)`. On `OSError`, remove its own file and its directory quietly and do nothing else. On success, delete the claimed file, script and directory; put `server_dir` on `sys.path`; import `attach_pid_injected`; remove the entry; call `attach({"mode": "connect", "address": ["127.0.0.1", port], "wait_for_client": False, "log_to": None, "adapter_access_token": None})`. Embed values with `repr()`.

`inject` order:

```
deadline = now + PID_ATTACH_TOTAL
if target.has_remote_exec:
    write script + marker into tempfile.mkdtemp()
    run target.interpreter -I -c "import sys; sys.remote_exec(pid, path)"
    if exit 0: wait connected up to PID_ATTACH_REMOTE_EXEC -> return "sys.remote_exec"
    if tdb's rename of marker fails: script owns it; wait connected to deadline
    else if exit != 0: keep stderr as remote_exec_error, delete temp dir
    (exit 0 + claimed by tdb: leave the script in place, it is still queued)
if linux and shutil.which("gdb") is None: raise PidAttachError(classify_failure(...))
spawn sys.executable -m debugpy --connect 127.0.0.1:PORT --disable-sys-remote-exec --pid PID
wait for: connected | helper exit != 0 | not pid_exists | deadline
```

Any wait loop polls `pid_exists` about every 0.25 s. Hold a strong reference to the helper-reaping task. In `finally`, kill any helper still running on failure or cancellation. After success, let the helper finish: it exits when the handshake completes. `gdb_missing` is only checked on Linux.

Message requirements are in the spec's Failure messages table. Name `sudo sysctl kernel.yama.ptrace_scope=0` and "run tdb as root"; for scope 3 say only a reboot lowers it.

- [ ] **Step 5: Run to verify they pass**

Run: `uv run pytest tests/unit/test_python_attach_inject.py tests/unit/test_python_attach_probe.py --no-cov -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/tdb/languages/python_attach.py src/tdb/_timeouts.py pyproject.toml uv.lock tests/unit/test_python_attach_inject.py
git commit -m "feat(attach): inject into Python targets with remote_exec and gdb fallback"
```

### Task 3: Wire it into the profile, CLI, controller and front ends

**Files:**
- Modify: `src/tdb/languages/base.py`, `src/tdb/languages/python.py`, `src/tdb/cli.py`, `src/tdb/session/controller.py`, `src/tdb/app.py`, `src/tdb/server/runner.py`
- Test: `tests/unit/test_cli_python_attach.py`, `tests/unit/test_controller_python_pid_attach.py`

**Interfaces:**
- Consumes: `probe_target`, `inject`, `PidAttachError`, `TargetInfo`, `PID_ATTACH_BLOCKED_NOTICE`.
- Produces:
  - `AdapterSpec.after_attach_request(self, initialized: asyncio.Event) -> Awaitable[str | None]`: async, default returns `None`.
  - `PidAttachDebugpyAdapter(DebugpyAdapter)` in `python.py`, built as `PidAttachDebugpyAdapter(target: TargetInfo, listen_port: int)`.
  - `build_python_profile(..., attach_pid: int | None = None, python: str | None = None)`.
  - `DebugController.attach_method: str | None`, set by `remote_attach`.

- [ ] **Step 1: Write the failing tests**

```python
# test_cli_python_attach.py, with probe_target stubbed to return TargetInfo(4242, "/usr/bin/python3.14", True)
def test_attach_python_by_lang(stub_probe):
    args = parse_args(["--lang", "python", "-a", "4242"])
    assert args.profile.id == "python"
    assert isinstance(args.profile.adapter, PidAttachDebugpyAdapter)
    assert args.program is None

def test_attach_python_detected_from_proc(fake_proc, stub_probe):  # exe named python3.14, no --lang
def test_attach_python_rejects_program(stub_probe, tmp_path):      # SystemExit, "cannot be combined"
def test_attach_python_forwards_python_flag(stub_probe):           # --python P reaches probe_target(4242, P)
def test_probe_error_becomes_parser_error(capsys):                 # LanguageNotSupportedError -> SystemExit(2)
def test_remote_attach_keeps_plain_adapter():                      # -r 5678: type(adapter) is DebugpyAdapter

def test_attach_body_shape():
    body = PidAttachDebugpyAdapter(TARGET, 6000).attach_body(host="127.0.0.1", port=0, opts={})
    assert body["listen"] == {"host": "127.0.0.1", "port": 6000}
    assert body["redirectOutput"] is True and body["subProcess"] is False
    assert "connect" not in body

def test_quirks():
    q = PidAttachDebugpyAdapter(TARGET, 6000).quirks
    assert q.attach_via_adapter and q.pre_arm_pause_on_attach and q.stdin_route is None

# test_controller_python_pid_attach.py, reusing the stub client style of test_controller_pid_attach.py
async def test_hook_runs_after_attach_request_sent():              # order: attach written, then after_attach_request
async def test_attach_method_recorded():                           # hook returns "sys.remote_exec"
async def test_initialized_event_sets_hook_event():                # _on_initialized sets the Event passed to the hook
async def test_hook_error_propagates():                            # PidAttachError leaves remote_attach
async def test_default_hook_does_nothing_for_other_adapters():     # attach_method is None
```

Add an app-level test in the style of existing `run_test()` tests:
- Injection failure sets `_startup_error` to `tdb: <message>` and exits with code 2.
- With a stopped event, no blocked notice appears; while still running after the delay, a notice containing "pause" appears.

Add a runner test: `PidAttachError` prints `tdb: <message>` to stderr and exits 2.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_cli_python_attach.py tests/unit/test_controller_python_pid_attach.py --no-cov -v`
Expected: FAIL with ImportError on `PidAttachDebugpyAdapter`.

- [ ] **Step 3: Implement the adapter and profile**

`PidAttachDebugpyAdapter`: quirks `AdapterQuirks(pre_arm_pause_on_attach=True, attach_via_adapter=True, stdin_route=None)`. `attach_body` drops `connect` and uses the values in Global Constraints. `after_attach_request` awaits `inject(self.target, self.listen_port, initialized)`.

`build_python_profile`: with `attach_pid`, call `probe_target(attach_pid, python)`, choose a free port with a new `free_port() -> int` in `python_attach.py` (bind port 0 on 127.0.0.1), and use the new adapter.

- [ ] **Step 4: Implement the CLI**

- Add `"python"` to `PID_ATTACH_LANGUAGES`.
- In `_resolve_language`, add a branch beside Go's: `lang_id == "python" and args.attach_pid is not None` calls `build_python_profile(adapter=adapter, adapter_paths=config.adapters, program=None, attach_pid=args.attach_pid, python=args.python)`.
- Extend the Go-only positional PROGRAM check to `("go", "python")`.
- Update the `-a` help text, the `_detect_lang` non-Linux message and the "applies to" error to include Python.

- [ ] **Step 5: Implement the controller and front ends**

- `controller.py`: create `self._initialized_seen = asyncio.Event()` and `self.attach_method = None` in `__init__`; set the event first in `_on_initialized`. At the end of `remote_attach`, after `_launch_sent.set()`, do `self.attach_method = await self.profile.adapter.after_attach_request(self._initialized_seen)`.
- `app.py` `_start_session`: catch `PidAttachError` beside `OSError`, set `_startup_error = f"tdb: {exc}"`, exit with 2. After a successful attach with `attach_method` set, `notify` the method. If `pause_on_attach` is on, `set_timer(PID_ATTACH_BLOCKED_NOTICE, ...)`, and if the phase is still RUNNING, notify that the program is not running Python code (likely blocked in a system call), pause cannot land yet, and breakpoints fire when it next runs Python.
- `runner.py`: catch `PidAttachError`, print `tdb: {exc}` to stderr, `sys.exit(2)`; print the method on success.

- [ ] **Step 6: Run to verify they pass**

Run: `uv run pytest tests/unit/test_cli_python_attach.py tests/unit/test_controller_python_pid_attach.py tests/unit/test_cli_native_attach.py tests/unit/test_controller_pid_attach.py tests/unit/test_remote_attach_via_adapter.py --no-cov -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/tdb tests/unit
git commit -m "feat(attach): support tdb -a PID for Python programs"
```

### Task 4: Real-process integration tests

**Files:**
- Create: `tests/integration/test_python_pid_attach.py`, `tests/integration/fixtures/python_attach_target.py`

**Interfaces:**
- Consumes: `tdb -a PID --headless` and the TUI through a pty. Find the JSON-RPC calls used by existing headless tests under `tests/integration` and reuse them.

The target script takes a mode (`busy`, `join`, `sleep`, `stdin`, `debugpy_listen`) and a heartbeat path, and on Linux calls `prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY)` through ctypes so tests pass under `ptrace_scope=1`. A fixture starts it and kills it in teardown, with no exceptions.

- [ ] **Step 1: Write the tests**

Parametrize over the Python versions available from `uv python find`, skipping missing ones. Skip on macOS (root required) and when gdb is absent on Linux. Do not skip Windows for `test_busy`, so Windows CI exercises DLL injection.

```python
def test_busy(target):            # breakpoint hits in work(); stack shows work and loop; quit; heartbeat advances
def test_running_frame_breakpoint(target):
def test_main_in_join_worker_breakpoint(target):
def test_blocked_main_attaches_and_quits(target):    # status running, target alive afterwards
@pytest.mark.skipif(no_py314)
def test_blocked_314_falls_back_to_debugpy(target):  # method is "debugpy injection", attach under about 10 s
def test_attach_twice(target):
def test_no_port_or_child_left_after_quit(target):   # Linux: no listening socket for the pid, no child processes
def test_output_reaches_console(target):             # get_output contains the target's print
def test_no_pause_on_attach_leaves_program_running(target):
@pytest.mark.skipif(no_py314)
def test_queued_script_stays_silent_after_fallback(target):
    # attach to blocked target, quit, wake the main thread (stdin mode: send a line)
    # target stays alive, stderr empty, temp directory removed
def test_target_already_using_debugpy(target):
    # debugpy_listen mode on tdb's interpreter: attach succeeds, or exits 2, within 35 s; target alive
def test_ptrace_denied_message():
    # Linux, ptrace_scope>=1, not root, target without the opt-in: exit 2, stderr names ptrace_scope
def test_failed_attach_leaves_no_adapter():          # after the failure, no debugpy.adapter child of tdb remains
def test_tui_end_to_end(target):                     # pty: source visible, Paused, q,q, tdb exits, heartbeat advances
```

- [ ] **Step 2: Run them**

Run: `uv run pytest tests/integration/test_python_pid_attach.py --no-cov -v`
Expected: all PASS, with skips only for missing interpreters or tools. Fix any failure in the code from Tasks 1-3, not by loosening the test. This is the first run of connect-back through tdb itself, so pre-armed pause and teardown are the likeliest trouble.

- [ ] **Step 3: Check for leaks**

Run: `pgrep -fa 'python_attach_target|debugpy.adapter'`
Expected: no output.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_python_pid_attach.py tests/integration/fixtures/python_attach_target.py
git commit -m "test(attach): real-process coverage for Python pid attach"
```

### Task 5: Documentation and full verification

**Files:**
- Modify: `README.md`, `docs/quickstart.md`, `docs/index.md`

- [ ] **Step 1: Update the docs**

Add `tdb -a PID` for Python to the feature lists and a quickstart example. Cover:
- Supported versions and that the target needs no setup.
- `ptrace_scope`, with the exact sysctl command.
- gdb is required on Linux for Python below 3.14 and for blocked programs.
- Blocked-target behaviour, and that quitting leaves the program running.

- [ ] **Step 2: Check `--doc` still builds**

Run: `uv run tdb --doc | grep -c "ptrace_scope"`
Expected: 1 or more.

- [ ] **Step 3: Run the full suite**

Run: `memcap-pytest` if available, otherwise `uv run pytest`
Expected: all pass. Compare any failure against a `main` worktree before calling it pre-existing.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/quickstart.md docs/index.md
git commit -m "docs: document Python attach by pid"
```

# Attach to a running Python program by PID

Date: 2026-11-03. Status: awaiting approval.

## Intent

`tdb -a PID` already attaches to running Go, C/C++, Rust and OCaml programs.
This adds Python: attach to a program that was started with no debugger and
no preparation, on any Python >= 3.11, including when it is hung or idle. A
hung program is often the reason to attach, so that case shapes the design.

`sys.remote_exec()` (PEP 768, Python 3.14) is the safest injection method but
is not enough alone: it runs only on the main thread at a safe point, so it
never fires while the main thread is blocked, and it still reports success.
gdb injection reaches those programs and covers Python 3.11-3.13.

## Goals

- `tdb -a PID` works on CPython 3.11+ with no change to the target and no
  `debugpy` installed in the target's environment.
- Language detection recognises a Python process on Linux without `--lang`.
- Hung and idle targets still attach, with a clear explanation of what
  can and cannot happen next.
- Quitting tdb leaves the program running, with no open port and no leftover
  child process. Attaching again later works.
- Every failure ends with a specific, actionable message and exit code 2.

## Non-goals

- Privilege handling. tdb does not run sudo or change system settings
  (decided by the user).
- Attaching to another machine, or a process owned by another user.
- MCP `debug_attach` by PID (*my call: an agent injecting into arbitrary
  processes deserves its own decision*).
- Following child processes of the attached program in v1 (*my call: untested;
  send `subProcess: false`*).
- Forwarding Console stdin to the target; it keeps its original stdin.
- A read-only stack view of a blocked main thread (a possible follow-up).

## Anti-goals

- A silent hang. tdb must never sit waiting on an injection that did not run.
- A stale queued `remote_exec` script doing anything visible after tdb moved on.
- Harming the target on quit: it must not be killed or left suspended.

## Design

### Connect-back: the target calls tdb

tdb spawns its own debugpy adapter (the same `python -m debugpy.adapter` as a
launch), then sends `attach` with `listen: {host: 127.0.0.1, port: P}`. The
adapter waits on P, and the injected code connects out to it. This replaces
`debugpy.listen()` in the target, which left an unauthenticated localhost port
open after detach and needed a saved PID-to-port record to reconnect.

Injection must run concurrently with the DAP handshake: it blocks until the
handshake finishes, so waiting for it first deadlocks.

### Sequence

1. Resolve the target interpreter and version (below).
2. Start the adapter; send `initialize`, then `attach` with `listen`.
3. Inject in the background:
   - Target >= 3.14 and remote debug available: call `sys.remote_exec` through
     the target's own interpreter, because caller and target versions must
     match. Wait up to `PID_ATTACH_REMOTE_EXEC_TIMEOUT` (5 s; slowest success
     seen was 3.9 s) for the target to connect.
   - Otherwise, or on timeout: run
     `python -m debugpy --connect 127.0.0.1:P --disable-sys-remote-exec --pid PID`
     under tdb's own interpreter. This uses gdb on Linux, lldb on macOS and DLL
     injection on Windows. On Linux, pass nothing that prefers lldb: it crashed
     the target in the one spike run.
4. The `initialized` event means injection worked; continue with the normal
   `do_configure` flow. If `PID_ATTACH_TOTAL_TIMEOUT` (30 s) passes first, stop
   the adapter and report the failure.

The fallback from `remote_exec` to gdb is automatic, with a one-line notice
that names the method used (*my call*).

### Injected payload

The `remote_exec` script reuses debugpy's own entry point, so behaviour matches
the gdb path: put `debugpy/server` on `sys.path`, import `attach_pid_injected`,
remove the path entry, and call `attach()` with `mode: "connect"`. The spike
confirmed this path, including a second attach.

A timed-out `remote_exec` stays queued and fires when the main thread wakes,
possibly long after. The script must therefore start by atomically claiming a
per-attempt marker file (rename). tdb claims the same marker before falling
back. Whoever renames it first owns the attempt; if the script loses, it exits
silently. The script and marker live in a private temp directory; the script
deletes them when it runs.

### Interpreter and language detection

- Linux: `/proc/PID/exe`. Add Python detection to
  `registry.detect_executable`, ahead of the ELF-means-C/C++ fallthrough: match
  the basename `python`, `python3`, `python3.N` (and `pypy`, rejected with a
  message). Embedded interpreters such as uWSGI need `--lang python`.
- Version: run the resolved interpreter with `-I -c` to print its version and
  whether `sys.remote_exec` exists. Refuse below 3.11 with a clear message.
  Only run a file whose name matches a Python interpreter: an embedding binary
  must never be executed. If the name doesn't match, or the file is gone, skip
  `remote_exec` and use debugpy's injection.
- tdb's `debugpy` floor rises to 1.8.20, the first checked release with
  `--disable-sys-remote-exec` (1.8.19 lacks it).
- macOS and Windows: `--lang python` is required, as for other languages. Use
  `--python PATH` to name the interpreter. Without it, skip `remote_exec` and
  go straight to debugpy's injection (*my call: no new dependency to find a
  process's executable*).
- A positional PROGRAM with `-a` on Python is rejected, as for Go.

### Code changes

- `cli.py`: add `python` to `PID_ATTACH_LANGUAGES`; update `-a` help and errors.
- `languages/python.py`: `build_python_profile` accepts `attach_pid`. With a
  PID, use a `DebugpyAdapter` variant with `attach_via_adapter=True` whose
  `attach_body` sends `listen`, `redirectOutput: true`, `subProcess: false` and
  no `connect`. Pick P with a free-port helper. The existing `-r` path is
  unchanged.
- New `languages/python_attach.py` (or similar): interpreter probing, the
  strategy order, payload, and failure classification. Keep it free of
  controller and UI imports so it is unit-testable.
- `session/controller.py`: `remote_attach` calls a new `AdapterSpec` hook right
  after the attach request is sent, which runs injection. The default hook does
  nothing, so other languages are unaffected.
- `_timeouts.py`: the two timeouts above.
- `app.py`: reuse the `_startup_error` exit path for injection failures; add
  the blocked-target notice. Headless and `--server` go through the same
  `remote_attach`, so they get this feature too.
- Already correct, confirm by test: `_is_remote_attach` disconnects without
  terminating, restart is disabled, and `--no-pause-on-attach` is honoured.

### Blocked targets

If no stop arrives about 3 s after configuration, show a notice in the TUI:
the program is not running Python code (likely blocked in a system call), so
pause cannot land yet; breakpoints will fire when it next runs Python. Status
stays Running. Do not treat this as an error. Worker threads running Python can
still be paused.

### Failure messages

Exit code 2, on stderr, using `_startup_error`:

| Situation | How to tell | Message content |
|---|---|---|
| No such process | PID check | PID not running |
| Target below 3.11, or PyPy | Version probe | Unsupported interpreter |
| ptrace denied | gdb output mentions ptrace/`ptrace_scope`; or `remote_exec` fails with "PyRuntime address lookup failed" while `ptrace_scope` >= 1 and tdb is not root | Explain; print `sudo sysctl kernel.yama.ptrace_scope=0` and "or run tdb as root"; scope 3 can only be lowered by a reboot |
| gdb missing | Not on PATH when gdb is needed | Install gdb, and say why it is needed (Python < 3.14, or main thread blocked) |
| Remote debug disabled and no gdb | "Remote debugging is not enabled" | Name `PYTHON_DISABLE_REMOTE_DEBUG` / `-X disable-remote-debug`, and gdb as the alternative |
| Timeout | No `initialized` | Injection did not complete; suggest possible causes |

"PyRuntime address lookup failed" is also the version-mismatch error, so it must
not be reported as a permissions problem without the `ptrace_scope` check.
tdb does not refuse up front on `ptrace_scope`: a target may have opted in
with `PR_SET_PTRACER`, so it tries first and classifies the failure.

## Evidence from the spike

Linux x86-64, debugpy 1.8.22, tdb on 3.12, targets with no debugpy installed.
Yama was satisfied by `PR_SET_PTRACER` in the target.

| Case (connect-back, attached twice unless noted) | Result |
|---|---|
| `remote_exec`, 3.14, busy | Breakpoint hit both times; no port or child left |
| gdb, 3.11 and 3.14, busy | Same |
| gdb, 3.11, main in `join()`, busy worker | Same, 2 threads seen |
| gdb, 3.11 and 3.14, main in `sleep` | Attaches, no leftovers; pause not tried on this path |
| `remote_exec`, 3.14, main blocked in `sleep`/stdin/`recv`/idle asyncio/`join()` | Never fires, reports success (listen mode) |

Also seen in listen mode: gdb on 3.14 attaches to all those blocked states; pause
does not land on a single blocked thread; breakpoints in running frames work on
3.11, 3.12, 3.14; `redirectOutput` delivers output events on 3.12.

Not tested: connect-back driven by tdb itself (the spike used a raw DAP client),
Windows, macOS, containers, ARM, free-threaded builds, a target that already
uses debugpy, a queued `remote_exec` firing after fallback.

## Testing

`uv run pytest` must pass.

- Unit: detection, version gating, strategy order (fake injectors), failure
  classification for each row above, `attach_body`, CLI argument errors.
- Integration, real processes: targets opt in with `PR_SET_PTRACER` as the spike
  did, so tests pass under `ptrace_scope=1`. Skip when gdb or Python 3.14 is
  absent. Cover: busy, `join()`, blocked main, attach twice, quit leaves target
  running with no listener, and the queued script firing after fallback stays
  silent. Every test kills its target in teardown.
- Target already using debugpy: must fail clearly or work, never hang.
- End to end under a pty: `tdb -a PID`, source visible, `q,q`, target alive.

## Docs

Update `README.md`, `docs/quickstart.md`, the `-a` help text and the feature list.
Include the `ptrace_scope` explanation and the blocked-target behaviour.

## Left to the builder

Exact wording of messages and private helper names.

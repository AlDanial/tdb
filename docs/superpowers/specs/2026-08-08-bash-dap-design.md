# Bash DAP Support — Design

**Date:** 2026-08-08
**Branch:** `bash-dap`
**Status:** Approved

## Goal

Debug bash scripts in tdb with the core local experience: launch, line
breakpoints (including conditions), step in/over/out, variables view,
evaluate console, and stack navigation.

## Scope

**In:** launch mode, breakpoints + conditions, stepping, variable
inspection (locals/globals, arrays, associative arrays), evaluate,
stack view, DAP pause, live breakpoint edits while running.

**Out (v1):** remote attach, error modal (`parse_error` stays `None`;
fatal errors appear in the console view), stepping into child bash
processes, stopping inside subshells/pipelines, bash-on-Windows
(native Windows gets a clear error suggesting WSL), stdin to the
debuggee (DEVNULL, same as Perl).

**Requirements settled during brainstorming:**
- Zero external dependencies — no bashdb, no Node. The adapter drives
  bash's own `DEBUG` trap / `extdebug` machinery, mirroring how the
  Perl adapter drives stock perl5db.
- Bash **4.4+** minimum. Older bash (notably stock macOS 3.2) gets a
  one-line error naming the requirement.
- Unix only (Linux + macOS with a modern bash).

## Architecture

Three new pieces, mirroring the Perl layout:

```
src/tdb/languages/bash.py       LanguageProfile for bash
src/tdb/adapters/bash/          bundled DAP adapter (python -m tdb.adapters.bash)
  __main__.py  server.py  session.py
  tdb_harness.sh                bash-side instrumentation, shipped as package data
```

Process chain:

```
tdb foo.sh
  └─ python -m tdb.adapters.bash          (DAP over stdio)
       └─ bash foo.sh                      spawned with:
            BASH_ENV=<path>/tdb_harness.sh
            pass_fds: cmd pipe (adapter→bash), resp pipe (bash→adapter)
            env: __TDB_CMD_FD, __TDB_RESP_FD
```

Bash sources `BASH_ENV` for every non-interactive shell, so the
harness runs before line 1 of the script. The harness immediately
**unsets `BASH_ENV`** so child bash processes run uninstrumented —
the recursive-subprocess guard (see CLAUDE.md pitfalls). Child
scripts therefore run freely; no stepping into them in v1.

### Registry / detection

- `_EXTENSION_MAP`: `.sh` and `.bash` → `bash`.
- Shebang sniffing extended: a shebang mentioning `bash` → `bash`.
- `build_bash_profile` registered under `bash`.
- Note: many `.sh` files are POSIX-sh or zsh. v1 treats them as bash;
  the harness's version check produces the actionable error if the
  interpreter turns out to be wrong for the file.
- Config override `{"adapters": {"bash": "/path/bash"}}` names the
  bash executable to spawn (same twist as the Perl profile, where the
  key names the interpreter, not the adapter).

### stdio

Debuggee stdout/stderr are PIPEd and pumped to DAP `output` events
exactly as `adapters/perl/session.py` does. stdin is DEVNULL.

## The harness (`tdb_harness.sh`)

Setup, in order: check `BASH_VERSINFO` ≥ (4,4) — on failure print one
clear stderr line and exit; `shopt -s extdebug`; `set -o functrace`;
install the `DEBUG` trap; `unset BASH_ENV`. Every identifier is
prefixed `__tdb_` to stay out of the debuggee's namespace.

**(revised during implementation):** `shopt -s extdebug` is armed
*inside the trap string itself*, not here at harness setup. Enabling it
directly from a `BASH_ENV` startup file makes bash try to load its
bashdb debugger profile ("cannot start debugger; debugging mode
disabled" on stderr), leaving extdebug OFF for the rest of the run.
`shopt -s extdebug` is idempotent, so re-running it on every trap firing
(after startup-file processing has finished) is harmless.

### Fast path (per-command cost)

The `DEBUG` trap fires before every simple command. It returns
immediately — with **zero IPC and zero forks** — unless one of:

1. Pending data on the command pipe, checked with
   `read -t 0 -u $__TDB_CMD_FD` (a builtin). If pending, drain and
   apply (breakpoint edits, `pause`).
   **(revised during implementation):** applying a queued breakpoint
   edit on this drain path still base64-decodes its path/condition
   fields (a `base64 -d` fork), same as the stopped loop — the "zero
   forks" guarantee only holds for the no-pending-data fast path itself,
   not for an edit actually being drained.
2. Stop-check matches:
   - Subshells never stop: `(( BASH_SUBSHELL > 0 ))` → return.
   - Step state: `step` always stops; `next` stops when
     `${#FUNCNAME[@]}` ≤ depth saved at resume; `finish` when <;
     `continue` only on breakpoints.
   - Breakpoint table: bash associative array keyed
     `canonical_path:line`. Each newly seen `$BASH_SOURCE` is
     canonicalized once via a `cd; pwd -P` subshell and cached in
     `__tdb_canon`. A breakpoint's condition string (if any) is
     `eval`'d at the trap; **exit status 0 means stop**.

The pending-data check is what makes live interaction work: the
adapter can push breakpoint edits or `pause` mid-run and the next
trap applies them — DAP pause without signals. (If the debuggee is
blocked in an external command, pause takes effect when it returns.)

### Stopped loop

On stop the harness writes `stopped|reason|file|line` to the response
pipe, then serves newline-delimited commands from the command pipe:

| command | behavior |
|---|---|
| `stack` | walk `FUNCNAME` / `BASH_SOURCE` / `BASH_LINENO`, top-level frame named `main` (bash's own convention) |
| `locals` | `local -p` — the trap runs inside the stopped function's scope |
| `globals` | `declare -p` filtered (exclude `__tdb_*` and bash-internal specials) |
| `eval <expr>` | eval in current scope; respond with captured stdout+stderr and exit status |
| `setbp` / `clearbp` / `clearall` | edit the breakpoint table |
| `step` / `next` / `finish` / `continue` | record step mode + `${#FUNCNAME[@]}` depth, exit the loop, return from the trap |

Multi-line payloads (declare -p output, eval results) are
base64-encoded on the wire — the one external command (`base64`,
coreutils/macOS-standard) the harness runs, and only while stopped.
Responses are framed as `ok <b64>` / `err <b64>` lines.

### Correctness constraints (regression-test these)

- With `extdebug` on, a `DEBUG` trap returning non-zero **skips the
  next command** (and status 2 forces a return). The trap must
  guarantee exit status 0 on every resume path. Failure mode if
  wrong: debugger randomly skips statements.
- Trap-body code runs in the debuggee shell, so it must survive
  `set -euo pipefail`: every fallible construct guarded. A
  `set -euo pipefail` fixture script must debug identically to the
  same script without it.
- The stopped loop's `locals` and `eval` handling must execute
  **inline in the trap body**, not inside a harness helper function:
  calling a function pushes a new scope, so `local -p` would report
  the helper's locals and `eval` would resolve `local` declarations
  against the wrong frame. Helper functions are fine for anything
  that doesn't read the debuggee's scope (framing, base64, stack
  walking).

## Python adapter (`tdb.adapters.bash`)

Same split as the Perl adapter:

- `__main__.py` — stdio DAP entry point.
- `server.py` — DAP request/event dispatch (initialize, launch,
  setBreakpoints, configurationDone, threads/stackTrace/scopes/
  variables, evaluate, continue/next/stepIn/stepOut, pause,
  disconnect/terminate).
- `session.py` — owns the bash subprocess and the two pipes:
  - Spawns bash with `start_new_session=True`; terminate/restart
    kills the process group (same as Perl).
  - Checks `shutil.which(bash)` before spawning; a missing bash fails
    the launch request with an install hint. A pre-connection child
    exit (e.g. the harness's version check) surfaces its stderr as
    the launch failure message.
  - Verifies `tdb_harness.sh` exists in the installed package before
    launch. Unlike the Perl compile shim there is no degraded mode:
    fail the launch loudly, naming the packaging problem.
    `tdb_harness.sh` is added to pyproject.toml package-data **in the
    same commit** that references it.
  - `stopOnEntry` = arm `step` mode before the script starts.
  - Translates DAP requests into harness commands; parses
    `declare -p` output into the DAP variable tree — scalars as
    leaves, indexed and associative arrays as expandable nodes.
  - EOF on the response pipe → `exited` (real exit code) +
    `terminated` events.
    **(revised during implementation):** exit detection is actually by
    process reap (`Process.wait()`/`returncode`), not response-pipe EOF —
    a backgrounded grandchild that inherited the response pipe's write
    end keeps it open long after bash itself has exited, which would
    hang EOF-based detection indefinitely. `_reap()` bounds how long it
    waits for the output pumps to flush before reporting `on_exit`.

### Frame semantics

Stack frames come from the harness `stack` command. **Locals are only
readable for the innermost frame** — bash has no API into outer
frames' locals. Frame 0: Locals + Globals scopes; outer frames:
Globals only. Documented limitation.

**(revised during implementation):** a third scope, Environment, was
added after this spec — see 2026-08-08-bash-env-scope-design.md. Frame
0 is Locals + Globals + Environment; outer frames are Globals +
Environment.

## Language profile (`languages/bash.py`)

```python
LanguageProfile(
    id="bash",
    display_name="Bash",
    adapter=BashAdapter(bash_executable=(adapter_paths or {}).get("bash")),
    presentation=Presentation(lexer="bash", frame_placeholder="main"),
    capabilities=ProfileCapabilities(),  # all gates off
)
```

`BashAdapter.command()` → `[sys.executable, "-m", "tdb.adapters.bash"]`.
No quirks: no pre-arm pause, no attach-via-adapter (attach is out of
scope). `launch_body` carries `type: "bash"`, program/args/cwd/env/
stopOnEntry, plus the optional bash-executable override.

## Known limitations (v1, documented)

- Debuggee code that installs its own `DEBUG` trap clobbers the
  harness; debugging silently degrades to free-running.
- No stopping inside subshells `(...)`, `$(...)`, or pipeline
  segments; they execute normally.
- Child bash processes run uninstrumented.
- Outer-frame locals not inspectable (innermost frame only).
- Pause is deferred while blocked in an external command.
- `.sh` files that aren't bash are only diagnosed at launch, by bash
  itself or the harness version check.

## Testing

Mirrors the Perl suite:

- **Integration** (`tests/integration/`, skipped when bash < 4.4 —
  e.g. macOS CI with stock 3.2):
  - `test_bash_adapter_launch.py` — launch, stopOnEntry, output
    events, exit code, missing-bash and old-bash failures.
  - `test_bash_adapter_breakpoints.py` — set/hit/clear, conditions
    (stop on exit-0), live edits while running, pause.
  - `test_bash_adapter_stepping.py` — step in/over/out across
    functions and `source`d files; the extdebug return-status
    regression (no skipped statements); depth tracking.
  - `test_bash_adapter_inspection.py` — locals vs globals, arrays,
    associative arrays, evaluate with side effects and non-zero exit.
  - Fixtures in `tests/integration/fixtures/`: loops, nested
    functions, a `source`d file, `set -euo pipefail`, a
    subshell/pipeline script (assert no stop inside), arrays +
    assoc arrays, a script that spawns a child bash script (assert
    child runs uninstrumented).
- **Unit** (`tests/unit/`): `declare -p` parser (scalars, quoting
  edge cases, indexed/assoc arrays, exported/readonly flags);
  registry detection for `.sh`/`.bash`/bash shebangs.

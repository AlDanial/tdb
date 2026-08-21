# Ruby Debugging Support (Core Backend) — Design

**Date:** 2026-08-21
**Status:** Approved design, pre-implementation
**Branch:** `add-ruby-support` (based on `main` @ 7acfe3e; the unmerged
Perl `--run` fix on `fix-perl-run-mode` is unrelated to this work)
**Goal:** Debug Ruby programs in tdb via the `debug` gem's `rdbg`, at the
same tier as Perl: launch, stepping, breakpoints with conditions, stack /
variables / evaluate console with tab completion, fatal-error modal, remote
attach, `--terminal`, `--run`, and `--record`/`--replay`.

## Scope and decomposition

The user-approved target is full Python parity, decomposed into three
sequential projects. **This spec covers only project 1.**

1. **Core backend (this spec)** — everything listed under Goal.
2. **Ruby-side hooks (follow-on)** — `binding.break`-style live hook and a
   post-mortem exception hook (the debug gem has native post-mortem
   support).
3. **Process/concurrency extras (follow-on)** — forked-child auto-attach
   (per-PID sockets via the debug gem's fork handling) and a threads/fibers
   inspection modal.

Also in scope, per user decision: **File → Open is ungated for all
languages** (same-language restriction), not just Ruby. See its section
below.

Out of scope for project 1: Bundler integration (`rdbg -c` / `bundle exec`
wrapping — plain `tdb script.rb` only; Bundler projects work only if the
user's environment resolves `rdbg`), `child_process_strategy`,
`task_inspection`, cookie-authenticated remote attach.

## Probe-verified facts (2026-08-21, ruby 3.3.8 / debug gem 1.11.1)

These were verified by driving `rdbg` directly with a scripted DAP client;
they are load-bearing for the design:

- `rdbg --open --sock-path <path>` speaks **DAP natively** over a UNIX
  socket. The debug gem auto-detects DAP from the `Content-Length:`
  greeting. No custom adapter (à la Perl/Bash) is needed.
- Advertised capabilities: `supportsConfigurationDoneRequest`,
  `supportsConditionalBreakpoints`, `supportsCompletionsRequest`,
  `supportsEvaluateForHovers`, `supportsFunctionBreakpoints`,
  `supportsExceptionFilterOptions` (filters `any`, `RuntimeError`, both
  condition-capable, none default), `supportsStepBack`,
  `supportsTerminateRequest`, `supportTerminateDebuggee`.
- The full standard sequence `initialize → launch → setBreakpoints →
  configurationDone → stopped(breakpoint) → continue → terminated` works
  as-is; `launch` succeeds over the socket (rdbg has already launched the
  debuggee; the request is effectively an acknowledgment).
- **Debuggee stdout/stderr is NOT forwarded as DAP `output` events.** It
  stays on the rdbg process's own stdout/stderr ("output to the STDOUT/ERR
  printed on the TERMINAL"). Something must pump the pipes.
- `--open=vscode` must never be used: it attempts to launch VS Code.
- AF_UNIX socket paths are limited to ~107 chars; long temp paths fail with
  `AF_UNIX path too long`. Socket paths must be short.
- `--nonstop` runs the script without stopping at the first line; without
  it, rdbg waits stopped at the beginning.
- rdbg's `DEBUGGER:` banner lines ("Debugger can attach via …", "wait for
  debugger connection…", "Connected.") are printed on stderr.

## Architecture

Approach chosen (option B of the brainstorm): a **thin bundled proxy
adapter** — a Python stdio↔socket DAP bridge — rather than teaching the
core client a socket-launch transport. Rationale: zero core changes (Ruby
looks exactly like every other language to the controller, so `--run`,
record/replay, and the modals ride existing paths), rdbg quirks stay
contained in one directory, and Windows support is a transport detail
inside the proxy.

New code:

- `src/tdb/languages/ruby.py` — declarative profile: `RdbgAdapter
  (AdapterSpec)` + `build_ruby_profile()`.
- `src/tdb/adapters/ruby/__main__.py` — stdio wiring, copied verbatim from
  the perl/bash shape.
- `src/tdb/adapters/ruby/server.py` — `RubyDapServer`, the bridge
  (~300 lines expected).

Local-launch process tree:

```
tdb (TUI, DAP client)
 └─ python -m tdb.adapters.ruby      (proxy: stdio DAP ⇄ socket DAP)
     └─ rdbg --open --sock-path <short path> [--nonstop] -- program.rb args
         (rdbg = debug gem's DAP server; debuggee runs inside it)
```

The proxy is a store-and-forward pipe with seq translation, not a
debugger. Remote attach bypasses it entirely (rdbg is already a DAP
server; tdb TCP-connects directly).

## Language profile, registry, CLI

`build_ruby_profile()` returns:

- `id="ruby"`, `display_name="Ruby"`.
- **Adapter** `RdbgAdapter`: `id="rdbg"`; `command()` returns
  `[sys.executable, "-m", "tdb.adapters.ruby"]`. The proxy is always
  present, so `AdapterNotFoundError` never fires at this layer; the real
  `rdbg` is resolved *inside the proxy* (configured path from `launch_body`
  opts, else `shutil.which("rdbg")`), and a missing/too-old rdbg fails the
  **launch response** with the hint `gem install debug` plus the
  `{"adapters": {"rdbg": "/path/to/rdbg"}}` config key (mirrors cpp's hint
  style).
- **Quirks:** `attach_via_adapter=False`, `pre_arm_pause_on_attach=False`.
- `launch_body()` / `attach_body()`: standard fields only (program, args,
  cwd, env, stopOnEntry, console; host/port for attach). The configured
  rdbg path travels in launch-body opts.
- **Presentation:** `lexer="ruby"`, `frame_placeholder="<main>"`,
  `parse_error=parse_ruby_error` (new, in `languages/errors.py`).
- **Capabilities:** `pause_while_running=True`, `compute_step_units=None`
  (line-based stepping; `-k` snapping passes through as for non-Python),
  `child_process_strategy=None`, `task_inspection=False` (Processes/Tasks
  modals show the existing "Not available for Ruby" message).

Registry (`src/tdb/languages/registry.py`):

- `_EXTENSION_MAP[".rb"] = "ruby"`.
- Shebang branch for `b"ruby"` in `detect()` (after python/perl; no
  substring-collision hazard).
- `register("ruby", build_ruby_profile)` at the bottom of the file.

CLI (`src/tdb/cli.py`):

- Add `"ruby"` to the remote-attach allowlist at `cli.py:423` — the only
  gate touched.
- `--lang ruby` / `--adapter rdbg` and the `adapters` /
  `default_adapters` config keys work automatically via the registry.

Deliberately unchanged: `?` doc-help in the evaluate console stays
Python-only; `--python` / `--pv` / `--no-subprocess` correctly reject
Ruby via the existing `profile.id != "python"` gates.

## Proxy internals (`RubyDapServer`)

Same `_on_<command>` introspection pattern as Perl/Bash, plus a **default
passthrough**: any request without a local handler is re-framed and
forwarded to rdbg verbatim; rdbg's responses/events flow back with
`request_seq`/`seq` remapped. Locally handled commands:

- **`initialize`** — respond from a static `CAPABILITIES` dict (rdbg is
  not running yet; same trick as the Perl server). The dict mirrors the
  probe-verified capability set above.
- **`launch`** — resolve rdbg; check `rdbg --version` (fail launch if debug
  gem < 1.9); build argv
  `[rdbg, "--open", "--sock-path", <path>, *(["--nonstop"] if not
  stop_on_entry else []), "--", program, *args]`; spawn in its own process
  group with the launch body's `cwd`/`env` and pipes on stdout/stderr;
  poll for the socket with a timeout (on timeout: failed launch response
  that includes rdbg's captured stderr — this is where "gem not found"
  errors surface); connect; run the proxy's own `initialize` + `launch`
  handshake against rdbg; then emit `initialized` to tdb. If rdbg's live
  capabilities diverge from the static dict, trust the static dict (tdb
  already consumed it) and log the diff.
- **`configurationDone`** — forward. If `stop_on_entry` and no `stopped`
  event arrives within a short window, send `pause` to rdbg to synthesize
  the entry stop (an implementation-time probe decides whether this
  workaround is actually needed; the debugpy attach quirk sets precedent).
- **`disconnect` / `terminate`** — forward, wait briefly for rdbg to exit,
  then SIGTERM → SIGKILL the process group. Always reply to tdb even if
  rdbg is already gone.

Cross-cutting behavior:

- **Output pump:** two reader threads on rdbg's stdout/stderr pipes → DAP
  `output` events (`stdout` / `stderr` categories). rdbg's `DEBUGGER:`
  banner lines on stderr are filtered (adapter noise, not program
  output). EOF on both pipes + process exit → synthesize `exited` (real
  exit code) and `terminated` events if the socket didn't already deliver
  them.
- **Seq remapping:** two counters (to-client, to-rdbg) plus an in-flight
  map of forwarded request seqs so responses carry the client's original
  seq. Reverse requests from rdbg get mirror treatment.
- **Windows transport:** no `--sock-path`; use
  `--port <free port> --host 127.0.0.1`, free port from bind-to-0 with a
  small retry loop for the race, plus `--cookie <random>` if the cookie
  handshake proves usable over DAP (see Risks — 127.0.0.1 binding is the
  actual security boundary either way); process-group handling
  uses the established `CREATE_NEW_PROCESS_GROUP` pattern (commit
  974961b).
- **Unix socket path:** short-named temp dir (e.g.
  `mkdtemp(prefix="tdb-rdbg-")`); if the resulting path approaches the
  AF_UNIX limit, fall back to the TCP transport.
- **Failure containment:** socket death mid-session → `terminated` to tdb
  and nonzero exit, never a hang; every local error path produces a
  well-formed DAP error response ("structured errors, never a crash" —
  multi-language design doc rule).

## Modes

### Remote attach

`tdb -r host:port --lang ruby` TCP-connects **directly** to a
user-started rdbg (no proxy):

- Remote side: `rdbg --open --port 5678 --host 0.0.0.0 script.rb` (waits
  stopped for the client) or with `--nonstop` (runs immediately; tdb
  attaches later and pauses on demand). Both are supported.
- tdb sends `initialize` / `attach` / `configurationDone`, same shape as
  the debugpy path. `--lang ruby` is required (attach has no file to
  detect from; same as Perl).
- Program output stays on the remote terminal (Python remote-attach
  parity). Restart stays disabled in remote-attach mode (existing
  behavior).
- **Documented limitation:** rdbg's `--cookie` auth lives in its own
  protocol greeting, not DAP, so cookied endpoints are unsupported. Docs
  recommend localhost/SSH tunnels (as the debug gem's docs do).

### `--terminal`

When the launch body carries `console="externalTerminal"`, the proxy does
not spawn rdbg itself: it sends a `runInTerminal` reverse request to tdb
with the full rdbg argv (including `--open --sock-path …`), cwd, and env.
tdb spawns the terminal via the existing `_TERMINAL_SPECS` table
(`session/terminal.py:40`); rdbg and the debuggee live in that terminal
(interactive Ruby programs get a real tty/stdin); the proxy
polls-and-connects to the socket exactly as in the normal path. The
output pump is absent in this mode — output belongs to the terminal,
matching the other languages. Windows uses the TCP variant.

### `--run`

Gated on `pause_while_running=True`, which Ruby sets. Run mode launches
with `stop_on_entry=False` → proxy passes `--nonstop` → program runs
immediately. Breakpoints armed before `configurationDone`, `pause` while
running, and asynchronous stop events are plain DAP passing through the
proxy untouched; rdbg handles `pause` mid-execution including inside
blocking calls. Integration coverage mirrors the Perl case in
`test_run_mode.py`.

### `--record` / `--replay`

Language-agnostic by design: the recorder stamps `"language": "ruby"` in
the session header and replay rebuilds the profile via the registry.
Implementation checks: add `"ruby"` to the `_LAUNCH_REQUIRED` /
`_ATTACH_REQUIRED` tables at `replay.py:27-28` if those are per-language
lists, and prove the round-trip with `test_replay_ruby.py` (modeled on
`test_replay_perl.py`).

## File → Open ungating (all languages)

User-approved targeted improvement. File → Open ("pick a different
program, restart the session on it") is currently Python-only via two
gates: menu-label hiding at `app.py:308` and the keybinding no-op guard
at `app.py:1389`.

- Remove both gates; the item appears for every language.
- Add a **same-language check** in `action_open_file`'s dismiss path: the
  picked file must resolve via `registry.detect()` to the running
  session's `profile.id`; otherwise a warning toast
  ("`<file>` is not a <Language> program") and no restart.
- `detect()` failures (unknown extension, unreadable file) get the same
  warning path, never a crash.
- Both entry paths (menu click and Alt+F keybinding) converge on
  `action_open_file`, so the fix covers both; per the project rule, audit
  confirms no other entry path exists.
- Cross-language switching (restart re-resolving the profile) is
  explicitly out of scope.

## Fatal-error modal

New `parse_ruby_error()` in `languages/errors.py`, wired as the profile's
`parse_error`. Handles:

- Classic bottom-up traceback:
  `script.rb:3:in '<main>': message (RuntimeError)` followed by
  `\tfrom …` frames (both `` `old` `` and `'new'` method-name quoting,
  which changed in Ruby 3.4).
- Ruby 3.x top-down variant.
- `SyntaxError`'s distinct `file:line: message` shape.

Extracts file, line, exception class, and message so the modal can jump
the Code View to the failing line. Unparseable stderr falls back to the
raw-text modal (existing `parse_error`-miss behavior).

## Version floors

- `rdbg` on PATH or configured via `adapters.rdbg`.
- debug gem ≥ 1.9 (proxy checks `rdbg --version` at launch and fails the
  launch response with a clear message otherwise).
- Ruby ≥ 3.1 (first release bundling the debug gem).
- Tested against ruby 3.3.8 / debug 1.11.1.

## Testing

Unit (`tests/unit/`):

- `test_ruby_profile.py` (modeled on `test_perl_profile.py`): adapter
  argv, launch/attach bodies, capability flags, hint text.
- `test_registry_ruby.py`: `.rb` extension, `ruby` shebang, `--lang ruby`.
- `test_profile_contract.py` covers Ruby automatically (parametrized over
  `registry.known_languages()`).
- Proxy-local tests: seq remapping, `DEBUGGER:` banner filter (pure
  functions, no Ruby required).
- `parse_ruby_error` cases in `test_error_parsers.py`: classic traceback,
  nested-frame backtrace, top-down variant, `SyntaxError`, both quoting
  styles, garbage input → `None`.
- File→Open same-language check: accept `.rb` in a Ruby session, reject
  `.py` in a Ruby session with warning, detect-failure path.

Integration (`tests/integration/`), all behind a `shutil.which("rdbg")`
skip plus a `ruby_ok()` helper (debug gem ≥ 1.9):

- Reuse the scripted-DAP-client harness:
  `AdapterClient.start(module="tdb.adapters.ruby")`.
- `test_ruby_adapter_launch.py`: launch, stop-on-entry, nonstop, exit
  codes, output events from the pump.
- `test_ruby_adapter_breakpoints.py`: set/clear/conditional breakpoints.
- `test_ruby_adapter_inspection.py`: stack, scopes, variables, evaluate,
  completions.
- `test_ruby_session.py`: through the real controller.
- `test_ruby_terminal.py`: runInTerminal handshake.
- Ruby cases in `test_run_mode.py` and the remote-attach suite.
- `test_replay_ruby.py`: record/replay round-trip.
- One File→Open integration check: a Perl session reopens another Perl
  script cleanly (proves the ungating for existing languages).
- Fixtures: small `.rb` programs in `tests/integration/fixtures/`.

CI: Dockerfile gains Ruby. The debug gem has a C extension, so on Alpine
either `apk add ruby ruby-dev make gcc musl-dev` + `gem install debug`,
or Alpine's packaged bundled gems if they cover debug ≥ 1.9 — resolved at
implementation time. Tests skip gracefully where rdbg is absent.

Packaging: the proxy is pure Python — no `[tool.setuptools.package-data]`
entries needed (unlike perl/bash's non-Python assets).

## Documentation

- README language table: Ruby row (supported modes, `gem install debug`
  hint).
- Per-language mode docs (`--terminal`, `--run`, remote attach,
  record/replay): Ruby column/row each, including the remote-attach
  cookie limitation.
- New "Debugging Ruby" section: launch, remote attach
  (`rdbg --open --port …`), version floors, and the Bundler non-scope.
- File→Open docs updated: available for all languages, same-language
  restriction stated.

## Risks / open implementation questions

- **Entry-stop synthesis:** whether rdbg emits `stopped` unprompted after
  `configurationDone` when waiting at entry — probe at implementation
  time; the `pause` fallback in the proxy covers the negative case.
- **Alpine debug gem build:** C-extension build on musl is expected to
  work with standard build deps but is unverified until the Dockerfile
  task.
- **Windows TCP transport:** free-port race handled by retry; cookie use
  on localhost is proxy-internal (proxy speaks DAP to rdbg, so if the
  cookie greeting turns out to be rdbg-protocol-only, drop it and rely on
  127.0.0.1 binding — decided by an implementation-time probe).
- **`supportsStepBack`:** rdbg advertises it; tdb has no step-back UI.
  Ignored in project 1; noted as a possible future feature.

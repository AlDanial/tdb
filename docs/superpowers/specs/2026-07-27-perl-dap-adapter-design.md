# Perl DAP Adapter — Design

**Date:** 2026-07-27
**Status:** Approved (brainstorm with Al, 2026-07-27)

## Goal

Add Perl debugging to tdb by writing a DAP adapter that drives Perl's
stock debugger (`perl -d` / perl5db.pl) for execution control and
injects helper subs into the debugged program for all data extraction.
Helpers return structured JSON to the adapter, which translates it to
DAP for tdb.

Remote attach is a first-class requirement with debugpy-equivalent
look-and-feel: the debuggee calls `listen($port)` then
`wait_for_client()` and tdb connects to it.

## Decisions (from brainstorm)

- **Adapter form:** Python, bundled inside tdb (`python -m
  tdb.adapters.perl`), same process model as debugpy. Not a
  standalone Perl program.
- **Control channel:** stock perl5db over a TCP socket (its
  RemotePort mechanism / TTY-handle redirection), never a PTY, never
  a custom Devel:: debugger core.
- **v1 scope:** core debugging (launch, breakpoints with conditions,
  stepping, continue/pause, stack, variables with lazy expansion,
  evaluate console, headless/RPC/MCP) **plus** rich data dumping,
  a Specials scope, and remote attach. Explicitly out: watch
  expressions, fork following, exception breakpoints,
  statement-granularity stepping, `--terminal`.
- **Platform:** Unix first; nothing in the design precludes Windows
  (TCP loopback + file-based injection work on Strawberry Perl), but
  v1 tests and claims cover Linux/macOS only.
- **Perl floor:** 5.18+ (JSON::PP core since 5.14; the B PADLIST API
  stabilized in 5.18).

## Architecture

### Components

```
src/tdb/adapters/perl/
    __main__.py    # python -m tdb.adapters.perl → stdio DAP server
    server.py      # DAP endpoint (requests in, responses/events out)
    session.py     # perl5db driver: socket, child process, prompt
                   # state machine, one-command-at-a-time queue
    helpers.pl     # injected subs, package Devel::TdbHelper (pkg data)
    Devel/TdbRemote.pm  # debuggee-side remote-attach library (pkg data)
src/tdb/languages/perl.py   # LanguageProfile (mirrors cpp.py)
src/tdb/dap/wire.py         # NEW: DAP framing extracted from
                            # dap/client.py, shared by client & adapter
                            # (refactor, no behavior change)
```

### Topology — launch

```
tdb ──DAP over stdio──> adapter (python -m tdb.adapters.perl)
                          ├── TCP listener on 127.0.0.1:<ephemeral>
                          │     ▲ perl5db command channel (text)
                          └── spawns: perl -d prog.pl args
                              env PERLDB_OPTS="RemotePort=127.0.0.1:<port>"
                              stdin/stdout/stderr on pipes → DAP output events
```

Debugger conversation and program I/O never share a channel. Program
stdout/stderr are forwarded as DAP `output` events (categories
stdout/stderr). stdin is not connected (matches Python
internalConsole behavior).

Handshake mirrors debugpy: accept perl5db connection → wait for first
`DB<1>` prompt (perl -d stops at the first executable line) → inject
helpers via `do '<path>/helpers.pl'` → emit `initialized` → receive
setBreakpoints / configurationDone → hold the launch response until
after configurationDone (DAP-conformant). `stopOnEntry=false` → issue
`c` after configurationDone; else report the entry stop.

### Topology — attach (remote)

Debuggee opts in via the shipped library, debugpy-style:

```perl
use Devel::TdbRemote;                # first line of the program
...
Devel::TdbRemote::listen(5678);      # open listening socket; returns
Devel::TdbRemote::wait_for_client(); # block until tdb connects; stop here
```

tdb side: `tdb --lang perl -r host:5678` → DAP `attach {host, port}` →
the adapter **connects out** to the debuggee (same direction as
debugpy).

Mechanism: `Devel::TdbRemote` loads perl5db in NonStop mode (debugger
armed, program runs at full speed). `wait_for_client()` accepts the
connection, installs the accepted socket as perl5db's I/O handles
(perl5db's "TTY" is a filehandle pair; this is its own RemotePort
mechanism pointed the other way), loads `helpers.pl` from its own
directory, and sets `$DB::single = 1` — stopping on the line after the
call.

Arming caveat (documented plainly): Perl compiles debug hooks at
compile time. `use Devel::TdbRemote;` as the FIRST line makes the rest
of the program and everything it loads debuggable; code compiled
before the `use` is invisible to the debugger. Bulletproof
alternatives: `perl -d:TdbRemote prog.pl` or `PERL5OPT=-d:TdbRemote`.
There is no attaching to a completely unprepared process (true of
debugpy as well).

A remote box needs only the two Perl files (`Devel/TdbRemote.pm` +
`helpers.pl`, which always travel together) on `PERL5LIB` — tdb itself
is not required there.

## Driving perl5db

- **One command in flight.** perl5db is a strict command/response
  REPL. The driver holds an asyncio queue; the response terminator is
  the prompt regex `^\s*DB<+\d+>+ $`. This is the parser's ONLY
  regex.
- **Command vocabulary** (complete): `b <line> [cond]`,
  `b <file>:<line> [cond]`, `B <line>`, `n`, `s`, `r`, `c`,
  `do 'helpers.pl'`, and helper-call evals. Output of human-oriented
  commands (`T`, `V`, `y`, `.`, ...) is never parsed — location
  always comes from the `location()` helper.
- **Stops.** After `c`/`n`/`s`/`r` the driver is in "running" state
  and ignores socket bytes until a prompt reappears. New prompt while
  running = stopped → call `location()` helper (never parse the
  banner) → emit DAP `stopped` (reason: breakpoint if location
  matches a known breakpoint, else step).
- **Breakpoints.** DAP setBreakpoints replaces per-file; driver keeps
  a per-file registry, diffs, issues individual `B`/`b`. On
  "not breakable" rejection, retry at the next breakable line from
  the `breakable(file)` helper (Perl's own `%{"_<$file"}` table) and
  report the moved line in the DAP response — tdb's existing
  unbound-breakpoint warning and gutter placement work unchanged.
  Conditions pass through as Perl expressions.
- **Pause.** Launch mode: SIGINT to the owned child pid; perl5db
  traps it and stops. Attach mode: an out-of-band one-byte "pause" on
  a dedicated control connection that TdbRemote polls via a
  $SIG{ALRM}-driven check setting `$DB::single = 1`. The
  end-to-end attach test is the decision point: if this proves
  flaky, v1 gates pause in attach mode with a clear message.

## Data extraction — helpers.pl

- **Wire convention.** Helpers are called through the debugger's eval
  as a single line (`p Devel::TdbHelper::stack()`); each returns
  exactly one line `TDB>>>{...json...}<<<TDB`. The driver matches only
  marked lines. JSON via JSON::PP. All entry points wrapped in
  `eval {}` — a helper bug degrades to a JSON error reply, never a
  debuggee crash. All helpers live in package Devel::TdbHelper.

- **Inventory.**

| Helper | Serves DAP | Returns |
|---|---|---|
| `location()` | stopped event | file, line, sub name, protocol version |
| `stack()` | stackTrace | frames: file, line, sub, args summary (@DB::args) |
| `scopes(frame)` | scopes | handles for Lexicals / Globals / Specials |
| `vars(frame, scope)` | variables | name, value preview, expandable, child handle |
| `expand(handle)` | variables (nested) | one level of children |
| `evaluate(frame, expr)` | evaluate | preview + expandable handle |
| `breakable(file)` | setBreakpoints | breakable lines from %{"_<$file"} |
| `source(file)` | source request | content from @{"_<$file"} (serves remote attach) |

- **Lexicals strategy** (in order): (1) PadWalker if installed;
  (2) core-only read-only B pad walk (CV → PADLIST → names/values;
  stable from 5.18); (3) on failure, that scope degrades to
  `<lexicals unavailable — install PadWalker>` while Globals and
  Specials still work. Top-frame `evaluate` additionally gets Perl's
  native magic (package-DB evals see the stopped frame's lexicals),
  so the console works there even degraded.
- **Variable tree.** Adapter-side registry: variablesReference →
  (frame, access path). `expand` dumps one level per call — lazy, and
  circular refs are harmless. Dumping rules: blessed refs show class
  name first; overloaded objects via `overload::StrVal` (never
  trigger user overloads); tied vars labeled `tied via Class`; code
  refs show symbol name; undef distinct from empty string.
- **Specials scope.** Per frame: `$_`, `@_` (real per-frame args from
  @DB::args), `$@`, `$!`, `$0`, `@ARGV`, `@INC`, `%ENV`,
  input/output separators. Read-only in v1 except via the console.

## tdb integration

- **Profile** (`languages/perl.py`, mirrors cpp.py):
  - `PerlAdapter(AdapterSpec)`, `id = "perl-tdb"`; `command()` =
    `[sys.executable, "-m", "tdb.adapters.perl"]` (bundled —
    AdapterNotFoundError impossible for the adapter itself).
  - `launch_body`: program/args/cwd/env/stopOnEntry + `perl`
    (interpreter override) when configured. `attach_body`: host/port.
  - `pick_exception_filters` → `[]`. Uncaught `die` = stderr output +
    terminated event (exception breakpoints are future work).
  - `build_perl_profile(adapter, adapter_paths)`: single adapter.
    Documented twist: `{"adapters": {"perl": "/opt/bin/perl"}}` names
    the PERL INTERPRETER to spawn (the adapter binary is always tdb's
    own module).
  - No quirks. Capabilities all off (line stepping, no child
    processes, no task inspection) — existing gating covers
    everything with zero new code.
  - `Presentation(lexer="perl")`.
- **Detection.** Extensions `.pl`, `.pm`, `.t` → perl; `#!...perl`
  shebang branch beside the python one. `--lang perl` forces.
- **CLI.** `-r/--remote-attach` becomes allowed for perl
  (`tdb --lang perl -r host:5678`). `--lang perl` is REQUIRED with
  `-r` — with no local program there is nothing to auto-detect from,
  and detection's no-program default remains python. `--local-root`/`--remote-root`/
  `-k` mapping semantics carry over; when a mapped local file is
  missing, Code View falls back to the `source()` helper before the
  `<Could not read>` placeholder. `--python`/`--pv` stay python-only;
  perl interpreter override is config-only in v1. `-k`/`-t` work
  as-is (language-neutral).
- **Packaging.** `helpers.pl` and `Devel/TdbRemote.pm` ship as
  package data in the wheel (pyproject.toml). Docs show the
  copy-two-files recipe for remote boxes.
- **Docs (same change, not deferred).** README: languages-table row
  (Perl | perl-tdb (bundled) | needs perl ≥ 5.18 | core + remote
  attach), remote-attach walkthrough mirroring the debugpy one,
  PadWalker note, arming caveat. SKILL.md: perl in auto-detection
  list + debug_attach notes.

## Error handling

- **Missing/old perl.** Preflight `perl -e 'require v5.18'` at
  launch; failure → DAP error naming the problem + the
  `{"adapters": {"perl": ...}}` override. Surfaces through the same
  startup-error path as the C++ adapters.
- **Helper delivery.** Launch mode: `do '<local path>/helpers.pl'`.
  Attach mode: TdbRemote loads helpers.pl from its own directory.
  `location()` carries a protocol version; mismatch → "update
  Devel/TdbRemote.pm + helpers.pl on the remote host".
- **Protocol failures.** Prompt timeout or unparseable helper JSON →
  the in-flight DAP request errors, message includes the raw socket
  tail; the session survives. Socket EOF → terminated event. Adapter
  death → DAPClient's existing watcher.
- **Attach failures.** Connect refused/timeout → error reminding of
  listen()/wait_for_client() and reachability.

## Testing

Four layers (pattern of the C++ work); everything skips wholesale
when perl is absent:

1. **Pure-Python unit** (no perl): prompt state machine on recorded
   transcripts; breakpoint diffing; variablesReference registry;
   detection/CLI; profile-contract suite picks up perl via
   known_languages() automatically.
2. **Helper tests** (skip-if-no-perl): run helpers.pl functions under
   plain perl against fixture structures (nested / blessed / tied /
   overloaded / circular), assert exact JSON shape. No debugger.
3. **Adapter integration** (skip-if-no-perl): scripted DAP over stdio
   against real `perl -d` fixtures: launch → breakpoint → step →
   stack → variables → evaluate → quit; B-walk lexicals on the
   running perl; forced-degradation test for the PadWalker fallback.
4. **End-to-end** (like test_cpp_session.py): DebugController + real
   adapter + real perl, launch AND attach (fixture calls
   listen()/wait_for_client() on an ephemeral port). This test
   decides whether attach-mode pause ships or is gated.

## Future work (explicitly out of v1)

Watch expressions (language-neutral tdb feature), fork following,
exception breakpoints (`o dieLevel`-based), `--terminal` for perl,
Windows test coverage, writable Specials, statement-granularity
stepping via PPI.

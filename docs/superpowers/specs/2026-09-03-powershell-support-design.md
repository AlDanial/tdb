# PowerShell Debugging Support — Design

**Date:** 2026-09-03
**Status:** Approved design, pre-implementation
**Branch:** `add-powershell-support` (based on `main` @ 0970f25)
**Goal:** Debug PowerShell 7 scripts in tdb via PowerShell Editor Services
(PSES), the Debug Adapter Protocol server behind the VS Code PowerShell
extension. Tier: launch, stepping, breakpoints with conditions / hit counts
/ log messages, stack / variables / evaluate console, script output in the
Console View, fatal-error modal, and `--run` (pause while running).

## Scope

In scope (v1):

- Launch-mode debugging of `.ps1` / `.psm1` scripts with `pwsh` 7.x on
  Linux and macOS.
- `--run` mode: PSES honors DAP `pause` mid-script (probe-verified).
- `--record` / `--replay` fall out of the generic DAP client and need no
  PowerShell-specific work; they are exercised by one replay test.
- A Windows transport seam in the proxy (named pipe vs. UNIX socket) so
  Windows support is a follow-up of verification, not rework. Windows is
  documented as *experimental / untested* in v1.

Out of scope (follow-ups, each its own project):

- `--terminal` (external terminal). Rejected with the Go-style
  "not supported for PowerShell yet" message.
- Remote / process attach (PSES can attach to another pwsh process by pid
  via `Enter-PSHostProcess`). `-r` / `-a` with `--lang powershell` are
  rejected upstream in `cli.py` like bash.
- Break-on-error. PowerShell 7 supports `$ErrorActionPreference = 'Break'`,
  which makes the engine debugger stop where an error is raised; PSES
  surfaces that as a normal stop. The injection point (a `-Command`
  preamble vs. an `evaluate` before `configurationDone`) needs a probe.
- Windows PowerShell 5.1 (Windows-only engine; PSES still supports it but
  tdb targets `pwsh` only).
- Windows CI job (`windows-latest`) and any fixes it surfaces.
- Auto-downloading PSES.

Not to be committed: `examples/powershell_7.6.5-1.deb_amd64.deb` (an
untracked interpreter package the user downloaded; add it to
`.gitignore`'s local excludes or delete it — it is not part of this work).

## Probe-verified facts (2026-09-03, pwsh 7.6.5 / PSES v4.7.0, Linux)

Verified by driving PSES with a scripted DAP client (throwaway scripts in
the session scratchpad, not in the repo). Load-bearing for the design:

- **Start command.** `pwsh -NoLogo -NoProfile -NonInteractive -File
  <PSES>/Start-EditorServices.ps1 -HostName tdb -HostProfileId tdb
  -HostVersion <v> -BundledModulesPath <PSES parent> -LogPath <dir>
  -LogLevel None -SessionDetailsPath <file> -DebugServiceOnly` plus a
  transport switch: `-Stdio` or `-DebugServicePipeName <name>`.
  `-LogLevel None` silences all PSES chatter on stdout.
- **Transports.** Both work on Linux. With `-Stdio` PSES emits **no DAP
  `output` events at all**: script output (`Write-Host`, `Write-Output`,
  `Write-Error`) is lost. With `-DebugServicePipeName` PSES writes a
  session file `{"status":"started","debugServiceTransport":"NamedPipe",
  "debugServicePipeName":"/tmp/CoreFxPipe_<name>","powerShellVersion":
  "7.6.5"}` and listens on that UNIX socket; pwsh's own stdout then
  carries the script output. On Windows the same field names a real named
  pipe (`\\.\pipe\<name>`).
- **stdout echo.** When the script starts, PSES's temporary console prints
  one fake prompt line: `PS <cwd>> . '<script>' arg1 arg2`.
- **`launch` request.** Fields `script`, `args`, `cwd` work.
  `stopOnEntry` and `env` are declared on `PsesLaunchRequestArguments`
  but **never read** by the launch handler (verified in
  `LaunchAndAttachHandler.cs` @ main). `executeMode` defaults to
  dot-sourcing.
- **stopOnEntry emulation.** A breakpoint on line 1 of the script is
  reported `verified: true` and PowerShell stops at the *first executable
  statement* (comments and `function` definitions are skipped).
- **Delayed stack traces.** `stackTrace` without `levels` returns one
  placeholder frame `{"name": "<Breakpoint>", "presentationHint":
  "label", line, column}`. With `levels: N` the real frames follow it
  (`Add`, `Outer`, `<ScriptBlock>`). tdb's client already sends
  `levels=20` (`dap/client.py::stack_trace`), so no work is needed; the
  placeholder frame is frame 0 and carries the accurate current position.
- **Scopes:** `Auto`, `Command`, `Local`, `Script`, `Global`. `Global`
  contains PSES internals (`$__psEditorServices_DebugServer`, ...).
- **Capabilities advertised:** `supportsConfigurationDoneRequest`,
  `supportsFunctionBreakpoints`, `supportsConditionalBreakpoints`,
  `supportsHitConditionalBreakpoints`, `supportsSetVariable`,
  `supportsDelayedStackTraceLoading`, `supportsLogPoints`,
  `supportsCancelRequest`. No exception filters.
- **pause** works while the script runs; the resulting `stopped` event has
  `reason: "step"`, not `"pause"`.
- **evaluate** with `context: "repl"` prints the result to pwsh stdout and
  returns `result: ""`. Contexts `watch` / `hover` / `variables` /
  `clipboard` return the value in the response body. A failing expression
  (`$nope.Foo()`) returns `success: true, result: ""`.
- **terminate** is unsupported (`Method not found - terminate`).
  **disconnect** succeeds but pwsh keeps running afterwards.
- **Uncaught `throw`** does not stop the debugger: the script terminates
  (`terminated` event, no `exited`), and pwsh prints its concise error
  view on stdout with ANSI colour, first line
  `Exception: <path>:<line>` followed by a `Line |` source snippet.
- **Session teardown.** PSES emits `terminated` when the script ends but
  never `exited`; pwsh stays alive until killed.

## Architecture

Approach: a bundled Python DAP proxy, the Ruby shape.

```
tdb  --stdio-->  tdb.adapters.powershell (proxy)  --UNIX socket / named pipe-->  pwsh + PSES
                                                   <--pwsh stdout pipe--
```

- `src/tdb/languages/powershell.py` — `PowerShellAdapter(AdapterSpec)` +
  `build_powershell_profile`, as thin as `bash.py`.
- `src/tdb/adapters/powershell/{__init__,__main__,server.py}` — the proxy.
  Store-and-forward with seq renumbering, copying the structure of
  `adapters/ruby/server.py` (locally handled requests, forwarded rest,
  stdout pump, process-group teardown).
- `src/tdb/languages/errors.py` — `parse_powershell_error`.
- `src/tdb/languages/registry.py` — extension / shebang detection, builder
  registration.

Nothing PSES-specific lands in `dap/client.py`, `session/controller.py`,
or the widgets. The controller's existing behavior (levels=20 stack
traces, evaluate context pass-through, run-mode pause) is sufficient once
the proxy normalizes PSES.

## Language profile, registry, CLI

`PowerShellAdapter`:

- `id = "pses"`; `command()` returns `[sys.executable, "-m",
  "tdb.adapters.powershell"]` (never raises `AdapterNotFoundError`; a
  missing pwsh/PSES is reported by the proxy at launch, as with rdbg).
- `launch_body` returns `{"type": "powershell", "request": "launch",
  "program", "args", "cwd", "stopOnEntry", "console"}` plus `env` when
  given, plus `"pwsh": <path>` and `"pses": <dir>` when the config
  overrides are set (mirrors ruby's `"rdbg"` field). `console ==
  "externalTerminal"` raises `LanguageNotSupportedError("--terminal is not
  supported for PowerShell yet")`.
- `attach_body` raises `LanguageNotSupportedError`.
- `pick_exception_filters` returns `[]`.

`build_powershell_profile(adapter, adapter_paths, program)`:

- Accepts `adapter in (None, "pses")`; anything else -> "unknown adapter".
- `adapter_paths["pwsh"]` -> interpreter override; `adapter_paths["pses"]`
  -> PSES module directory override.
- `LanguageProfile(id="powershell", display_name="PowerShell",
  presentation=Presentation(lexer="powershell",
  parse_error=parse_powershell_error, frame_placeholder="<ScriptBlock>"),
  capabilities=ProfileCapabilities(pause_while_running=True))`.
- `opaque_frame`: `lambda name: name == "<Breakpoint>"` is **not** used —
  frame 0 is the only frame with the exact current line/column, and its
  scopes resolve correctly (verified: `scopes {frameId: 0}` returns
  Local/Script/Global). It stays selectable.

Registry:

- `_EXTENSION_MAP`: `".ps1": "powershell"`, `".psm1": "powershell"`.
- Shebang: first line containing `pwsh` -> powershell (checked after the
  existing shebang rules; none of them match "pwsh").
- `register("powershell", build_powershell_profile)` after go.

CLI: `-r` / `-a` with `--lang powershell` are rejected with the existing
bash-style message; `--terminal` is rejected by the adapter (above).

Config precedence for the PSES directory (documented in README):

1. `{"adapters": {"pses": "<dir>"}}` in tdb's `config.json`
2. `TDB_PSES_PATH` environment variable
3. Newest `~/.vscode/extensions/ms-vscode.powershell-*/modules/PowerShellEditorServices`
   (also `~/.vscode-insiders`, `~/.vscode-server`); "newest" = highest
   extension version parsed from the directory name.
4. Not found -> launch fails with one line: where to download
   `PowerShellEditorServices.zip` (GitHub releases, pinned version in the
   message), where to unzip it, and the two ways to point tdb at it.

`<dir>` is the directory containing `Start-EditorServices.ps1`. The proxy
also accepts the *parent* (the unzip root) and descends one level if the
script is not found directly.

Interpreter precedence: `{"adapters": {"pwsh": "<exe>"}}`, else `pwsh` on
PATH. Not found -> "pwsh (PowerShell 7) not found on PATH — install from
https://aka.ms/powershell, or set {"adapters": {"pwsh": ...}}".

## Proxy internals (`PowerShellDapServer`)

### Process spawn

- Resolve pwsh and PSES per the precedence above.
- Pipe name: `tdb-pses-<pid>-<8 hex>`; session file and log dir in a
  short `tempfile.mkdtemp(prefix="tdb-pses-")` (UNIX socket path length is
  PSES's problem here — it always uses `/tmp/CoreFxPipe_<name>` — but the
  session-file directory is ours and must be removed on teardown).
- `subprocess` with `start_new_session=True` (POSIX) /
  `CREATE_NEW_PROCESS_GROUP` (Windows), `stdin=DEVNULL`, `stdout=PIPE`,
  `stderr=STDOUT`, `cwd` = the launch request's `cwd`, env = `os.environ`
  merged with the request's `env`, plus `NO_COLOR=1` and
  `TERM=dumb` (belt and braces; verify during implementation which one
  pwsh 7.6 honours for `$PSStyle.OutputRendering`, keep both if harmless).
- Wait for the session file to appear and parse as JSON with
  `status == "started"`, bounded by `_timeouts.ADAPTER_LISTEN`. Timeout or
  early pwsh exit -> launch response `success: false` with the last ~20
  lines of pwsh output, then teardown.

### Transport seam

One function `connect_debug_service(details: dict) ->
(asyncio.StreamReader, asyncio.StreamWriter)`:

- POSIX: `asyncio.open_unix_connection(details["debugServicePipeName"])`.
- Windows: `\\.\pipe\<name>` opened via `ProactorEventLoop`'s pipe
  support (`loop.create_pipe_connection` / `open` + thread-backed
  streams). Written but only smoke-tested by the follow-up Windows CI
  project; v1 unit tests cover the POSIX branch and assert the Windows
  branch is selected on `sys.platform == "win32"` via monkeypatching.

Everything after connect uses `tdb.dap.protocol.read_message` /
`encode_message` on that stream pair, exactly as the ruby proxy does.

### Locally handled requests

| Request | Handling |
|---|---|
| `initialize` | Answered from static `CAPABILITIES` = the PSES list above (PSES is not running yet). `supportsTerminateRequest: True` is added because the proxy implements it. |
| `launch` | Spawn + connect (above), then forward `{"script": program, "args", "cwd"}` to PSES. Remember `stopOnEntry` and the script path. Reply with PSES's response. |
| `setBreakpoints` | If `stopOnEntry` and the source is the main script and the entry stop has not happened yet: append `{"line": 1}` to the forwarded list; strip the last entry from the response. Otherwise forward verbatim. |
| `configurationDone` | Forward. If `stopOnEntry` and the client never sent `setBreakpoints` for the main script, send the synthetic line-1 `setBreakpoints` first. |
| `pause` | Forward; set `pause_pending = True`. |
| `evaluate` | Rewrite `context: "repl"` -> `"watch"`; forward. |
| `terminate` | Answer locally (`success: true`), then teardown. |
| `disconnect` | Forward to PSES with a short timeout (ignore failure), answer, then teardown. |

### Event rewrites

- `stopped`: if the entry stop is pending, rewrite `reason` to `"entry"`,
  mark it done, and re-send the user's own breakpoint list for the main
  script (without the synthetic line 1) *before* forwarding the event, so
  the user never observes the extra breakpoint. Else if `pause_pending`,
  rewrite `reason: "step"` -> `"pause"` and clear the flag. Any other
  `stopped` clears `pause_pending` too (a real breakpoint beat the pause).
- `terminated`: forwarded as-is; the proxy then waits for pwsh to exit
  (it does not, on its own — see teardown) so `exited` is synthesized.

### stdout pump

A task reads pwsh stdout line-by-line and emits `output` events with
`category: "stdout"`. Filtering: drop the single line matching
`^PS .*> \. '.*'` that arrives first after `configurationDone` (the
echoed prompt); nothing else is filtered. The pump also appends every
line to a bounded ring buffer (last 200 lines) which feeds (a) the
launch-failure message and (b) `parse_powershell_error` via the normal
`ServerEventHandler` stderr-check path — the controller's fatal-error
check reads accumulated **stderr**-category output, so the proxy tags
lines as `stderr` from the first line matching the concise error header
(`^\w+(Exception|Error)?: .*:\d+$` or `^Exception: `) until the
`terminated` event. Verify this heuristic against `Write-Error` (which
prints `Write-Error: <msg>` without a path and is *not* fatal) in tests.

### Teardown

Triggered by `disconnect`, `terminate`, tdb's stdin EOF, or pwsh exit.
Steps: send `disconnect` to PSES if the socket is open (ignore errors);
close the socket; if pwsh is still alive, `SIGTERM` the process group,
wait `_timeouts` grace, then `SIGKILL`; emit `exited {exitCode}` (real
code, or 0 when the proxy killed a script that had already `terminated`)
followed by `terminated` if not yet sent; remove the temp dir. Idempotent.

## Fatal-error modal

`parse_powershell_error(text, exit_code) -> ParsedError | None`:

- Input: accumulated stderr-category text (ANSI already absent thanks to
  `NO_COLOR`; strip escapes defensively anyway).
- Recognize the concise view: header line `<Kind>: <path>:<line>` where
  `<Kind>` is `Exception`, `<Name>Exception`, or a cmdlet-error name;
  then optional `Line |` / `N | <source>` / `| ~~~` lines; then the
  `| <message>` line.
- `ParsedError(header=<Kind>: <path>:<line>, message=<message>,
  frames=[ErrorFrame(path, line, func="")], detail=<original text>)`.
  PowerShell's concise view names only the throwing location, so one
  frame; `frame_placeholder="<ScriptBlock>"` labels it.
- Return `None` for `Write-Error` output (no `path:line` header) and for
  any text when `exit_code == 0`.

## `--run`

`pause_while_running=True`; the proxy's `pause` handling above supplies
the `reason: "pause"` stop that run mode's `ConsoleRunHandler` expects. No
other changes.

## `--record` / `--replay`

No PowerShell-specific work; the proxy is just another stdio adapter.
One replay test (`tests/integration/test_replay_powershell.py`, modelled
on `test_replay_ruby.py`) proves it.

## Version floors

- `pwsh` >= 7.2 (`$PSStyle` / `NO_COLOR`; earlier 7.x untested). The
  proxy reads `powerShellVersion` from the session file and refuses < 7.0
  with a clear message; 7.0–7.1 get a warning line in the Console View.
- PSES >= 4.0 (`-DebugServiceOnly` + session file shape). The README pins
  the tested release (v4.7.0) in its download command.

## Testing

Unit (`tests/unit/`, no pwsh needed):

- `test_powershell_profile.py`: builder, adapter-id rejection, config
  overrides landing in the launch body, `--terminal` rejection, attach
  rejection, capabilities.
- `test_registry_powershell.py`: `.ps1` / `.psm1` / `#!/usr/bin/env
  pwsh` detection; `extensions_for("powershell")`.
- `test_pses_lookup.py`: precedence (config > env > VS Code dir), newest
  extension version wins, unzip-root descent, not-found message text.
- `test_powershell_errors.py`: `parse_powershell_error` on captured pwsh
  7.6 output (throw, `[int]::Parse("x")` .NET exception, `Write-Error`
  non-fatal, exit code 0 guard).
- `test_powershell_proxy.py`: drive `PowerShellDapServer` against a fake
  PSES (asyncio UNIX-socket server + a fake `pwsh` Python script that
  writes the session file and prints canned stdout). Covers: initialize
  answered statically; launch spawn/connect/forward; stopOnEntry merge and
  strip; `reason` rewrites for entry and pause; evaluate context rewrite;
  prompt-line filter; stdout -> `output` events; stderr tagging of the
  error block; terminate/disconnect teardown kills the fake pwsh; launch
  failure surfaces pwsh output.

Integration (`tests/integration/`, skipped unless `pwsh` and a resolvable
PSES are present):

- `powershell_adapter_harness.py`: fixture scripts under
  `tests/integration/fixtures/powershell/` (`simple.ps1`, `functions.ps1`,
  `loop.ps1`, `throws.ps1`, `writes_error.ps1`) + a `DebugController`
  driver modelled on `ruby_adapter_harness.py`.
- `test_powershell_adapter_launch.py`: entry stop on first executable
  line; args visible in `$args`; cwd honoured; env var visible; stdout
  captured; clean teardown leaves no `pwsh` child (poll `/proc` or
  `psutil`-free `os.kill(pid, 0)`).
- `test_powershell_adapter_breakpoints.py`: line, conditional, hit-count,
  log-point; breakpoint inside a function; set-while-running.
- `test_powershell_adapter_stepping.py`: next / stepIn / stepOut across
  `Outer -> Add`; stack shows `<Breakpoint>`, `Add`, `Outer`,
  `<ScriptBlock>`.
- `test_powershell_adapter_inspection.py`: scopes list; `Local` variables;
  `setVariable`; evaluate returns values (not empty); evaluate of a
  failing expression returns empty result without hanging.
- `test_powershell_session.py`: end-to-end via `DebugController` +
  `ServerEventHandler`: uncaught throw -> `terminated`, `exited(1)`, and
  `parse_powershell_error` yields the modal data; `Write-Error` script
  exits 0 and yields no modal.
- `test_powershell_run_mode.py`: `--run` on `loop.ps1`, pause lands with
  `reason == "pause"`, evaluate `$i` works, continue, second pause.
- `test_replay_powershell.py`.

CI:

- `Dockerfile`: install pwsh from Microsoft's
  `powershell-<ver>-linux-musl-x64.tar.gz` (Alpine; the `.deb` in
  `examples/` is for Debian hosts only) into `/opt/microsoft/powershell/7`
  with a `/usr/local/bin/pwsh` symlink; download and unzip the pinned
  PSES release to `/opt/pses` and `ENV TDB_PSES_PATH=/opt/pses`.
- `.github/workflows/test.yml`: same PSES download step on the Linux job
  (ubuntu runners ship pwsh already).
- Windows job: follow-up project.

## Documentation

- README: language list bullet; supported-languages table row (`pses`,
  requirement "pwsh >= 7.2 + PowerShell Editor Services module", features
  "core debugging + `--run`; no `--terminal`, no attach"); detection list
  entries; a `### PowerShell` section with the PSES download/unzip
  commands, the three lookup methods, the two config keys, the
  Windows-experimental note, and the Windows PowerShell 5.1 exclusion;
  Windows note in the platform section.
- `examples/hello_powershell.ps1`: small script with a function call,
  a loop, and a `Write-Error` so each Console View category is visible.
- `CLAUDE.md`: no change needed.

## Risks / open implementation questions

- **`NO_COLOR` vs `TERM=dumb`.** Which one pwsh 7.6 honours for host
  output rendering is unverified; the first implementation task checks
  both and documents the result in the proxy module docstring.
- **Prompt-echo filter fragility.** The echoed line format (`PS <cwd>> .
  '<script>' ...`) comes from PSES's temporary console; a PSES upgrade
  could change it. The filter is one regex in one place, and a miss only
  leaks one cosmetic line into the Console View.
- **Entry breakpoint on a script whose line 1 is executable.** The
  synthetic and a user breakpoint on line 1 would coincide; the merge
  must not duplicate, and stripping must keep the user's entry. Unit test.
- **stderr tagging heuristic** for the fatal-error modal may misclassify
  a script that deliberately prints `Foo: /path:3`. Acceptable: the only
  consequence is text landing in the modal, gated on non-zero exit.
- **Windows named pipe client** is untested until the follow-up CI job.
- **PSES pinned version drift.** The README and Dockerfile pin v4.7.0; a
  newer release that changes the session-file shape or start-script
  parameters would surface as a launch failure with PSES's own output.

## Addendum (2026-09-03, pre-plan probes)

Four further probe results, found while writing the implementation plan,
amend the sections above. Where they conflict, this addendum wins.

1. **Exit codes are not observable through DAP.** After `terminated`,
   `evaluate` of `$LASTEXITCODE` / `$?` returns an empty result, and PSES
   never emits `exited`. The proxy therefore launches a bundled
   *launcher script* instead of the user's script:
   `src/tdb/adapters/powershell/tdb_launch.ps1` takes the user script path
   and its arguments, runs the script with the call operator
   (`& $Script @ScriptArgs`, so `exit N` returns to the launcher with
   `$LASTEXITCODE = N`), and prints one sentinel line
   `"\x1etdb-exit:<code>"` on stdout. The proxy strips the sentinel from the
   Console View and uses it as the `exited` code. An uncaught terminating
   error propagates through `&`, so the launcher never reaches the
   sentinel; PSES sends `terminated`, pwsh prints the concise error view,
   and the proxy reports exit code **1**. Probe-verified under PSES:
   `exit 7` -> sentinel 7; `throw` -> no sentinel + concise view naming the
   *user* script; entry breakpoint on line 1 of the user script still lands
   on its first executable statement (the launcher's own lines are never
   breakpointed); breakpoints inside functions hit. Cost: one extra bottom
   frame `<ScriptBlock>` whose source is `tdb_launch.ps1`, documented in
   the README. The "Teardown" paragraph's "real code, or 0" wording is
   replaced by this rule.
2. **PSES joins launch `args` unquoted** into the PowerShell command line
   (`. '<script>' arg1 arg2`). An argument containing a space or an
   apostrophe is split or breaks the parse (the run then terminates with
   no output). The proxy single-quotes every argument as a PowerShell
   literal (`'` -> `''`) before forwarding — the launcher path is passed
   as `script` (PSES escapes that one itself); the user script path and
   all user args go through the quoting.
3. **`NO_COLOR=1` and `TERM=dumb` both** switch pwsh 7.6 to plain-text
   error rendering; the proxy sets both.
4. **Error header kinds.** The concise view header is
   `<Kind>: <path>:<line>` where `<Kind>` is `Exception`, a .NET exception
   class (`MethodInvocationException`), **or a cmdlet name** (`Get-Item:`).
   Non-terminating errors print the identical block and the script
   continues with exit code 0, so `parse_powershell_error` MUST return
   `None` unless `exit_code` is non-zero; the message may span several
   `|`-prefixed lines and is joined with spaces.

## Addendum 2 (2026-09-04, from the first real-PSES integration run)

1. **PSES drops requests sent before its `initialize` response.** The proxy
   must await the reply to its proxy-originated `initialize` before forwarding
   `launch` (it runs on the client loop, so awaiting is safe). PSES answers
   `launch` first and emits `initialized` after it.
2. **stopOnEntry emulation, corrected.** A line-1 breakpoint on the user
   script binds to a *function body* when line 1 is a `function` definition
   (it then fires only when that function is first called, or never). The
   proxy instead sets a synthetic breakpoint on `tdb_launch.ps1`'s
   `& $Script @ScriptArgs` line, swallows that stop, clears the launcher
   breakpoint, issues `stepIn`, and reports the resulting stop as reason
   `"entry"`. The user's own breakpoint lists are never merged or rewritten;
   the "setBreakpoints" and "configurationDone" rows of the proxy table
   above are replaced by this mechanism.
3. **`Write-Error` output is tagged `stderr`.** PowerShell renders
   non-terminating errors as ConciseView blocks, so the classifier tags them
   like fatal ones; this is correct console colouring. No fatal-error modal
   results because `parse_powershell_error` returns `None` for exit code 0.
   The block names the launcher's `&` line rather than the user's
   `Write-Error` line (cosmetic, deferred). The "Write-Error stays stdout"
   wording in the body is superseded.
4. **Abrupt proxy death.** On Linux the proxy sets `PR_SET_PDEATHSIG`
   (SIGKILL) on the pwsh child so a SIGKILLed proxy cannot orphan pwsh.
5. Real stack under a breakpoint inside `Add`:
   `<Breakpoint>`, `Add`, `Outer`, `<ScriptBlock>` (user script),
   `<ScriptBlock>` (tdb_launch.ps1), then a source-less
   `Interactive Session` frame at the bottom.

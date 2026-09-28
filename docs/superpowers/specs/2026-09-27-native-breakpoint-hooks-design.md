# Native live breakpoint hooks: C/C++, Rust, OCaml

Date: 2026-09-27
Status: approved design, awaiting implementation plan

## Goal

Give C, C++, Rust, and native OCaml programs the same one-line live
breakpoint hook that Python (`tdb.breakpoint()`), Perl
(`Devel::TdbRemote::breakpoint()`), Ruby (`Tdb.breakpoint`), and Go
(`tdb.Breakpoint()`) already have. A user runs their program normally, not
under tdb. When execution reaches the hook, tdb opens on the program's
terminal attached to the live process, paused on the line after the call
with locals visible. Quitting tdb detaches and the program runs on. Later
hook calls reuse the running tdb or spawn a fresh one.

As the enabling step, `tdb -a PID` (today Go-only) is generalized to any
native binary debugged through gdb or lldb-dap.

## Non-goals

- OCaml bytecode (ocamlearlybird cannot attach to a pid).
- `--remote-attach` for OCaml (stays unsupported; unrelated to this work).
- Hooks on macOS or Windows. `tdb -a PID --lang X ./binary` may work on
  macOS with lldb-dap but is not tested or documented here.
- Changing `--remote-attach` semantics for gdb (`target remote` attach
  keeps resuming the debuggee).

## Verified mechanics (probe, 2026-09-27, gdb 17.1 and lldb-dap 21.1.8)

These results were measured with a throwaway DAP driver against a C
program that waits for TracerPid and then calls `tdb_breakpoint_stop()`.
The design leans on all of them.

1. Both adapters accept a `pid` attach argument alongside `program`.
2. A `setFunctionBreakpoints` for `tdb_breakpoint_stop` sent before
   `configurationDone` is reported `pending` by gdb and becomes
   `verified` when gdb loads the program during attach. lldb-dap has
   the target loaded at attach time and verifies it immediately.
3. gdb order: `configurationDone` response, then the attach response,
   then `stopped` with reason `attach`. This is the reverse of the
   remote-stub order the controller assumes today (stop before
   response). lldb-dap: attach response, then `initialized`, then
   `configurationDone` either continues or stops per `stopOnEntry`.
4. The hook stop arrives as reason `breakpoint` on both adapters with
   frames `tdb_breakpoint_stop` -> `tdb_breakpoint` -> caller. Two
   `stepOut` requests land on the caller's next line with correct
   locals.
5. `disconnect` with `terminateDebuggee: false` leaves the process alive
   with TracerPid 0 on both adapters. A second attach to the same
   process works and the program runs to completion afterwards.

## Design

### 1. tdb side: native pid attach

**CLI (`src/tdb/cli.py`).**

- `-a/--attach PID` is accepted for profiles `go`, `cpp`, `rust`, and
  `ocaml`. For `ocaml` the adapter must be `gdb` or `lldb-dap`; if the
  resolved adapter is `ocamlearlybird` the CLI errors with
  "pid attach needs a native OCaml adapter (--adapter lldb-dap or gdb)".
  Other profiles keep today's error, reworded to list the four languages.
- When `-a` is given without a program, on Linux the program defaults to
  `os.path.realpath(f"/proc/{pid}/exe")`. Elsewhere the CLI errors asking
  for the program path. A program given explicitly wins (for a separate
  symbol file or a copied binary).
- Language sniffing for `-a` without `--lang` reads the pid's
  executable and checks, in order: Go buildinfo (`is_go_binary`) ->
  `go`; OCaml runtime markers (`ocaml_flavor(...) == "native"`) ->
  `ocaml`; Rust runtime symbol names (`rust_eh_personality` or
  `__rust_alloc` present in the file) -> `rust`; any other ELF -> `cpp`.
  Program-file detection (`registry.detect`) is unchanged; Rust binaries
  passed as a program still need `--lang rust` as documented.
- `--no-pause-on-attach` keeps its meaning: the debuggee is about to
  stop itself, so do not stop it at attach.
- The registry `resolve()` and the `cpp`/`rust`/`ocaml` builders accept
  an `attach_pid` keyword (Go's builder already does).

**Adapters (`src/tdb/languages/cpp.py`, `rust.py`, `ocaml.py`).**

- `GdbDapAdapter` and `LldbDapAdapter` take `attach_pid: int | None`.
  With a pid:
  - gdb `attach_body` -> `{"program": program, "pid": pid}`.
  - lldb-dap `attach_body` -> `{"program": program, "pid": pid,
    "stopOnEntry": opts["pause_on_attach"]}`.
  - Rust subclasses keep appending their `initCommands`; the OCaml
    lldb subclass appends its formatter import in the attach body too.
  - `OCamlLldbAdapter` currently opts out of `attach_via_adapter`; with a
    pid it opts back in (instance-level quirks, as `DelveAdapter` does).
- New method `AdapterSpec.hook_function_breakpoints() -> tuple[str, ...]`,
  default `()`. gdb and lldb-dap adapters return
  `("tdb_breakpoint_stop",)` when built with a pid. The controller
  installs these during attach configuration before `configurationDone`.
  They are never entered into `state.breakpoints`, so the Breakpoints
  view does not show them (same as Go's `main.main` entry breakpoint).
- New quirk `AdapterQuirks.attach_stop_is_pausable: bool = False`. gdb
  sets it (instance-level) for pid attach. Meaning: the attach itself
  leaves the inferior stopped; the controller waits for that stop and
  then resumes unless the user asked to pause on attach. lldb-dap does
  not need it (`stopOnEntry` handles both cases inside the adapter).
- `ProfileCapabilities.breakpoint_hook_frame` predicates:
  - cpp: frame name in `{"tdb_breakpoint_stop", "tdb_breakpoint",
    "tdb_breakpoint_lang"}`.
  - rust: cpp set plus `"tdb::breakpoint"`.
  - ocaml (native adapters only): cpp set plus `"tdb_ocaml_breakpoint"`,
    `"caml_c_call"`, and raw names starting with `camlTdb.breakpoint_`
    or `camlTdb__breakpoint_` (OCaml's mangled forms; the predicate sees
    the raw frame name, not the demangled presentation).
  The predicates are set unconditionally; `dap_events` already gates on
  `is_remote_attach`.

**Controller (`src/tdb/session/controller.py`).**

- In `do_configure`'s attach branch: after user breakpoints, call
  `set_function_breakpoints(hook_function_breakpoints())` when non-empty.
- Post-attach block: when `quirks.resume_after_remote_attach` or
  `quirks.attach_stop_is_pausable` is set, await `_stopped_event` (10 s
  timeout, same as the bootstrap path) instead of testing
  `state.phase`, because gdb's pid-attach stop arrives after the attach
  response. Then:
  - `attach_stop_is_pausable and self._pre_arm_pause` -> leave stopped.
    The stop (reason `attach`) reaches the UI as a normal stop.
  - otherwise -> resume, exactly as today.
- No change to `stop()`: `disconnect(terminate=False)` in remote-attach
  mode already detaches (verified).

**`--info` (`src/tdb/info.py`).** New "C/C++" section with a
`tdb.h dir` row pointing at the packaged header directory, for
`-I "$(...)"` use. Rust and OCaml libraries live in the repository, not
the wheel, so they get documentation pointers only.

### 2. Hook libraries

One recipe, one stop symbol, three languages. Every hook does the
following, in order:

1. If stdin or stdout is not a terminal: return silently (programs can
   keep hook calls when run from a pipe or as a service).
2. Once per process: `prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY)` so a
   non-ancestor debugger (tdb's gdb/lldb-dap, which are descendants)
   may attach under Yama `ptrace_scope=1`.
3. Read `TracerPid` from `/proc/thread-self/status` (per thread: gdb
   and lldb attach thread by thread). If non-zero, a debugger is
   attached: call `tdb_breakpoint_stop()` and return.
4. Otherwise wait up to 3 s for a tdb spawned earlier to exit (its TUI
   lingers after detach; two TUIs on one tty race), then spawn
   `tdb --lang <lang> -a <pid> --no-pause-on-attach` with stdin, stdout,
   and stderr inherited. `tdb` is `$TDB` if set, else found on PATH.
5. Poll TracerPid every 50 ms for up to 60 s. Escape early if the
   spawned tdb exits first. On timeout or exit: warn on stderr
   ("tdb did not attach; continuing") and return without stopping.
6. Call `tdb_breakpoint_stop()`.

`tdb_breakpoint_stop` is an empty, no-inline, externally visible C
function; tdb's hidden function breakpoint lands there and the
step-out predicates carry the user to their own frame. Non-Linux builds
warn once and return. The hook never traps when untraced, so a program
without tdb installed still runs correctly.

- **C and C++** (`src/tdb/adapters/native/tdb.h`, shipped in the wheel,
  added to `pyproject.toml` package-data): header-only, C99 and C++
  compatible. `tdb_breakpoint_stop` and `tdb_breakpoint` are defined
  `__attribute__((weak, noinline))` (non-static) so including the header
  from several translation units links to one symbol. Public API:
  `void tdb_breakpoint(void);`, which calls the internal
  `tdb_breakpoint_lang("cpp")` (the profile covers both C and C++).
  `tdb_breakpoint_lang(const char *lang)` holds the whole recipe so the
  OCaml stub can reuse it with another language id.
- **Rust** (`rust/tdb/Cargo.toml`, `rust/tdb/src/lib.rs`): crate `tdb`,
  zero dependencies. It declares the single `prctl` FFI prototype itself
  (libc is always linked on Linux) so plain `rustc --crate-type lib`
  builds it in CI, where cargo is absent. `tdb_breakpoint_stop` is
  `#[no_mangle] #[inline(never)] pub extern "C"`. Public API:
  `tdb::breakpoint()`. `#[cfg(not(target_os = "linux"))]` variant warns
  once. Users depend on it as a cargo git dependency or a path.
- **OCaml** (`ocaml/tdb/`): `tdb.ml` exposes `Tdb.breakpoint : unit ->
  unit` as an `external` bound to the C stub `tdb_ocaml_breakpoint`,
  which includes a copy of `tdb.h` and calls
  `tdb_breakpoint_lang("ocaml")`. Files: `tdb.ml`, `tdb_stubs.c`, `tdb.h`
  (copy), `dune`, `dune-project`, `tdb.opam`. Users vendor the
  directory or `opam pin` it. A unit test asserts `ocaml/tdb/tdb.h` is
  byte-identical to the packaged header.

The hooks do not read any other environment variable; adapter choice
comes from the user's `config.json` `default_adapters`, like every other
tdb invocation.

### 3. Examples and documentation

- `examples/C/breakpoint_hook_demo.c`, `examples/C++/breakpoint_hook_demo.cpp`,
  `examples/Rust/breakpoint_hook_demo/` (Cargo project with a path
  dependency on `../../../rust/tdb`), `examples/OCaml/breakpoint_hook_demo.ml`
  with build instructions in a leading comment (plain `ocamlfind`/`ocamlopt`
  and dune).
- README: a "Live breakpoint hook" subsection in each of the C/C++,
  Rust, and OCaml sections mirroring the Perl/Ruby/Go ones (install,
  one-line usage, what happens on quit, Linux-only note, ptrace_scope
  note); the intro list and the Python hook section's cross-reference
  list gain the three languages; the `-a/--attach` documentation drops
  "Go only".
- `examples/README.md`: build lines for the four demos.

### 4. Testing

**Unit (`tests/unit/`).**

- CLI: `-a` accepted for cpp/rust/ocaml, rejected for python/perl/ruby/
  bash with the new message; earlybird rejection; program defaulted
  from `/proc/PID/exe` (monkeypatched); sniffing order with synthetic
  files; `--no-pause-on-attach` still requires `-a` or `-r`.
- Adapters: gdb and lldb-dap pid attach bodies; Rust and OCaml
  subclasses keep their init commands; `hook_function_breakpoints`
  only with a pid; OCaml lldb quirks flip with a pid.
- Controller: with a fake client, pid attach installs the hook
  function breakpoint before `configurationDone`, waits for the
  late attach stop, resumes without pause and stays stopped with it.
- Predicates: positive and negative frame names per language,
  including the OCaml mangled forms.
- Header sync: `ocaml/tdb/tdb.h` equals the packaged `tdb.h`.
- Packaging: the header is listed in `pyproject.toml` package-data.

**Integration (`tests/integration/test_native_breakpoint_hook.py`).**
Parametrized over language (c, cpp, rust, ocaml) x adapter (gdb,
lldb-dap), each skipped when its toolchain or adapter is missing.
Linux-only, like the Go test.

1. Build the demo against the hook library (gcc/g++, rustc with
   `--extern`, ocamlopt with `unix.cmxa` and the stub).
2. Run it under `HookDebuggee` (pty + fake tdb). Assert the fake tdb
   was spawned with `--lang <lang> -a <pid> --no-pause-on-attach`.
3. Kill the fake tdb, then play tdb's part for real: build the profile
   with `attach_pid`, drive `DebugController.remote_attach` with
   `pre_arm_pause=False`, wait for the breakpoint stop, assert the top
   frame satisfies the profile's `breakpoint_hook_frame`, step out until
   it does not, and assert the frame is the demo's caller on the line
   after the hook with the expected local value.
4. `stop()` (detach). Assert the debuggee is alive and untraced.
5. The demo's second hook call spawns the fake tdb again; repeat step 3
   for the second stop, detach, and assert the program exits 0 with its
   final output.

Rust and OCaml demo builds in tests must not require cargo, dune, or
ocamlfind.

**Manual.** Real-TUI run of each demo: stop on the caller line with
locals, `c` to the second stop, quit, program finishes; and quit at the
first stop, program respawns tdb at the second.

## Risks and mitigations

- **Yama `ptrace_scope` 2 or 3, or containers without `SYS_PTRACE`:**
  attach fails. The hook's poll times out with a warning naming the
  likely cause; tdb shows the adapter's attach error. Documented.
- **Optimized builds inline or drop the hook frames:** `tdb_breakpoint_stop`
  is `noinline`/`inline(never)` and externally visible; a stop still
  lands even at `-O2`, though locals may be optimized out (documented,
  as for any native debugging).
- **Stripped binaries:** the function breakpoint cannot resolve, so
  the hook's stop never fires. tdb attaches and the program runs on;
  the user sees an attached-but-running session. Documented: keep
  symbols (`-g`).
- **Two TUIs on one tty:** mitigated by the 3 s wait for the previous
  tdb, as in the Go hook.
- **gdb stop ordering regression for `--remote-attach`:** the new
  wait-for-stop covers both orders; the existing Rust remote-attach
  integration test guards it.

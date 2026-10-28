# Multi-Language Debugging

`tdb` debugs any language that has a Debug Adapter Protocol backend. Ten
languages are supported out of the box:

| Language | Adapter(s) | Dependencies           | Feature level |
|----------|------------|------------------------|---------------|
| Python | `debugpy` (default) | Python ≥ 3.11 | everything in this documentation |
| C / C++ / Fortran (any native binary) | `gdb` (default), `lldb-dap` (alternate) | `gdb -i dap` requires GDB ≥ 14 built with Python (RHEL 8/9: `dnf install gcc-toolset-14-gdb`, see [C/C++/Fortran](c-cpp-fortran.md)); `lldb-dap` ships with LLVM ≥ 17 (e.g. `apt install lldb`) | core debugging: breakpoints, stepping, stack, variables, evaluate console + remote attach (gdbserver/lldb-server, local symbol-bearing executable required) |
| Rust (explicit `--lang rust`) | `gdb` (Linux default), `lldb-dap` (macOS default) | any rustc building with debug info; GDB ≥ 14 or LLVM `lldb-dap` ≥ 17 (concurrency ownership evidence additionally requires stable Rust 1.98) | core debugging + best-effort Rust concurrency inspection + remote attach |
| Perl | perl-tdb (bundled) | perl ≥ 5.18 on PATH  | core debugging + remote attach |
| Bash | bash-tdb (bundled) | bash ≥ 4.4 on PATH  | core debugging (no remote attach) |
| Tcsh | tcsh-tdb (bundled) | tcsh on PATH | core debugging (no remote attach, no conditional breakpoints, no pause) |
| Ruby | `rdbg` (the [debug gem](https://github.com/ruby/debug)) | `rdbg` ≥ 1.9 on PATH | core debugging + remote attach |
| OCaml (native executable) | `lldb-dap` (default), `gdb` (alternate) | `lldb-dap` ships with LLVM ≥ 17; `gdb -i dap` requires GDB ≥ 14 | core debugging + domains-as-threads; Variables view shows no OCaml locals (upstream DWARF limitation); evaluate console is lldb/C-level, not OCaml |
| OCaml (bytecode executable) | `ocamlearlybird` (default) | `opam install earlybird` | core debugging + rich OCaml locals; no pause/`--run`, no evaluate responses, no fatal-error modal (ocamlearlybird 1.3.6 limitations); single-domain only |
| Go | `dlv` (the [Delve](https://github.com/go-delve/delve) DAP server) | Delve ≥ 1.21 on PATH (`go install github.com/go-delve/delve/cmd/dlv@latest`) | core debugging + goroutine inspection (wait graph, findings) + remote attach (no `--terminal`) |
| PowerShell 7 | `pses` (PowerShell Editor Services, driven by tdb's bundled proxy) | `pwsh` ≥ 7.2 on PATH + the PSES module (see [PowerShell](powershell.md)) | core debugging (breakpoints incl. conditional/hit-count/log, stepping, stack, variables, evaluate console) + `--run`; no `--terminal`, no remote attach; Windows untested |

## Language detection and selection

The language is auto-detected from the debug target:

1. A directory → Go, if it contains any `*.go` file (a Go package
   directory, e.g. `tdb ./pkg`); otherwise an error naming `--lang`.
2. File extension: `.py` → Python; `.go` → Go; `.pl` / `.pm` / `.t` → Perl;
   `.sh` / `.bash` → Bash; `.csh` / `.tcsh` → Tcsh; `.rb` → Ruby; `.ps1` /
   `.psm1` → PowerShell. (`.ml` / `.mli` do *not* auto-select OCaml, see
   point 6.)
3. Native executables (ELF, Mach-O, PE magic bytes) → C/C++/Fortran, unless
   byte-checking finds an OCaml marker (a native binary's
   `caml_program`/`caml_startup` runtime symbols, a bytecode file's
   trailing `Caml1999` marker, or a `#!...ocamlrun` shebang) → OCaml
   (native or bytecode respectively), or a Go buildinfo blob (the marker
   `go version` itself locates, scanned in the first 16MB of the file) →
   Go. A stripped native OCaml binary, or a Go binary whose buildinfo
   blob sits beyond the 16MB scan window, that byte-checking can't
   identify falls back to C/C++/Fortran; force it with `--lang ocaml` or `--lang
   go` respectively. Rust is never inferred from an executable; select it
   explicitly with `--lang rust`.
4. A `#!...python`, `#!...perl`, `#!...bash`, `#!...csh`/`#!...tcsh`,
   `#!...ruby`, or `#!...pwsh` shebang → Python / Perl / Bash / Tcsh / Ruby /
   PowerShell respectively.
5. C/C++/Fortran/Rust *source* files (`.c`, `.cpp`, `.rs`, …) produce an error with a
   hint: compile with debug info (`g++ -g -O0`) and debug the binary.
6. `.ml` / `.mli` (OCaml source) produce an error too: build the project
   first (dune's dev profile keeps debug info) and pass the **built
   executable** to `tdb` (`tdb ./_build/default/bin/main.exe`), never the
   `.ml` file.
7. Anything else produces an error naming the `--lang` override.

`--lang` forces the language; `--adapter` picks a non-default adapter within
it (`tdb --lang cpp --adapter lldb-dap ./myprog`). Rust always requires the
explicit language selection (`tdb --lang rust target/debug/app`).

`--adapter` also accepts a full path to a `gdb`, `lldb-dap`, or `dlv`
executable (`tdb --adapter /opt/llvm/bin/lldb-dap ./myprog`,
`tdb --adapter /usr/bin/gdb-multiarch ./myprog`,
`tdb --adapter ~/go/bin/dlv ./main.go`). The adapter id is taken from the
file's basename — versioned and variant names such as `lldb-dap-21` and
`gdb-multiarch` work — and that exact executable is used for the session,
ahead of anything on `$PATH` or in `config.json`'s `adapters` map.
`tdb --info` shows which `gdb` and `lldb-dap` would be used otherwise.

`gdb`'s DAP mode needs GDB ≥ 14, so `tdb` checks `gdb --version` before
starting a session. A `gdb` that is provably too old is refused with a
message naming its path and version; when that `gdb` came from `PATH`
(not from `--adapter` or `config.json`), `tdb` first looks for a newer one
installed by Red Hat's Software Collections
(`/opt/rh/gcc-toolset-*/root/usr/bin/gdb`, `/opt/rh/devtoolset-*/...`) and
uses the newest that qualifies. See [C/C++/Fortran](c-cpp-fortran.md) for RHEL 8.

> **Migration note:** extensionless Python scripts without a `python` shebang
> were previously assumed to be Python; they now require `--lang python`.

## Adapters are found on `PATH`

`tdb` does not download adapters. If the adapter executable isn't
found, the error names the package to install. To use an adapter from a
non-standard location, or change a language's default adapter, add to
`config.json` (see [Configuration](../reference/configuration.md)):

```json
{
  "adapters": {"lldb-dap": "/opt/llvm/bin/lldb-dap"},
  "default_adapters": {"cpp": "lldb-dap"}
}
```

## What works for non-Python languages

Core debugging works identically for every language: breakpoints (incl.
conditions and persistence), stepping, continue/pause, run-to-cursor, stack
navigation, variable inspection, the evaluate console, syntax highlighting,
and the JSON-RPC / MCP (v2) programmatic modes.

Python-specific features are hidden or return "not supported for this
language" message when debugging other languages: statement-granularity
stepping (non-Python languages step at their debugger's native granularity
— per line, except PowerShell, whose debugger stops per statement, see
[PowerShell](powershell.md)), the async task / process inspectors and wait
graph, the evaluate console's trailing-`?` help,
`--python`/`--pv`, `--no-subprocess`, automatic child-process attachment, and
the post-mortem / `tdb.breakpoint()` hooks (those hooks live inside Python
programs by nature). Remote attach (`-r`) also works for Perl (see
[Perl](perl.md), `Devel::TdbRemote` in place of `debugpy.listen()`), Ruby
(see [Ruby](ruby.md), `rdbg --open` in place of `debugpy.listen()`), Go (see
[Go](go.md), `dlv dap --listen` in place of `debugpy.listen()`), and for
Rust and C/C++/Fortran native binaries (against a gdbserver/lldb-server stub, with
a local symbol-bearing executable; see [Rust](rust.md) — the same flags work
for a C/C++/Fortran target without `--lang`), but not for Bash, Tcsh, OCaml, or
PowerShell. `--terminal` works for every launch-mode
language — Python, Perl, Bash, Tcsh, Ruby, and C/C++/Fortran, OCaml native, or Rust
sessions via `--adapter lldb-dap` — see
[External Terminal Support](../using/external-terminal.md). Go and PowerShell do
not support `--terminal` yet (`dlv dap` and PSES have no terminal-routing
mode). Typing into the Console view to feed the program's stdin works for
Python, Perl, Bash, Tcsh, Ruby, and PowerShell, but not for C/C++/Fortran, Rust,
OCaml, or Go, whose adapters spawn the debuggee themselves — see
[Console Output and Input](../using/console.md).

**Bash limitations (v1):** the bash adapter uses bash's own `DEBUG` trap and
has a smaller feature set than Python:

- Debuggee code that installs its own `DEBUG` trap clobbers the harness;
  debugging silently degrades to free-running.
- No stopping inside subshells `(...)`, `$(...)`, or pipeline segments; they
  execute normally.
- Child bash processes run uninstrumented.
- Outer-frame locals not inspectable (innermost frame only).
- Pause is deferred while blocked in an external command.
- `.sh` files that aren't bash are only diagnosed at launch, by bash itself
  or the harness version check.
- The `DEBUG` trap never fires on a function-definition line, so a
  breakpoint on a `func() {` line never hits; entry and step-in stops land
  on the first executable line of the function body instead.
- The debug control channel occupies two inherited file descriptors
  (typically high-numbered). A script that execs redirections onto those
  exact fds (e.g. `exec 63>&-` or reusing them for its own I/O) will
  silently break debugging.

See [Bash](bash.md) for launch details.

**Tcsh limitations (v1):** tcsh has no debug hooks at all, so the tcsh
adapter debugs an instrumented temporary *copy* of the script (original
paths and line numbers are preserved in everything tdb displays):

- Conditional breakpoints are not supported; a condition set in the
  Breakpoint View is ignored for tcsh (the breakpoint always stops).
- No asynchronous pause: a free-running script stops only at the next
  breakpoint (or when it exits).
- A breakpoint binds to the nearest safe statement at or after the
  requested line; unplaceable breakpoints are reported unverified.
- Stack frames represent `source`d files, not native call frames, and only
  literal `source` targets resolvable at launch are instrumented (computed
  or `cd`-dependent sources run normally but are atomic to the debugger).
- All frames show the same live shell state (Shell Variables, Environment,
  Aliases, Arguments).  Stock tcsh keeps no per-frame history.
- Multiple commands on one physical line (`a ; b`), command substitutions,
  and external commands are atomic stepping units.
- `$0` inside dynamically generated or evaluated text can expose the
  generated copy's path (ordinary lexical `$0` is rewritten correctly).
- Requires Python ≥ 3.11.

See [Tcsh](tcsh.md) for launch details.

**OCaml limitations (v1):** OCaml has two independent adapters with
different, non-overlapping gaps; see [OCaml](ocaml.md) for the full
picture:

- **Native (`lldb-dap`/`gdb`):** the Variables view shows no named OCaml
  locals in an OCaml frame (only a `Registers` scope). This is an
  upstream OCaml/DWARF limitation on stock OCaml 5.4, not a `tdb` bug; use
  the bytecode adapter when you need to inspect locals. The evaluate
  console evaluates lldb/C-level expressions (runtime spelunking), not
  OCaml expressions.
- **Bytecode (`ocamlearlybird`):** single-domain only (no multicore); no
  `pause` while running (so `--run` is unavailable); the evaluate console
  never gets a response for any expression (an `ocamlearlybird` 1.3.6
  limitation); and the fatal-error modal doesn't appear for an uncaught
  exception (`ocamlearlybird` swallows the real exception text before it
  reaches `tdb`). Run the program outside the debugger to see the
  backtrace instead.
- No Windows support, no remote attach, and a stripped native binary that
  byte-checking can't identify needs `--lang ocaml`.
- No stdin forwarding from the Console view (both adapters spawn the
  program themselves); a program that reads stdin needs `--terminal`
  (native, `--adapter lldb-dap`) or input from a file.

**Go limitations (v1):**

- `--terminal` is not supported (`dlv dap` has no terminal-routing mode).
- No stdin forwarding from the Console view: `dlv dap` spawns the program
  itself, so a program that reads stdin must get it from a file or pipe on
  its command line.
- The Goroutines workspace's wait graph shows channel-send/-receive,
  mutex, and `WaitGroup` edges; a goroutine parked in a `select` is
  classified but contributes no wait edge (Delve can't tell which of the
  `select`'s cases it's waiting on).
- No mutex-holder identification: Go's `sync.Mutex` doesn't record an
  owner at the runtime level, so unlike Python's Lock/Semaphore inspector
  the wait graph can name what a goroutine is blocked on but never who
  holds it.
- Goroutines spawned by `exec.Command` (child OS processes) are not
  automatically attached or debugged, unlike Python's automatic
  multiprocessing child attach.
- Detection of a Go binary via buildinfo check scans only the first
  16MB of the file; a huge binary whose buildinfo blob sits beyond that
  falls back to C/C++/Fortran — force it with `--lang go`.
- `-a`/`--attach`'s language auto-detection (reading the target pid's
  buildinfo from `/proc/PID/exe`) is Linux-only; on macOS/Windows pass
  `--lang go` alongside `-a`.

See [Go](go.md) for launch details.

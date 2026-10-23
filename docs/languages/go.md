# Go

`tdb` debugs Go through [Delve](https://github.com/go-delve/delve)'s own
DAP server, `dlv dap` (Delve ≥ 1.21). Install it with:

```bash
go install github.com/go-delve/delve/cmd/dlv@latest
```

`dlv` must be on `PATH`, or point tdb at it with
`{"adapters": {"dlv": "/path/to/dlv"}}` in `config.json`.

**Launch modes** — `tdb` picks the right Delve mode automatically:

```bash
tdb main.go               # source file: `go run`-style debug mode
tdb ./pkg                 # package directory: `go run`-style debug mode
tdb ./binary               # pre-built Go binary (buildinfo-detected): exec mode
tdb --test ./pkg          # Go test package: Delve test mode
tdb -a PID                 # attach to a running local Go process by pid
tdb -r host:port --lang go # attach to a `dlv dap --listen` already running remotely
```

`tdb --test ./pkg` debugs the package's tests under Delve's `test` mode;
pass test-binary flags after `--`, e.g. `tdb --test ./pkg -- -run TestFoo`.
`-a`/`--attach` works for Go and, through gdb/lldb-dap, for C/C++/Fortran, Rust,
and OCaml; on Linux `tdb` identifies the language from `/proc/PID/exe`
without `--lang` (elsewhere pass `--lang go` alongside `-a`). Attaching
stops the process immediately so you get control right away;
`--no-pause-on-attach` leaves it running instead, for a program that is
about to stop itself (see the live breakpoint hook below).

**Entry stop:** Delve's own `stopOnEntry` halts at the process entry point
before any goroutine exists (no stack, no source). `tdb` instead runs to
`main.main`, so the default entry stop lands on your program's first line
with its source on screen; `--no-stop-on-entry` skips it as usual.

**Live breakpoint hook:** the Go counterpart of Python's
`tdb.breakpoint()`. Import the `tdb` module from this repository and call
`tdb.Breakpoint()` where you want the debugger to open:

```go
import "github.com/AlDanial/tdb/go/tdb"

func compute(n int) int {
    total := 0
    for i := 0; i < n; i++ {
        total += i
    }
    tdb.Breakpoint() // tdb opens here, paused on the next line
    return total
}
```

```bash
go get github.com/AlDanial/tdb/go/tdb
go build -gcflags=all=-N -l -o prog . && ./prog   # run it directly, not under tdb
```

When the call is reached, the program starts `tdb --lang go -a <its pid>
--no-pause-on-attach` on its own terminal (the `tdb` on `PATH`, or the one
named by `$TDB`), waits for tdb's Delve to attach, and traps with
`runtime.Breakpoint()`; tdb steps out of the trap so the stop lands on the
line after the call, in your own frame. Later `tdb.Breakpoint()` calls
reuse the running tdb. Quitting tdb (`Ctrl+q`) detaches and the program
runs on; the next call opens a fresh tdb. The call is a no-op when stdin
or stdout is not a terminal, and it warns and continues when `tdb`
cannot be found or exits before attaching. Build with
`-gcflags=all=-N -l` so locals stay inspectable. Linux only: the hook
grants ptrace access to tdb's Delve with `PR_SET_PTRACER` (Yama
`ptrace_scope=1`); elsewhere it warns once and returns. See
`examples/Go/breakpoint_hook_demo/`.

**Remote attach:** start Delve's own DAP server against your program
first —

```bash
dlv dap --listen=host:port ./binary
```

— then connect from `tdb`:

```bash
tdb -r host:port --lang go
```

Do not expose the port to an untrusted network; prefer an SSH tunnel.

**Goroutine inspection:** in a Go session the Threads action (`Alt+T`, or
the **Goroutines (N)** menu label) opens the Goroutines workspace while
the debuggee is stopped — a fresh, bounded snapshot of goroutines, wait
edges, and findings, the same shape as the Rust Concurrency workspace.
Goroutines parked entirely in Go-runtime internals (GC workers,
finalizers, netpoll) are hidden by default; press `a` to reveal them
alongside the rest. The **Wait Graph** tab groups goroutines by the
channel, mutex, or `WaitGroup` they're blocked on; the **Findings** tab
surfaces stuck channels, mutex convoys, and likely leaks with a
confidence level (`confirmed` vs `probable`) — Go mutexes never record an
owner, so, unlike Python's Lock/Semaphore inspector, findings can name
what's blocked but not who holds it. The snapshot collects up to 150
goroutines; a busier program reports the header
`Goroutines (150) — N more not collected` rather than silently truncating.
Refresh (`r`) after the next stop to collect a new snapshot.

**Limitations (v1):** `--terminal` is not supported (`dlv dap` has no
terminal-routing mode); a goroutine parked in a `select` is classified but
contributes no wait edge (Delve can't tell which case it's waiting on);
no mutex-holder identification (impossible for Go's runtime); goroutines
spawned indirectly via `exec.Command` child OS processes are not
automatically attached; buildinfo-based binary detection scans only the
first 16MB of the file (`--lang go` overrides for anything larger); `-a`'s
`/proc`-based language auto-detection is Linux-only (pass `--lang go`
alongside `-a` on macOS/Windows).

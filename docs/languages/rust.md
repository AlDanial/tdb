# Rust

Rust debugging is intentionally explicit: build a normal debug executable,
leave it unmodified, and pass that executable with `--lang rust`. Linux and
macOS are supported; Windows is not yet (MSVC-toolchain binaries carry PDB
debug info that neither `gdb -i dap` nor this release's probes read). `tdb` does
not compile, instrument, or auto-detect Rust programs. Core debugging
(breakpoints, stepping, stacks, variables, remote attach) works with any
rustc that emits debug info; the concurrency inspector's layout-specific
*ownership evidence* additionally requires the current stable Rust
**1.98** standard-library layout — on other versions the workspace still
opens with stack-based wait classification and a compatibility warning.
Build with debug information and no optimization for the most useful
source locations:

```bash
cargo rustc -- -C debuginfo=2 -C opt-level=0
# Equivalent direct rustc settings: rustc -C debuginfo=2 -C opt-level=0 src/main.rs
```

As for C/C++/Fortran, the Console view cannot feed the program's stdin (the gdb and
lldb adapters spawn it themselves); use `--terminal` with `--adapter
lldb-dap` for interactive programs. A panic that terminates the debuggee opens the error modal with the parsed
panic message and backtrace; `tdb` injects `RUST_BACKTRACE=1` into launched
programs (your own value wins if you set one) so the frames are present.

The common commands are:

```bash
cargo build
tdb --lang rust target/debug/app
tdb --lang rust --adapter lldb-dap --run target/debug/app
tdb --lang rust --adapter lldb-dap --terminal xterm target/debug/app
tdb --lang rust --adapter gdb --remote-attach host:2345 target/debug/app
```

`--run` and normal stopped-on-entry launch work with both adapters. On Linux,
use GDB (the default) or `lldb-dap`; on macOS, use `lldb-dap` (the default).
External terminals are LLDB-only: GDB's DAP mode cannot provide terminal
integration. The mode/platform matrix is:

| Platform | Normal / `--run` | `--terminal` | `--remote-attach` |
|----------|------------------|--------------|-------------------|
| Linux | GDB or `lldb-dap` | `lldb-dap` only | GDB or `lldb-dap` |
| macOS | `lldb-dap` | `lldb-dap` only | `lldb-dap` |

Remote attach connects to a GDB-remote server already running on the target.
Pass the matching **local, unmodified executable with debug symbols** as the
program argument; `tdb` requires it for symbols and Rust concurrency evidence.
When source paths differ, pair `--local-root` with `--remote-root`. Do not
expose the remote debug port to an untrusted network: prefer an SSH tunnel,
for example `ssh -L 2345:127.0.0.1:2345 host`, then attach to `127.0.0.1:2345`.

**Attach to a running process:** `tdb --lang rust -a PID` attaches gdb (or
`--adapter lldb-dap`) to a live Rust process and stops it; tdb loads
symbols from `/proc/PID/exe`, or from an executable path you pass as well.
Without `--lang`, tdb recognizes a non-stripped Rust binary by its runtime
symbols; a stripped one is debugged as C/C++/Fortran.

**Live breakpoint hook:** the Rust counterpart of Python's
[`tdb.breakpoint()`](../python/breakpoint-hook.md). Depend on the `tdb` crate from
this repository (it has no dependencies) and call `tdb::breakpoint()`:

```toml
[dependencies]
tdb = { git = "https://github.com/AlDanial/tdb" }   # or path = ".../rust/tdb"
```

```rust
fn compute(n: u64) -> u64 {
    let total: u64 = (0..n).sum();
    tdb::breakpoint(); // tdb opens here
    total
}
```

```bash
cargo build && ./target/debug/prog    # dev profile; run it directly, not under tdb
```

When the call is reached, the program starts `tdb --lang rust -a <its pid>
--no-pause-on-attach` on its own terminal (the `tdb` on `PATH`, or the one
named by `$TDB`), waits for tdb's gdb or lldb to attach, and stops in the
hook; tdb steps out so the stop lands in your own frame on, or just after,
the call line, as the compiler's line table attributes the return address.
Later calls reuse the running tdb. Quitting tdb (`Ctrl+q`)
detaches and the program runs on; the next call opens a fresh tdb. The
call is a no-op when stdin or stdout is not a terminal, and it warns and
continues when `tdb` cannot be found, exits before attaching, or cannot
attach within 60 s (Yama `ptrace_scope` 2 or 3, or a container without
`SYS_PTRACE`, blocks attaching). If a tdb whose attach failed is still
running, later calls warn once and return rather than open another tdb
over it. Use the dev profile so locals stay
inspectable. Linux only: the hook grants ptrace access to tdb's debugger
with `PR_SET_PTRACER`; elsewhere it warns once and returns. See
`examples/Rust/breakpoint_hook_demo/`.

In a Rust session the Threads action (`Alt+T`, or the Threads menu entry)
opens the Rust Concurrency workspace while the debuggee is stopped — a
fresh, bounded snapshot of threads, wait edges, and findings. The graph is
best effort: `confirmed` evidence is directly observed by the debugger,
`probable` is a supported inference, and `unknown` means ownership could not
be established. A deadlock is confirmed only when every edge in its cycle is
confirmed. A suspected cycle or whole-program stall is a diagnostic lead, not
a proof: it means a cycle has incomplete evidence, or all observed application
threads are blocked while the graph cannot safely invent an owner. Refresh
after the next stop to collect a new snapshot.

The built-in evidence recognizes current `std` synchronization layouts. A
future helper crate may expose stable, application-provided synchronization
metadata for broader primitives and stronger ownership evidence.

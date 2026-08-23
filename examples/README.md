threading: Use for I/O bound tasks. Easy to write, but you must use Locks to prevent data corruption. Limited by the GIL (only 1 CPU core used).

multiprocessing: Use for CPU bound tasks. Bypasses the GIL, uses multiple CPU cores. Heavy memory footprint because it copies the entire Python environment.

asyncio: Use for massive I/O bound tasks (networking). Extremely lightweight, no Locks required (mostly), but requires writing code in an entirely different paradigm (async/await), and blocking code (time.sleep) ruins the entire system.

## Rust debugging

Build a normal debug executable and pass that already-built, unmodified file
explicitly to `tdb`; Rust executable auto-detection is intentionally disabled:

```bash
cargo rustc -- -C debuginfo=2 -C opt-level=0
# Equivalent direct rustc settings: rustc -C debuginfo=2 -C opt-level=0 src/main.rs
tdb --lang rust target/debug/app
tdb --lang rust --adapter lldb-dap --run target/debug/app
tdb --lang rust --adapter lldb-dap --terminal xterm target/debug/app
tdb --lang rust --adapter gdb --remote-attach host:2345 target/debug/app
```

Rust 1.98 is the supported current-stable layout. Linux supports GDB and
`lldb-dap`; macOS uses `lldb-dap`. `--terminal` requires `lldb-dap`. Remote
attach requires the matching local executable with debug symbols; use an SSH
tunnel rather than exposing the debug-server port. The Rust Concurrency view
is a best-effort stopped-state snapshot: confirmed evidence is directly
observed, probable evidence is inferred, and unknown evidence is incomplete.
Suspected cycles and whole-program stalls are leads to investigate, not proofs.

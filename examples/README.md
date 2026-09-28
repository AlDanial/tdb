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

Core debugging works with any rustc that emits debug info; the concurrency
inspector's ownership evidence requires the stable Rust 1.98 standard-library
layout (other versions degrade to stack-based classification with a
warning). Linux supports GDB and
`lldb-dap`; macOS uses `lldb-dap`. `--terminal` requires `lldb-dap`. Remote
attach requires the matching local executable with debug symbols; use an SSH
tunnel rather than exposing the debug-server port. The Rust Concurrency view
is a best-effort stopped-state snapshot: confirmed evidence is directly
observed, probable evidence is inferred, and unknown evidence is incomplete.
Suspected cycles and whole-program stalls are leads to investigate, not proofs.

## Live breakpoint hook demos (C, C++, Rust, OCaml)

Each demo sums `0..9` into a local `total` alongside a five-element
`local_list`, then calls the language's live breakpoint hook. Run each
directly, not under `tdb`. C, C++, and Rust stop in `tdb` on the line
after the hook call, with `total` and `local_list` visible in the
Variables view. OCaml stops in `tdb` on the `Tdb.breakpoint ()` line
itself, and ocamlopt's native code emits no debug info for locals, so
`total` and `local_list` don't appear there (globals and the stack still
do).

**C:**

```bash
gcc -g -O0 -I "$(tdb --info | awk -F': ' '/tdb.h dir/ {print $2}')" \
    -o demo breakpoint_hook_demo.c && ./demo
```

**C++:**

```bash
g++ -g -O0 -I "$(tdb --info | awk -F': ' '/tdb.h dir/ {print $2}')" \
    -o demo breakpoint_hook_demo.cpp && ./demo
```

**Rust:**

```bash
cd examples/Rust/breakpoint_hook_demo
cargo run
```

**OCaml:**

```bash
cp ../../ocaml/tdb/{tdb.ml,tdb.mli,tdb_stubs.c,tdb.h} .
ocamlopt -g -o demo tdb_stubs.c tdb.mli tdb.ml breakpoint_hook_demo.ml && ./demo
```

# OCaml

`tdb` debugs OCaml through two independent adapters, chosen automatically
from how the executable was built:

- **Native** (`ocamlopt`/dune's default build) → **`lldb-dap`** (default;
  `--adapter gdb` also works). OCaml 5's domains show up as threads.
- **Bytecode** (`ocamlc -g`) → **`ocamlearlybird`** (`opam install
  earlybird`). Single-domain only, but with rich, real OCaml locals.

**Build requirement:** compile with `-g` (dune's dev profile already
does). Always pass the **built executable** to `tdb`, never the `.ml`
source, as in `tdb ./_build/default/bin/main.exe`, not `tdb main.ml` (which
errors with this exact guidance).

Earlybird additionally as a fragile relative path matching so for
best results provide a fully quallified path to the source files.

```
ocamlc -g -o my_program.byte "$PWD/my_program.ml"
```

Then, to ensure transition from bytecode to OCaml source in the
Code view, provide an explicit temporary breakpoint (`-t`)
at the first line of execution:

```
tdb --lang ocaml --adapter ocamlearlybird -t "$PWD/my_program.ml:2" ./my_program.byte
```

**OCaml version:** tdb requires **OCaml ≥ 4.12**. Older runtimes print
`Printexc` backtraces without function names, which the fatal-error
modal's parser does not understand (frames would be dropped and the
modal would wrongly suggest recompiling with `-g`).

With native GDB, the default entry stop is translated to the first
executable line in project OCaml source; the generated C/ELF startup code is
not shown.
Unfortunately OCaml's native debug information does not always identify
tihs line correctly.
`--no-stop-on-entry` and explicit CLI breakpoints retain their
usual behavior.

A stripped native binary that byte-checking can't identify as OCaml falls
back to C/C++/Fortran; force it with `--lang ocaml`. Override the adapter or point
at a non-`PATH` install in `config.json` (see
[Configuration](../reference/configuration.md)):

```json
{
  "adapters": {"ocamlearlybird": "/path/to/ocamlearlybird"},
  "default_adapters": {"ocaml": "gdb"}
}
```

**Domains as threads (native only):** OCaml 5's domains are presented in
the Threads modal (`Alt+T` or the **Threads (N)** menu label) as `Domain 0
(main)`, `Domain 1`, and so on, in creation order. The runtime's internal
"backup thread" (one per domain, used for I/O blocking) is hidden by
default since it's never running user code; press `a` in the Threads
modal to reveal it alongside the domains. Breakpoints, stepping, continue,
and pause all work normally against any domain. In both the Stack view
and the Threads modal, OCaml frame names are demangled from the
runtime's raw `camlModule__name_NNN` convention into readable
`Module.name` form (e.g. `camlOcaml_domains.worker_297` displays as
`Ocaml_domains.worker`); runtime C frames (`caml_start_program`,
`caml_callback_exn`, and the like) are shown as-is, unchanged.

**Variables view (native):** stock OCaml 5.4 native DWARF shows **no
named locals**. The `scopes`/`variables` round trip succeeds, but every
OCaml frame's `Locals` and `Globals` scopes come back empty; only a
`Registers` scope has data (the raw register file, not decoded OCaml
values). This is an upstream OCaml compiler/DWARF limitation on the
verified toolchain (OCaml 5.4.0 / lldb 21), not a `tdb` bug. If you need
to inspect local variables, use the bytecode adapter instead.

**Variables view (bytecode):** `ocamlearlybird` reports real local names
and values, exactly like Python locals.

**Evaluate console:** the two adapters give fundamentally different
consoles. Native (`lldb-dap`) evaluates **lldb/C-level expressions**.
This is runtime spelunking (registers, raw memory, C symbol names), not
OCaml expression evaluation. Bytecode (`ocamlearlybird`) is meant to
evaluate real OCaml expressions in scope, but in the verified
`ocamlearlybird` 1.3.6 release its `evaluate` request never responds (a
confirmed upstream limitation, not specific to `tdb`); the console
silently returns nothing for any expression.

**Uncaught exceptions:** native sessions set a breakpoint on
`caml_fatal_uncaught_exception` so the debugger stops there, and `tdb`
also parses the `OCAMLRUNPARAM=b` backtrace it injects into the debuggee's
environment once the process exits, opening the same error modal Python
tracebacks get. Bytecode sessions do **not** get this: `ocamlearlybird`
intercepts the uncaught exception itself and only reports a generic
"Program exited due to Uncaught_exc" message, so the real exception text
never reaches `tdb`. Run the program outside the debugger
(`OCAMLRUNPARAM=b ./main.byte`) to see its backtrace.

**Limitations (v1):** no Windows support; no remote attach; bytecode
sessions can't `pause` a running program (so `--run` is unavailable for
bytecode. Native sessions support `--run` normally); `Unix.fork` and Eio
fibers are out of scope (only OCaml 5 domains are presented as threads).

**Attach to a running process:** `tdb -a PID` attaches lldb-dap (or
`--adapter gdb`) to a live native OCaml process and stops it; tdb
recognizes the OCaml runtime in `/proc/PID/exe` and always picks a native
adapter (bytecode programs cannot be attached to). `--no-pause-on-attach`
leaves the process running, for a program about to stop itself.

**Live breakpoint hook:** the OCaml counterpart of Python's
[`tdb.breakpoint()`](../python/breakpoint-hook.md), for native code. Vendor the
`ocaml/tdb` directory from this repository (or `opam pin` it), add
`(libraries tdb)` to your executable's dune stanza, and call
`Tdb.breakpoint ()`:

```ocaml
let compute n =
  let total = ref 0 in
  for i = 0 to n - 1 do total := !total + i done;
  Tdb.breakpoint ();   (* tdb opens paused on this line *)
  !total
```

```bash
dune build && ./_build/default/prog.exe   # dev profile; run it directly, not under tdb
```

Without dune: copy `tdb.ml`, `tdb.mli`, `tdb_stubs.c`, and `tdb.h` from
`ocaml/tdb` next to your source and build with
`ocamlopt -g -o prog tdb_stubs.c tdb.mli tdb.ml prog.ml`.

When the call is reached, the program starts `tdb --lang ocaml -a <its pid>
--no-pause-on-attach` on its own terminal (the `tdb` on `PATH`, or the one
named by `$TDB`), waits for tdb's lldb or gdb to attach, and stops in the
hook, and tdb steps out of the hook's frames into your own. Unlike the
other languages' hooks, the stop lands on the `Tdb.breakpoint ()` call
line itself rather than the line after: both lldb and gdb map the hook's
return address back to the call. ocamlopt also emits no debug info
for locals, so the Variables view shows none for OCaml frames (globals
and the stack are still available). Later calls reuse the running tdb.
Quitting tdb (`Ctrl+q`) detaches and the program runs on; the next call
opens a fresh tdb. The call is a no-op when stdin or stdout is not a
terminal, and it warns and continues when `tdb` cannot be found, exits
before attaching, or cannot attach within 60 s (Yama `ptrace_scope` 2 or
3, or a container without `SYS_PTRACE`, blocks attaching). Keep debug
info (`-g`, dune's dev profile). Linux only: the hook grants ptrace
access to tdb's debugger with `PR_SET_PTRACER`; elsewhere it warns once
and returns. See `examples/OCaml/breakpoint_hook_demo.ml`.

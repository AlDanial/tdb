# C, C++, and Fortran

Tips for debugging native C, C++, and Fortran executables with `gdb` or `lldb-dap`.

- Compile with `-g` (ideally `-g -O0` or `-g3 -O0`).
  If no breakpoint in a file can be
  bound, `tdb` prints a console warning suggesting the program may lack
  debug info.
- The Console view's stdin line is not available: `gdb -i dap` and
  `lldb-dap` spawn the program themselves, so `tdb` has no handle on its
  stdin. A program that reads stdin needs `--terminal` (with
  `--adapter lldb-dap`) or its input from a file.
- Stack frames pointing into system libraries often have no source on disk;
  the Code View shows a `<Could not read …>` placeholder while the stack,
  variables, and evaluate console remain fully usable.
- For compiled executables, the Code View shows shows a brief note while
  it attempts to load the associated source file.  The note will identify
  the failure reason if the source cannot be loaded (executable was built
  without `-g`, or the adapter is not working, e.g. a GDB too old for DAP).
- GDB (the default adapter) has the most complete libstdc++
  pretty-printing. `lldb-dap` (via `--adapter lldb-dap`) also debugs
  GCC-built binaries fine.  DWARF is compiler-neutral.
- **STL containers in the Variables view.** A distro `gdb` shows
  `std::vector<Result>` as `std::vector of length 2, capacity 2` with
  `[0]`, `[1]` children because gcc installs libstdc++'s pretty-printers
  next to it and `gdb` auto-loads them. A `gdb` built from source into
  its own prefix never finds that auto-load script and shows the same
  variable as `_M_impl` / `_M_start` pointer soup. `tdb` sources a small
  helper into every `gdb` session that, on the first `std::` value it
  sees, looks for gcc's printers (the distro auto-load script for the
  loaded `libstdc++.so`, `share/gcc*/python` near that library or near
  gdb's data directory, then the usual distro, Red Hat toolset, Homebrew
  and MacPorts locations) and uses them; when none are installed it
  falls back to its own bundled printers for `std::string`, `vector`,
  `deque`, `list`, `map`/`set` (and multi-), `unordered_map`/`set`,
  `unique_ptr`, `shared_ptr`/`weak_ptr` and `optional`. Printers gdb
  auto-loads itself or that your `.gdbinit` registers still take
  precedence. The helper also shields gdb's DAP layer from gcc 15's map
  and set printers, whose child count is broken there (expanding a
  `std::map` fails with "name 'self' is not defined" on an unpatched
  distro gdb). `TDB_GDB_STL_PRINTERS` tunes it: `off` registers nothing,
  `bundled` forces the bundled printers (and drops gdb's auto-loaded
  ones), a directory holding `libstdcxx/` names gcc's printers
  explicitly. Type `tdb-stl-printers` in the evaluate console to see
  which printers are in use.
- **GDB evaluate-console quirk:** GDB's DAP treats REPL input as CLI
  commands, so evaluate expressions with an explicit `print`, e.g.
  `print x` rather than bare `x` (bare `x` collides with GDB's
  examine-memory command). `lldb-dap` evaluates bare expressions directly.
- `--terminal` (see [External Terminal Support](../using/external-terminal.md))
  requires `--adapter lldb-dap`; GDB's DAP mode has no terminal integration
  and `tdb` refuses `--terminal` with the default `gdb` adapter.
- **GDB source-location quirk:** some GDB builds (RHEL 8 among them)
  report no source file for a stopped frame -- `info source` answers
  "No current source file" -- until a `list` command has selected the
  default source file. `tdb` therefore sends `list` once, before its
  first stack query, so the Code View can find the source. This is why a
  fresh gdb session shows `Current source file is …` in `info source`
  from the evaluate console without you having typed `list`.
- **RHEL 8 (and 9) gdb is too old for DAP:** the stock `/usr/bin/gdb` is
  8.2 on RHEL 8 (10.2 on RHEL 9) and answers `gdb -i dap` with
  ``Interpreter `dap' unrecognized``. Install a newer one
  (minimum GCC Toolset version is 14) from the
  GCC Toolset, which leaves `/usr/bin/gdb` alone:

  ```bash
  sudo dnf install gcc-toolset-14-gdb   # gdb 14.2 under /opt/rh/gcc-toolset-14
  ```

  `tdb` then finds `/opt/rh/gcc-toolset-14/root/usr/bin/gdb` by itself
  whenever the `gdb` on `PATH` is missing or too old — no `scl enable`
  needed (`tdb --info` shows it as `gdb used instead`). Two things pin
  the old one and must be changed by hand: `--adapter /usr/bin/gdb`, and
  an `"adapters": {"gdb": "/usr/bin/gdb"}` entry in `config.json` (`tdb`
  seeds that entry with whatever `gdb` was on `PATH` the first time it
  wrote the file). Either delete the entry or point it at the toolset
  gdb. A GDB ≥ 14 that was built without Python support gives the same
  ``Interpreter `dap' unrecognized`` error (`gdb --configuration` shows
  `--without-python`); `tdb --info` probes for this and reports
  `GDB LACKS DAP CAPABILITY AND CANNOT BE USED` in its `DAP interpreter`
  row when the gdb it found rejects `-i dap`. Use `--adapter lldb-dap`
  or a gdb built with
  Python.

**Attach to a running process:** `tdb -a PID` attaches gdb (or `--adapter
lldb-dap`) to a live native process and stops it. On Linux tdb reads
`/proc/PID/exe` to identify the language (Go, OCaml, Rust, otherwise
C/C++/Fortran) and to load symbols; pass the executable path as well to use a
different symbol file, and pass `--lang` off Linux. `--no-pause-on-attach`
leaves the process running, for a program about to stop itself. An
interpreter process (python3, perl, ruby, bash) is attached as C/C++/Fortran; for
Python, Perl, and Ruby programs use `--remote-attach` instead.

**Live breakpoint hook:** the C/C++/Fortran counterpart of Python's
[`tdb.breakpoint()`](../python/breakpoint-hook.md). Include `tdb.h` (its directory
is the `tdb.h dir` row of `tdb --info`) and call `tdb_breakpoint()`:

```c
#include "tdb.h"

int compute(int n) {
    int total = 0;
    for (int i = 0; i < n; i++) total += i;
    tdb_breakpoint();   /* tdb opens here, paused on the next line */
    return total;
}
```

```bash
gcc -g -O0 -I "$(tdb --info | awk -F': ' '/tdb.h dir/ {print $2}')" -o prog prog.c
./prog    # run it directly, not under tdb
```

When the call is reached, the program starts `tdb --lang cpp -a <its pid>
--no-pause-on-attach` on its own terminal (the `tdb` on `PATH`, or the one
named by `$TDB`), waits for tdb's gdb or lldb to attach, and stops in the
hook; tdb steps out so the stop lands on the line after the call, in your
own frame. Later calls reuse the running tdb. Quitting tdb (`Ctrl+q`)
detaches and the program runs on; the next call opens a fresh tdb. The
call is a no-op when stdin or stdout is not a terminal, and it warns and
continues when `tdb` cannot be found, exits before attaching, or cannot
attach within 60 s (Yama `ptrace_scope` 2 or 3, or a container without
`SYS_PTRACE`, blocks attaching). If a tdb whose attach failed is still
running, later calls warn once and return rather than open another tdb
over it. Build with `-g -O0` so locals stay
inspectable; a stripped binary attaches but never stops, because the hook's
stop symbol cannot be resolved. The header is C99 and C++ compatible and
header-only. Linux only: the hook grants ptrace access to tdb's debugger
with `PR_SET_PTRACER`; elsewhere it warns once and returns. See
`examples/C/breakpoint_hook_demo.c` and `examples/C++/breakpoint_hook_demo.cpp`.

Fortran (or any language that can call C) uses the same header: compile
`tdb.h` as C into an object that defines `tdb_breakpoint()`, declare it with
`bind(C)`, and link the object in:

```fortran
interface
    subroutine tdb_breakpoint() bind(C, name="tdb_breakpoint")
    end subroutine tdb_breakpoint
end interface
...
call tdb_breakpoint()   ! tdb opens here, paused on the next line
```

```bash
gcc -g -O0 -c -x c "$(tdb --info | awk -F': ' '/tdb.h dir/ {print $2}')/tdb.h" -o tdb.o
gfortran -g -O0 -o prog prog.f90 tdb.o
./prog    # run it directly, not under tdb
```

Use gdb (the default) for Fortran: lldb-dap stops on the right line but,
lacking Fortran support, shows no Fortran locals. See
`examples/Fortran/breakpoint_hook_demo.f90`.

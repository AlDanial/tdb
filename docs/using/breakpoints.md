# Breakpoints

Click the gutter in the Code View to toggle a breakpoint, or press `b` in Debug mode.

**Breakpoint indicators:**
- Red dot: active breakpoint
- Yellow dot: conditional breakpoint
- Blue dot: disabled breakpoint

**Conditional breakpoints:** Double-click a breakpoint to open the condition editor.
Set a Python expression (e.g., `x > 10`) and/or a hit count (pause on the Nth hit).

**Breakpoint View actions:**
- `D` : Disable / enable all breakpoints
- `C` : Clear all breakpoints

Breakpoints persist across session restarts.

**Breakpoints set at the debugger's own prompt (gdb):** when debugging native code
with `gdb`, commands typed in the Evaluate Console such as `b 83` or `delete 2`
go straight to gdb. Switching to the Breakpoint View (`Ctrl+B`, or clicking it)
synchronizes tdb's table with gdb's list: breakpoints created at the gdb prompt
are adopted (with their conditions and enabled state) and become ordinary tdb
breakpoints, and breakpoints deleted at the prompt are dropped. Watchpoints,
catchpoints and `tbreak` temporaries are left to gdb. `lldb-dap` has no such
prompt-level breakpoint channel, so nothing to sync there.

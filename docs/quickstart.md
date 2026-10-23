# Quick Start

```bash
# show comprehensive documentation in a terminal-based Markdown viewer
tdb --doc

# show the Help > About text, where tdb and its config, breakpoints and
# log files live, and the path + version of each interpreter and native
# debugger it would use (python, gdb, lldb-dap, perl, ruby, dlv,
# ocamlearlybird, bash, tcsh, pwsh), whether gdb has its DAP interpreter
# (`gdb -i dap`), whether perl has ExtUtils::MakeMaker (needed to
# build the bundled PadWalker), and the `examples dir` holding the
# per-language examples (C, C++, Fortran, Go, OCaml, Perl, PowerShell,
# Python, Ruby, Rust, bash, tcsh) installed with the package
tdb --info

# reset config.json to defaults (the old file is kept as
# config.json-YYYY-mm-DD-HH:MM:SS; HH.MM.SS on Windows)
tdb --config write-default

# build and cache the bundled PadWalker Perl module now rather than
# on the first Perl launch
tdb --config install-padwalker

# debug a Python program (stops at first line by default)
tdb my_program.py

# debug with arguments
tdb my_program.py arg1 arg2

# debug a C/C++ (or other native) executable built with -g.  The ELF/Mach-O/PE
# binary is auto-detected and debugged through GDB's DAP mode (GDB >= 14)
tdb ./myprog arg1 arg2

# same, but using lldb-dap (LLVM >= 17) instead of gdb
tdb --adapter lldb-dap ./myprog

# force the language when auto-detection can't tell (e.g. an extensionless script)
tdb --lang python ./mytool

# add breakpoints at lines 20 and 35 of `my_program.py` and line 14
# of `module.py` (when -k is given, --no-stop-on-entry is set and the
# program runs to the first breakpoint)
tdb -k 20 -k 35 -k module.py:14 my_program.py arg1 arg2

# run straight to line 20 without saving the breakpoint for future
# sessions (-t is -k minus the persistence)
tdb -t 20 my_program.py

# no TUI: run at (almost) full speed, printing nr each time line 25 is
# reached, then continuing (-e may be repeated; see "Eval Mode")
tdb --eval my_program.py:25 "print(f'{nr=}')" my_program.py

# use a specific virtualenv
tdb --python /path/to/venv/bin/python my_program.py

# step into, or stop at tracebacks in library code
tdb --no-just-my-code --python /path/to/venv/bin/python my_program.py

# run until first breakpoint (persisted during a prior run) or exit
tdb --no-stop-on-entry my_program.py

# run the debuggee in an external terminal
tdb --terminal xterm my_program.py

# attach to a remote Python program that has a debugpy server on port 5678
# (source code is automatically downloaded from the remote host)
tdb -r remotehost:5678

# attach to a remote Python program that has a debugpy server on port 5678
# and set a breakpoint where tdb and the remote program have the same
# source code layout
tdb -r remotehost:5678  -k my_program.py:42

# attach to a remote Python program that has a debugpy server on port 5678
# and set a breakpoint where code on the local host is at a different location
# than code on the remote host
tdb -r remotehost:5678 --local-root /my/code/dir --remote-root /app -k my_program.py:42

# attach to a program that pauses itself (debugpy.breakpoint() right after
# debugpy.wait_for_client()); without this flag tdb pauses the program on
# attach, which can leave it suspended after tdb quits
tdb -r remotehost:5678 --no-pause-on-attach

# separate tdb arguments from debuggee arguments with `--`
tdb --python /path/to/venv/bin/python -- my_program.py -k 17 --max 23.3
```

Alternatively, use the module entry point:

```bash
python -m tdb my_program.py
```

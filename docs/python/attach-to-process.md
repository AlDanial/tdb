# Attach to a Running Program

`tdb -a PID` attaches to a Python program that is already running. The program
needs no preparation: it doesn't have to import `debugpy`, or even have it
installed.

```bash
tdb -a 12345                      # Linux finds the language from the pid
tdb --lang python -a 12345        # needed on macOS and Windows
tdb -a 12345 --no-pause-on-attach # attach without pausing the program
```

tdb pauses the program on attach. Quitting tdb leaves it running, with no open
port or helper process behind, and you can attach again later.

## Requirements

- CPython 3.11 or newer.
- Permission to debug the process (see below).
- On Linux, `gdb` for Python below 3.14 and for programs whose main thread is
  blocked. Python 3.14 attaches without it when the program is running Python
  code.

On macOS and Windows, pass `--python /path/to/python` to name the program's
interpreter if you want tdb to try the Python 3.14 method first. Attaching on
macOS generally needs root.

## How tdb reaches the program

Python 3.14 can run a script in another process with `sys.remote_exec`, which is
the safest method. It only runs when the main thread next executes Python code,
so it can't reach a program blocked in a system call. tdb waits 5 seconds for it,
then falls back to debugpy's own injection (gdb on Linux, lldb on macOS, DLL
injection on Windows). Python 3.11 to 3.13 use that method directly. A
notification says which method was used.

gdb briefly stops the whole program at an arbitrary point, which is less safe
than `sys.remote_exec`. It is gone from the process before your session starts.

## Blocked and idle programs

A hung program or idle server may be blocked in a system call, with no Python
code running. tdb still attaches, but a pause can't land: the status stays
Running and a notification explains why. Breakpoints fire as soon as the program
next runs Python, and threads that are running Python can still be paused.

If native code holds the GIL, injection can't finish. tdb gives up after 30
seconds and says so, but it leaves the injection running rather than risk
crashing the program: its helper ends by itself once the GIL is released, and
the program may then print a harmless connection error to its stderr. Quitting
during injection works the same way. If the program has already connected, tdb
first finishes the handshake, which can delay quitting by a few seconds.

## Permission denied

Most Linux distributions restrict debugging to a process's own children with
`kernel.yama.ptrace_scope=1`, and this affects `pdb -p` too. tdb doesn't change
system settings or use `sudo`; it explains the problem and exits. Either allow
debugging of your own processes until the next reboot:

```bash
sudo sysctl kernel.yama.ptrace_scope=0
```

or run tdb as root. A setting of 3 can only be lowered by a reboot.

## Programs already using debugpy

Injecting into a program that is already being debugged with debugpy would end
that session, so tdb refuses on Linux. If the program called `debugpy.listen()`,
connect to it with [`tdb -r PORT`](../using/remote-attach.md) instead. Other
platforms can't detect this up front: tdb exits with a message if the program
stops answering after injection.

## Limitations

- Console View shows output the program writes through Python's `sys.stdout`
  and `sys.stderr`. The program keeps its own stdin, so Console input is off.
- Child processes of the attached program aren't followed.
- Restart is unavailable, because tdb didn't start the program.
- Code that already ran can't be stepped through. Breakpoints apply from now on,
  including in functions that are already running.

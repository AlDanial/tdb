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
- On Alpine and other musl systems, a main thread that is running Python code:
  debugpy's injection library needs glibc, so tdb can't reach a blocked one.

On macOS and Windows, pass `--python /path/to/python` to name the program's
interpreter if you want tdb to try the Python 3.14 method first. Attaching on
macOS generally needs root.

## How tdb reaches the program

Python 3.14 can run a script in another process with `sys.remote_exec`, which is
the safest method. It only runs when the main thread next executes Python code,
so it can't reach a program blocked in a system call. On Linux, Python 3.11 to
3.13 get the same effect through gdb, which queues the script with
`Py_AddPendingCall` and leaves at once. tdb waits 5 seconds for either, then
falls back to debugpy's own injection (gdb on Linux, lldb on macOS, DLL injection
on Windows). A notification says which method was used.

gdb briefly stops the whole program at an arbitrary point, which is less safe
than `sys.remote_exec`. It is gone from the process before your session starts.
On Linux below glibc 2.41, debugpy's injection can deadlock a program whose main
thread is waiting for the GIL, so tdb tries it last. That takes native code
holding the GIL for over 5 seconds.

tdb's adapter chooses a local port and waits there for the injected code to
connect back, and tdb injects only once it is listening. The two sides then
prove to each other that they hold a key made for this attach, without sending
it, before any debugging starts. So nothing else on the machine can pose as your
program, and your program won't talk to anything that took the port after tdb
left. The key stays off command lines.

Programs your program starts, or forks, are not debugged: a breakpoint doesn't
stop them. If the attach never finishes, because tdb died during it for
instance, the injected code lets the program go on after 65 seconds.

## Running tdb as root

Attaching as root to another user's program is safe only if you trust that user
as far as gdb's own risks go: gdb reads the program's files as root. tdb hands its
temporary files to that user, and never runs the program's binary unless root
alone could have changed it. The Python 3.14 method then needs an interpreter
installed by root; otherwise tdb uses gdb.

That user must be able to read tdb's copy of debugpy, so a tdb installed in a
private home directory can't attach to their programs, and tdb says so at once.
Pass `--python` to name an interpreter, and tdb will run it as root.

## Limits

- A program with a private `/tmp`, or in a container, can't see tdb's temporary
  files. Set `TMPDIR` to a directory it shares with tdb.
- As root, tdb refuses a temporary directory that another user could change. A
  sticky directory owned by root, like `/tmp` or `/var/tmp`, is fine.
- Below Python 3.14, a program that is at its recursion limit or out of memory
  when it runs tdb's queued code exits with a `SystemError`. Python gives tdb no
  way to catch that.
- If one of the program's signal handlers raises an exception while tdb attaches,
  such as `sys.exit` on SIGTERM, tdb's queued code absorbs it, so send the signal
  again.
- A local process that floods the adapter's port during the attach can make the
  attach fail, though it can't take your program's place or affect the program.
- Only one attach to a program can run at a time. A second one started during
  the first fails with "did not connect" and leaves the first alone.
- On Alpine and other musl systems with Python 3.11, if the attach never finishes
  the program stays traced, and slower, until it exits.
- A program tdb has attached to keeps one small fork callback, and a
  `sys._tdb_attach_fork_hooks` list, until it exits. Python can't unregister a
  fork callback. Attaching again adds nothing. The callback is what keeps forked
  children out of the debugger.

## Blocked and idle programs

A hung program or idle server may be blocked in a system call, with no Python
code running. tdb still attaches, but a pause can't land: the status stays
Running and a notification explains why. Breakpoints fire as soon as the program
next runs Python, and threads that are running Python can still be paused.

If native code holds the GIL, injection can't finish. tdb gives up after 30
seconds and says so, but it leaves the injection running rather than risk
crashing the program: its helper ends by itself once the GIL is released. The
program may then print debugpy's connection error to its stderr. Quitting during
injection works the same way. If the program has already connected, tdb
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

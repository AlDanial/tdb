"""Runs `debugpy --pid`, moving the injected code onto its own thread.

gdb stays inside the target for as long as the injected call lasts. debugpy's
attach spends that time importing, and `debugpy/common/log.py` runs `uname`
through `platform.platform()`. gdb leaves the other threads stopped after that
vfork, so a program with another thread wanting the GIL hangs for good. Here
the call only starts a thread and returns, so gdb is gone in moments and the
slow part runs with no debugger in the process.

Run as a script, with the marker path followed by debugpy's arguments, so
that it needs nothing from tdb. The rest is imported by tdb itself.
"""

import os
import sys

# Far below the 5 ms default: with every thread busy, each file the imports
# touch otherwise costs a full turn around them, and attaching takes minutes.
ATTACH_SWITCH_INTERVAL = 0.0001
# Longer than tdb waits, for an attach that never returns: pydevd waits for
# the handshake with no timeout, and a program shouldn't stay this way.
SWITCH_INTERVAL_LIMIT = 45.0

# A signal reaching the target during one of gdb's calls makes gdb abandon the
# call and detach with its frame still on the stack, and the program then
# segfaults. Passing signals straight through avoids that for all but SIGINT
# and SIGTRAP, which the second setting covers by unwinding the call.
GDB_SIGNAL_COMMANDS = ("handle all nostop noprint pass", "set unwindonsignal on")

FAST_HANDOFFS = f"""\
def _tdb_fast_handoffs(run):
    import _thread, os, sys
    fast = {ATTACH_SWITCH_INTERVAL!r}
    def ours():
        return fast / 2 <= sys.getswitchinterval() <= fast * 2
    # Another attach may be under way, and it isn't the program's setting.
    interval = 0.005 if ours() else sys.getswitchinterval()
    attaching = [True]
    def restore():
        if ours():
            sys.setswitchinterval(interval)
    def in_child():
        if attaching:
            restore()
    done = _thread.allocate_lock()
    done.acquire()
    def watch():
        done.acquire(timeout={SWITCH_INTERVAL_LIMIT!r})
        restore()
    try:
        sys.setswitchinterval(fast)
        if hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=in_child)
        _thread.start_new_thread(watch, ())
        run()
    finally:
        attaching.clear()
        restore()
        done.release()
"""


def harden_gdb_command(command: str) -> str:
    """debugpy's gdb command line, with `GDB_SIGNAL_COMMANDS` run first."""
    if not command.startswith("gdb "):
        return command
    extra = " ".join(f"--eval-command='{c}'" for c in GDB_SIGNAL_COMMANDS)
    return command.replace(" --batch ", f" --batch {extra} ", 1)


def background_code(code: str, ran_marker: str) -> str:
    """Python source that runs `code` on a new thread and returns.

    It is a single line with no quotes, for gdb, and runs in `__main__`, so
    it goes through `__builtins__` and leaves no names behind. Creating
    `ran_marker` proves the code reached the target, and fails if tdb gave up
    and removed the directory, and then nothing dials back. threading is
    imported here because debugpy's attach takes the importing thread for
    the main one.
    """
    body = f"""\
import _thread, threading
{FAST_HANDOFFS}
def run():
    _tdb_fast_handoffs(lambda: exec({code!r}, {{}}))
try:
    open({ran_marker!r}, "w").close()
except OSError:
    pass
else:
    _thread.start_new_thread(run, ())
"""
    return f"__builtins__.exec(__builtins__.bytes({list(body.encode())}), {{}})"


def main() -> None:
    ran_marker = sys.argv.pop(1)

    from debugpy.server import cli

    sys.path.append(
        os.path.join(os.path.dirname(cli.pydevd.__file__), "pydevd_attach_to_process")
    )
    import add_code_to_python_process as injector

    run_python_code = injector.run_python_code

    def run_on_a_thread(pid, python_code, *args, **kwargs):
        code = background_code(python_code, ran_marker)
        return run_python_code(pid, code, *args, **kwargs)

    check_call = injector.subprocess.check_call

    def hardened_check_call(command, *args, **kwargs):
        return check_call(harden_gdb_command(command), *args, **kwargs)

    injector.run_python_code = run_on_a_thread
    injector.subprocess.check_call = hardened_check_call
    cli.main()


if __name__ == "__main__":
    main()

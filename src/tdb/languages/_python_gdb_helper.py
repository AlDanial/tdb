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
# In the work directory, for the fallback: see read_token_from_file.
TOKEN_NAME = "token"
_DEBUGPY_ATTACH_CALL = "attach_pid_injected.attach(setup);"

GDB_SIGNAL_COMMANDS = ("handle all nostop noprint pass", "set unwindonsignal on")

FAST_HANDOFFS = f"""\
def _tdb_fast_handoffs(run):
    import _thread, os, sys
    fast = {ATTACH_SWITCH_INTERVAL!r}
    def ours():
        return fast / 2 <= sys.getswitchinterval() <= fast * 2
    # Another attach may be under way, and it isn't the program's setting.
    interval = 0.005 if ours() else sys.getswitchinterval()
    def restore():
        if ours():
            sys.setswitchinterval(interval)
    # Fork callbacks can't be removed, so a process gets one, for good, and
    # it serves whichever attaches are under way.
    forks = sys.__dict__.setdefault("_tdb_attach_fork_hooks", [])
    done = _thread.allocate_lock()
    done.acquire()
    def watch():
        done.acquire(timeout={SWITCH_INTERVAL_LIMIT!r})
        restore()
    try:
        sys.setswitchinterval(fast)
        if not forks and hasattr(os, "register_at_fork"):
            forks.append(None)
            os.register_at_fork(
                after_in_child=lambda: [hook() for hook in forks if hook]
            )
        forks.append(restore)
        _thread.start_new_thread(watch, ())
        run()
    finally:
        if restore in forks:
            forks.remove(restore)
        restore()
        done.release()
"""


def harden_gdb_command(command: str | list[str]) -> str | list[str]:
    """debugpy's gdb command line, with `GDB_SIGNAL_COMMANDS` run first.

    Raises if it is a gdb command with no `--batch` to anchor on: without
    these settings, a signal during gdb's call can segfault the target.
    """
    words = command.split(" ") if isinstance(command, str) else command
    if words[0] != "gdb":
        return command
    if "--batch" not in words:
        raise RuntimeError(f"no --batch in debugpy's gdb command: {command!r}")
    if isinstance(command, str):
        extra = " ".join(f"--eval-command='{c}'" for c in GDB_SIGNAL_COMMANDS)
        return command.replace(" --batch ", f" --batch {extra} ", 1)
    at = command.index("--batch") + 1
    extra = [f"--eval-command={c}" for c in GDB_SIGNAL_COMMANDS]
    return [*command[:at], *extra, *command[at:]]


def read_token_from_file(code: str, path: str) -> str:
    """debugpy's attach code, taking the adapter's token from `path`.

    Otherwise the token is in the code, and that goes on gdb's command line,
    which any local user can read.
    """
    if _DEBUGPY_ATTACH_CALL not in code:
        raise RuntimeError(f"no {_DEBUGPY_ATTACH_CALL} in debugpy's attach code")
    read = f'setup["adapter_access_token"] = open({path!r}).read();'
    return code.replace(_DEBUGPY_ATTACH_CALL, read + _DEBUGPY_ATTACH_CALL)


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


class _HardenedSubprocess:
    """Stands in for `subprocess` inside debugpy's injector module only."""

    def __init__(self, module) -> None:
        self._module = module

    def __getattr__(self, name: str):
        return getattr(self._module, name)

    def check_call(self, command, *args, **kwargs):
        return self._module.check_call(harden_gdb_command(command), *args, **kwargs)


def install_hardening(injector, ran_marker: str) -> None:
    run_python_code = injector.run_python_code

    token_path = os.path.join(os.path.dirname(ran_marker), TOKEN_NAME)

    def run_on_a_thread(pid, python_code, *args, **kwargs):
        code = read_token_from_file(python_code, token_path)
        code = background_code(code, ran_marker)
        return run_python_code(pid, code, *args, **kwargs)

    injector.run_python_code = run_on_a_thread
    injector.subprocess = _HardenedSubprocess(injector.subprocess)


def main() -> None:
    ran_marker = sys.argv.pop(1)

    from debugpy.server import cli

    sys.path.append(
        os.path.join(os.path.dirname(cli.pydevd.__file__), "pydevd_attach_to_process")
    )
    import add_code_to_python_process as injector

    install_hardening(injector, ran_marker)
    cli.main()


if __name__ == "__main__":
    main()

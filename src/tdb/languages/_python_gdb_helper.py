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
# the handshake with no timeout, and a program shouldn't stay this way. tdb
# allows PID_ATTACH_TOTAL to connect and as long again for the response, so by
# now it has given up, and the attach is abandoned and the program let go.
SWITCH_INTERVAL_LIMIT = 65.0

# A signal reaching the target during one of gdb's calls makes gdb abandon the
# call and detach with its frame still on the stack, and the program then
# segfaults. Passing signals straight through avoids that for all but SIGINT
# and SIGTRAP, which the second setting covers by unwinding the call.
GDB_SIGNAL_COMMANDS = ("handle all nostop noprint pass", "set unwindonsignal on")

# In the work directory, for the fallback: see prepare_attach_code.
TOKEN_NAME = "token"
SCRIPT_NAME = "attach.py"
_DEBUGPY_ATTACH_CALL = "attach_pid_injected.attach(setup);"
# For a whole handshake, on each side.
HANDSHAKE_TIMEOUT = 10.0

# Runs in the target, just before debugpy's attach, on both routes.
#
# The target dials a port nobody vouches for: the adapter may have gone, and
# then anyone can hold it. debugpy's own tokens don't help, as whoever speaks
# first gives its token away. So the two sides prove they hold a key, over
# fresh nonces, with nothing secret on the wire, before pydevd sees the socket.
# _python_adapter.py is the other side.
#
# debugpy would also patch the target to rewrite each Python child's command
# line to dial the adapter: that shows the port to every local user, and the
# patch outlives tdb, so children die on a dead port.
ATTACH_PRELUDE = f"""\
def _tdb_dial(port, key):
    import hmac, os, socket, time
    deadline = time.monotonic() + {HANDSHAKE_TIMEOUT!r}
    def sign(label, nonces):
        return hmac.new(key, label + nonces, "sha256").digest()
    def read(size):
        data = b""
        while len(data) < size:
            left = deadline - time.monotonic()
            if left <= 0:
                raise OSError("timed out")
            sock.settimeout(left)
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise OSError("closed")
            data += chunk
        return data
    try:
        sock = socket.create_connection(
            ("127.0.0.1", port), timeout={HANDSHAKE_TIMEOUT!r}
        )
    except OSError:
        return None
    try:
        ours = os.urandom(32)
        sock.sendall(ours)
        reply = read(64)
        nonces = ours + reply[:32]
        if hmac.compare_digest(reply[32:], sign(b"adapter", nonces)):
            sock.sendall(sign(b"target", nonces))
            sock.settimeout(None)
            return sock
    except OSError:
        pass
    sock.close()
    return None
def _tdb_prepare(attach_pid_injected, port, key):
    import sys
    sys.path.insert(0, attach_pid_injected._debugpy_dir)
    try:
        import debugpy, debugpy.server, pydevd
    finally:
        del sys.path[0]
    debugpy.configure(subProcess=False)
    sock = _tdb_dial(port, key)
    if sock is None:
        return False
    start_client = pydevd.start_client
    def proven_socket(host, port):
        pydevd.start_client = start_client
        return sock
    pydevd.start_client = proven_socket
    return True
"""

FAST_HANDOFFS = f"""\
def _tdb_fast_handoffs(run):
    import _thread, os, sys
    # A second attach would dispose of the first's debugger, leaving the
    # program frozen until the watchdog, and neither attach working.
    me = object()
    if sys.__dict__.setdefault("_tdb_attach_under_way", me) is not me:
        return
    fast = {ATTACH_SWITCH_INTERVAL!r}
    def ours():
        return fast / 2 <= sys.getswitchinterval() <= fast * 2
    # Another attach may be under way, and it isn't the program's setting.
    interval = 0.005 if ours() else sys.getswitchinterval()
    attacher = _thread.get_ident()
    def restore():
        if ours():
            sys.setswitchinterval(interval)
    def stop_tracing_in_child():
        # With no fork handling from pydevd, a child keeps its tracer and no
        # threads to talk to tdb with, so its first stop would be for good.
        # Only the forking thread is here, and a lock another thread held is
        # held for good, so nothing that takes pydevd's locks, nor stderr's.
        try:
            sys.__dict__.pop("_tdb_attach_under_way", None)
            pydevd = sys.modules.get("pydevd")
            debugger = pydevd and pydevd.get_global_debugger()
            if debugger is None:
                return  # tdb has gone, and any tracer now is the program's.
            untraced()(None)
            threading = sys.modules.get("threading")
            if threading is not None:
                threading.settrace(None)
            if pydevd.PYDEVD_USE_SYS_MONITORING:
                pydevd.pydevd_sys_monitoring.stop_monitoring(all_threads=True)
            # Left open here, it hides the target's death from tdb.
            if debugger._client_socket is not None:
                debugger._client_socket.close()
            pydevd.set_global_debugger(None)
        except BaseException:
            pass
    def untraced():
        # pydevd's stand-in for sys.settrace warns on stderr.
        holder = sys.modules["pydevd"].pydevd_tracing.TracingFunctionHolder
        return holder._original_tracing or sys.settrace
    # Fork callbacks can't be removed, so a process gets one, for good, and
    # it serves whichever attaches are under way.
    forks = sys.__dict__.setdefault("_tdb_attach_fork_hooks", [])
    done = _thread.allocate_lock()
    done.acquire()
    def free_the_program():
        # pydevd waits for configurationDone with no timeout, so a stalled or
        # lost adapter would freeze the thread that attached. Tracing goes
        # last, as the attach turns it on once that thread is let go.
        # Found from the waiting frame, as losing the adapter clears pydevd's
        # global debugger and leaves that thread waiting on its own.
        try:
            frame = sys._current_frames().get(attacher)
            while frame is not None:
                if frame.f_code.co_name == "wait_for_ready_to_run":
                    frame.f_locals["self"].ready_to_run = True
                    if done.acquire(timeout=5):
                        stop_tracing()
                    return True
                frame = frame.f_back
        except BaseException:
            return True
        return False
    def stop_tracing():
        pydevd = sys.modules["pydevd"]
        if not pydevd.PYDEVD_USE_SYS_MONITORING:
            # stoptrace only reaches this thread. One thread can't clear
            # another's tracer, but can give it one that removes itself.
            settrace = untraced()
            def remove_itself(frame, event, arg):
                settrace(None)
            pydevd.pydevd_tracing.set_trace_to_threads(
                remove_itself, create_dummy_thread=False
            )
        pydevd.stoptrace()
    def watch():
        finished = done.acquire(timeout={SWITCH_INTERVAL_LIMIT!r})
        restore()
        # A starved attach may reach pydevd's wait only now.
        while not finished and not free_the_program():
            finished = done.acquire(timeout=1)
    try:
        sys.setswitchinterval(fast)
        if not forks and hasattr(os, "register_at_fork"):
            forks.append(stop_tracing_in_child)
            os.register_at_fork(after_in_child=lambda: [hook() for hook in forks])
        forks.append(restore)
        _thread.start_new_thread(watch, ())
        run()
    finally:
        if restore in forks:
            forks.remove(restore)
        restore()
        sys.__dict__.pop("_tdb_attach_under_way", None)
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


def prepare_attach_code(code: str, path: str) -> str:
    """debugpy's attach code, running `ATTACH_PRELUDE` first with the key from
    `path`. It can't be in the code: that goes on gdb's command line, which
    any local user can read.
    """
    if _DEBUGPY_ATTACH_CALL not in code:
        raise RuntimeError(f"no {_DEBUGPY_ATTACH_CALL} in debugpy's attach code")
    prepare = (
        f"exec({ATTACH_PRELUDE!r});"
        f'_tdb_key = bytes.fromhex(open({path!r}).read());'
        '_tdb_ready = _tdb_prepare(attach_pid_injected, setup["address"][1], _tdb_key);'
    )
    return code.replace(
        _DEBUGPY_ATTACH_CALL, f"{prepare}_tdb_ready and {_DEBUGPY_ATTACH_CALL}"
    )


def _encoded(source: str) -> str:
    """Python source as one line with no quotes, for any injection method.
    It runs in `__main__`, so it goes through `__builtins__`."""
    return f"__builtins__.exec(__builtins__.bytes({list(source.encode())}), {{}})"


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
    try:
        _tdb_fast_handoffs(lambda: exec({code!r}, {{}}))
    except BaseException:
        pass
try:
    open({ran_marker!r}, "w").close()
except OSError:
    pass
else:
    _thread.start_new_thread(run, ())
"""
    return _encoded(body)


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

    workdir = os.path.dirname(ran_marker)

    def run_on_a_thread(pid, python_code, *args, **kwargs):
        if sys.platform == "linux":
            code = prepare_attach_code(
                python_code, os.path.join(workdir, TOKEN_NAME)
            )
            code = background_code(code, ran_marker)
        else:
            # lldb and DLL injection have no gdb to get out of, and are
            # untested with a thread. Windows takes under 2 KB of code, so
            # this reads the script tdb left in the work directory.
            script = os.path.join(workdir, SCRIPT_NAME)
            code = _encoded(f"exec(open({script!r}).read())")
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

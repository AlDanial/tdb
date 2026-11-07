"""A program for `tdb -a PID` to attach to. Run with a mode and a heartbeat
file; add `no-opt-in` to refuse tracing by unrelated processes."""

import ctypes
import os
import signal
import socket
import subprocess
import sys
import threading
import time

mode, heartbeat = sys.argv[1], sys.argv[2]
opt_in = "no-opt-in" not in sys.argv[3:]

if opt_in and sys.platform == "linux":
    # PR_SET_PTRACER, PR_SET_PTRACER_ANY: lets tests pass under Yama
    # ptrace_scope=1 without changing the system.
    ctypes.CDLL(None).prctl(0x59616D61, ctypes.c_ulong(-1).value, 0, 0, 0)


def work(i):
    x = i * 2 + 1  # WORK_LINE
    return x


def loop():
    n = 0
    while True:
        n = work(n) % 1000  # LOOP_LINE
        with open(heartbeat, "w") as f:
            f.write(str(time.time()))
        print("tick", flush=True)
        time.sleep(0.05)


if mode == "debugpy_listen":
    # Before "ready": importing debugpy can take over a second, and a test
    # that attached sooner would find a target not yet using it.
    import debugpy

    debugpy.listen(("127.0.0.1", 0))

def spin():
    while True:
        sum(range(10000))


def child_work():
    return 1  # CHILD_LINE


def fork_children(raising):
    """Fork for good, logging how each child ended: "hung" if it never did."""
    log = heartbeat + ".children"
    while True:
        pid = os.fork()
        if pid == 0:
            child_work()
            sockets = sum(
                os.readlink(f"/proc/self/fd/{fd}").startswith("socket:")
                for fd in os.listdir("/proc/self/fd")
                if os.path.exists(f"/proc/self/fd/{fd}")
            )
            with open(log, "a") as f:
                f.write(f"sockets {sockets}\n")
            if raising:
                raise ValueError("in the child")
            os._exit(0)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if os.waitpid(pid, os.WNOHANG)[0]:
                outcome = "exit"
                break
            time.sleep(0.01)
        else:
            outcome = "hung"
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        with open(log, "a") as f:
            f.write(f"{outcome} \n")
        with open(heartbeat, "w") as f:
            f.write(str(time.time()))
        time.sleep(0.05)


def spawn_children():
    """Run Python children for good, logging what each saw and how it ended."""
    log = heartbeat + ".children"
    code = (
        "import sys\n"
        "cmdline = open('/proc/self/cmdline').read().replace(chr(0), ' ')\n"
        "open(sys.argv[1], 'a').write('child ' + cmdline + chr(10))\n"
    )
    while True:
        result = subprocess.run([sys.executable, "-c", code, log], capture_output=True)
        with open(log, "a") as f:
            f.write(f"exit {result.returncode}\n")
        with open(heartbeat, "w") as f:
            f.write(str(time.time()))
        time.sleep(0.05)


def hot_loop():
    n = 0
    while True:
        for _ in range(20000):
            n = work(n) % 1000
        with open(heartbeat, "w") as f:
            f.write(str(time.time()))


print("ready", flush=True)
if mode == "busy":
    loop()
elif mode == "busy_threads":
    # Every thread wants the GIL, so the injected code has to compete for it.
    for _ in range(3):
        threading.Thread(target=spin, daemon=True).start()
    hot_loop()
elif mode == "signalled":
    # A signal reaching the program during gdb's call used to abandon the
    # call and leave its frame on the stack.
    signal.signal(signal.SIGUSR1, lambda *_: None)
    loop()
elif mode == "allocating":
    # Almost always inside malloc, where gdb must not allocate in turn. An
    # idle thread makes malloc take its lock.
    threading.Thread(target=time.sleep, args=(3600,), daemon=True).start()
    while True:
        for _ in range(20000):
            bytes(3000)
        with open(heartbeat, "w") as f:
            f.write(str(time.time()))
elif mode == "shadowed":
    # Code run in __main__ can't trust these names.
    from os import open

    exec = bytes = chr = None
    while True:
        fd = open(heartbeat, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        os.write(fd, str(time.time()).encode())
        os.close(fd)
        time.sleep(0.02)
elif mode in ("forking", "forking_raise"):
    fork_children(raising=mode == "forking_raise")
elif mode == "spawning":
    spawn_children()
elif mode == "join_spawning":
    # Main blocked in join, so attaching takes debugpy's own injection.
    worker = threading.Thread(target=spawn_children, daemon=True)
    worker.start()
    worker.join()
elif mode == "join_busy":
    worker = threading.Thread(target=hot_loop, daemon=True)
    worker.start()
    worker.join()
elif mode == "join":
    worker = threading.Thread(target=loop, daemon=True)
    worker.start()
    worker.join()
elif mode == "sleep":
    time.sleep(3600)
elif mode == "stdin":
    sys.stdin.readline()
    loop()
elif mode == "recv":
    left, right = socket.socketpair()
    left.recv(1)
elif mode == "gil":
    # A worker holds the GIL in native code; PyDLL does not release it.
    worker = threading.Thread(target=ctypes.PyDLL(None).sleep, args=(6,))
    worker.start()
    worker.join()
    print("done", flush=True)
elif mode == "debugpy_listen":
    loop()

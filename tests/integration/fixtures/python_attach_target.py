"""A program for `tdb -a PID` to attach to. Run with a mode and a heartbeat
file; add `no-opt-in` to refuse tracing by unrelated processes."""

import ctypes
import socket
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

print("ready", flush=True)
if mode == "busy":
    loop()
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

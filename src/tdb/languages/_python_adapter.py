"""Runs debugpy's adapter, for `tdb -a`, behind a handshake with the target.

The adapter waits on a local port for the injected code to dial back, and
anything on the machine can connect first. This is the adapter's side of the
handshake in `_python_gdb_helper.ATTACH_PRELUDE`: only a peer that proves it
holds the key reaches debugpy, and the key itself is never sent.

Run as a script under `-P`, with the module to run as the argument, so that
it needs nothing from tdb. The key comes in the environment, as any local
user can read a command line.
"""

import hmac
import os
import runpy
import sys
import threading
import time

TOKEN_VARIABLE = "TDB_ADAPTER_TOKEN"
# As `_python_gdb_helper.HANDSHAKE_TIMEOUT`, which this can't import.
HANDSHAKE_TIMEOUT = 10.0
# Beyond this, connections are dropped at once. Holding them would run the
# process out of descriptors, and accept() failing ends debugpy's accept thread.
MAX_UNPROVEN = 16


def _read(sock, size, deadline):
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


def _peer_proves_itself(sock, key, deadline):
    theirs = _read(sock, 32, deadline)
    ours = os.urandom(32)
    nonces = theirs + ours
    sock.sendall(ours + hmac.new(key, b"adapter" + nonces, "sha256").digest())
    expected = hmac.new(key, b"target" + nonces, "sha256").digest()
    return hmac.compare_digest(_read(sock, 32, deadline), expected)


def require_handshake(key: bytes) -> None:
    # The adapter closes stderr off before it first imports its logging, which
    # keeps hold of the stream. Here the same imports come first.
    stderr = sys.stderr
    sys.stderr = open(os.devnull, "w")
    try:
        # In the adapter's own order: servers first is a circular import.
        from debugpy.adapter import clients, servers  # noqa: F401
    finally:
        sys.stderr = stderr

    connection = servers.Connection
    slots = threading.BoundedSemaphore(MAX_UNPROVEN)

    def handle(sock):
        try:
            deadline = time.monotonic() + HANDSHAKE_TIMEOUT
            proven = _peer_proves_itself(sock, key, deadline)
            sock.settimeout(None)
        except OSError:
            proven = False
        finally:
            slots.release()
        if proven:
            connection(sock)
        else:
            sock.close()

    def connection_after_handshake(sock):
        # On its own thread: this runs on the one thread accepting connections.
        if not slots.acquire(blocking=False):
            sock.close()
            return
        try:
            threading.Thread(target=handle, args=(sock,), daemon=True).start()
        except RuntimeError:
            slots.release()
            sock.close()

    servers.Connection = connection_after_handshake


def _exit_with_parent() -> None:
    """Blocked waiting for the program, the adapter never sees tdb go, and
    would hold its port for good. Windows has no reparenting to watch for."""
    parent = os.getppid()
    while os.getppid() == parent:
        time.sleep(1)
    os._exit(0)


def main() -> None:
    module = sys.argv.pop(1)
    token = os.environ.pop(TOKEN_VARIABLE, None)
    if not token:
        sys.exit(f"tdb: {TOKEN_VARIABLE} is not set; refusing to start the adapter")
    require_handshake(bytes.fromhex(token))
    if os.name == "posix":
        threading.Thread(target=_exit_with_parent, daemon=True).start()
    runpy.run_module(module, run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()

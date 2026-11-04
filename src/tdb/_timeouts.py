"""Centralized timeout constants (seconds).

Reasoning per constant lives beside its value so the "why this number?"
question doesn't require reading commit history or guessing. Group by
subsystem; alphabetize within group.

Bare integers/floats for wall-clock seconds. Always pass to
`asyncio.wait_for(..., timeout=X)` (or equivalent). Don't multiply
these together to derive new timeouts — add a new named constant.
"""

from __future__ import annotations


# --- DAP protocol round-trips --------------------------------------------
# debugpy is normally fast (single-digit ms) but some requests block on the
# debuggee's main thread (e.g. evaluate during a long sync call). 30s gives
# headroom for those without hanging a user-typed step forever.
DAP_REQUEST = 30.0
"""Default per-request timeout for `DAPClient._send`."""

# Child-process DAPClient disconnect runs in parallel with the parent's
# `disconnect(terminate=True)`. debugpy cascades termination from the
# parent, so the per-child handshake is cleanup, not load-bearing — keep
# it short so multi-process quit doesn't pile up 8 × 30s waits.
DAP_DISCONNECT_CHILD = 1.0
"""Wait for one child DAP session to acknowledge disconnect."""

# Parent disconnect is also bounded. The parent's `disconnect` is what
# actually kills the debuggee in non-remote-attach mode, so we wait a
# bit longer than children — but still capped to keep quit snappy.
DAP_DISCONNECT_PARENT = 2.0
"""Wait for the parent DAP session to acknowledge disconnect."""

# Time to wait for debugpy to emit `initialized` after launch/attach.
# Cold debugpy start (no compiled cache, large dependency graph) can
# take ~1-3s on modest machines; 10s is comfortable padding.
DAP_INITIALIZED = 10.0
"""Wait for `initialized` event after launch / attach."""

# Time to wait for a `stopped` event after launch with `stop_on_entry=True`.
# Same shape as DAP_INITIALIZED — we just need debugpy to reach line 1.
DAP_STOP_ON_ENTRY = 10.0
"""Wait for the first `stopped` event when stop_on_entry is set."""

# Child-process attach handshake: after a `debugpyAttach` reverse event,
# the child opens its DAP socket and we connect + initialize. 30s allows
# for slow forkserver starts on Linux (Python 3.14 default).
DAP_CHILD_ATTACH = 30.0
"""End-to-end timeout for connecting + initializing a child DAP session."""

# Ceiling for a spawn_tcp adapter to print its "listening at" line.
ADAPTER_LISTEN = 15.0

# How long a request that lost the DAP stream waits for the adapter's
# death watcher, so the error carries the adapter's stderr and diagnosis
# (gdb: "Interpreter `dap' unrecognized" + remedy) instead of asyncio's
# bare "Connection lost". An adapter that broke its pipe is exiting; a
# live one (TCP hiccup) keeps the original error after this.
ADAPTER_EXIT_GRACE = 2.0
"""Timeout for spawn_tcp adapter to announce its listening port."""

# Run-mode examine reads each child process's threads and stacks in
# turn. A child that is mid-fork or wedged in native code can leave
# `threads` unanswered; bound it so one slow child can't stall the
# whole snapshot (the parent and other children still get captured).
EXAMINE_CHILD = 2.0
"""Per-child-process budget for the examine collector."""


# --- RPC handler waits ---------------------------------------------------
# Step / continue can run a long tail in multi-process programs: after
# releasing all breakpoint hits, the remainder of the script may run for
# many seconds before `terminated` arrives. 30s wasn't enough; 10 min is
# arbitrary-but-comfortable for typical interactive sessions.
RPC_STEP_WAIT = 600.0
"""How long the RPC `_step_action` blocks awaiting the next stop."""

# `tdb -a PID` on Python. `sys.remote_exec` reports success as soon as the
# script is queued, and the script never runs while the main thread is
# blocked, so tdb waits this long for it to connect and then falls back.
# The slowest success measured was 3.9s, behind a C call holding the GIL.
PID_ATTACH_REMOTE_EXEC = 5.0
# Ceiling for injection as a whole, whichever method is used.
PID_ATTACH_TOTAL = 30.0
# Quitting mid-injection first lets the handshake finish, for this long:
# pydevd waits for it forever if the adapter goes away.
PID_ATTACH_QUIT = 5.0
# An injected target must answer `threads` within this; if it can't, no
# later request will get a reply either.
PID_ATTACH_RESPONSE = 5.0
# With no stop by this long after attaching, the program isn't running
# Python code and a pause can't land; the TUI says so.
PID_ATTACH_BLOCKED_NOTICE = 3.0

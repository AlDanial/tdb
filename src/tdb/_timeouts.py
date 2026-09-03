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
"""Timeout for spawn_tcp adapter to announce its listening port."""


# --- RPC handler waits ---------------------------------------------------
# Step / continue can run a long tail in multi-process programs: after
# releasing all breakpoint hits, the remainder of the script may run for
# many seconds before `terminated` arrives. 30s wasn't enough; 10 min is
# arbitrary-but-comfortable for typical interactive sessions.
RPC_STEP_WAIT = 600.0
"""How long the RPC `_step_action` blocks awaiting the next stop."""

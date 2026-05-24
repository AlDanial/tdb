"""One-shot loader that rehydrates a DebugState from a tdb.exception_hook snapshot.

The post-mortem flow:
  1. A debuggee process crashes with `sys.excepthook = tdb.exception_hook`
     installed; the hook writes a JSON snapshot to a temp file
     (frames + per-frame scopes + variables) and re-execs tdb with
     `--post-mortem snapshot.json`.
  2. The new tdb process reads the snapshot and calls
     `load_post_mortem_into(state, snapshot)` — this module.

The result is a fully-populated `DebugState` in `POST_MORTEM` phase:
no live DAP session, all stack_frames + frame_scopes + variables
already in memory, no async I/O involved. Lifted out of `DebugController`
because it shares nothing with the rest of the controller's machinery
(no `_active_client`, no event loop, no background tasks). Keeping it
separate also makes it independently testable.
"""

from __future__ import annotations

import os

from tdb.dap.types import Scope, Source, StackFrame, Variable
from tdb.session.state import DebugState, SessionPhase


def load_post_mortem_into(state: DebugState, snapshot: dict) -> None:
    """Populate `state` from a tdb.exception_hook snapshot dict.

    After this call: `state.phase == POST_MORTEM`, `stack_frames`,
    `frame_scopes`, `variables`, `current_frame_id`, and `scopes` are
    all set. `stop_reason == "exception"`. No DAP traffic happens.
    """
    state.transition_to(SessionPhase.POST_MORTEM)
    state.stop_reason = "exception"

    frames_data = snapshot.get("frames", [])
    vars_data: dict[str, list[dict]] = snapshot.get("variables", {})

    # Rebuild variables keyed by int reference.
    state.variables = {}
    for ref_str, entries in vars_data.items():
        ref = int(ref_str)
        state.variables[ref] = [
            Variable(
                name=e.get("name", ""),
                value=e.get("value", ""),
                type=e.get("type", ""),
                variables_reference=e.get("variablesReference", 0),
            )
            for e in entries
        ]

    # Rebuild frames + per-frame scopes.
    state.stack_frames = []
    state.frame_scopes = {}
    for fd in frames_data:
        fid = fd["id"]
        path = fd.get("filename", "")
        frame = StackFrame(
            id=fid,
            name=fd.get("funcname", "<frame>"),
            source=Source(
                path=path,
                name=os.path.basename(path) if path else None,
            ),
            line=fd.get("lineno", 0),
        )
        state.stack_frames.append(frame)
        state.frame_scopes[fid] = [
            Scope(name=s["name"], variables_reference=s["variablesReference"])
            for s in fd.get("scopes", [])
        ]

    if state.stack_frames:
        top = state.stack_frames[0]
        state.current_frame_id = top.id
        state.scopes = state.frame_scopes.get(top.id, [])

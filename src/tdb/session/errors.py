"""Session-level exceptions shared across inspection layers.

Lives in its own module so consumers below the service layer (e.g. the
Rust concurrency collector) can raise the gate error without importing
`inspect_service`, which imports them back.
"""

from __future__ import annotations


class SessionGateError(Exception):
    """The session is in a phase where inspection is impossible.

    ``reason`` is ``"running"`` (debuggee executing — no frames to
    inspect; pause first), ``"terminated"`` (session over), or
    ``"unsupported"`` (the active language profile doesn't support
    task/process inspection — e.g. cpp). Consumers translate the reason
    into their own user-facing wording.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

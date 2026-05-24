"""Per-child DAPClient lifecycle for multi-process debugging.

When the debuggee spawns a subprocess and debugpy fires `debugpyAttach`,
the parent's DAP session tells us host/port for a NEW session against
the child. We open that connection, register stopped/terminated/output
handlers, configure exception + line breakpoints, and add the new
DAPClient to `_clients` keyed by pid.

Lives outside `DebugController` because the manager owns its own
non-trivial state machine (dict of clients, attach lock, cascade
detection for forkserver workers in Python 3.14+) and shared nothing
with the rest of the controller besides a handful of callbacks.

The manager holds a reference back to the controller for:
  - `state` (to apply atomic enter_stop / read breakpoints)
  - `event_handler` (to surface child stops to the App / RPC server)
  - `_active_client` swap on child stop (so subsequent stepping goes
    to the process that's actually paused)
  - `_pause_parent` reciprocal (parent must pause when a child stops
    so the user sees a coherent "everyone is stopped" snapshot)
  - `_spawn_bg` for fire-and-forget tasks with strong refs
  - `_enabled_bps` helper
  - `_launch_params` for the parent's jmc setting

Sharing the controller object is pragmatic. The alternative — a
ChildProcessManagerHost Protocol — adds friction without changing the
real coupling: stops on a child do mutate controller-owned state.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from tdb._timeouts import DAP_CHILD_ATTACH, DAP_INITIALIZED
from tdb.dap.client import DAPClient
from tdb.dap.messages import Event

if TYPE_CHECKING:
    from tdb.session.controller import DebugController


log = logging.getLogger(__name__)


class ChildProcessManager:
    """Owns the dict of child DAPClients plus their attach/detach loop."""

    def __init__(self, controller: DebugController) -> None:
        self._ctl = controller
        self._clients: dict[int, DAPClient] = {}
        # Kept for backward compat: external callers may have grabbed the
        # attribute via `controller._child_attach_lock`. Attaches no
        # longer take it (see _attach for why dropping the serialization
        # was an 8-child latency win).
        self._attach_lock = asyncio.Lock()

    # --- Public surface --------------------------------------------------

    def has_clients(self) -> bool:
        return bool(self._clients)

    def get_client(self, pid: int) -> DAPClient | None:
        return self._clients.get(pid)

    def get_pids(self) -> list[int]:
        return list(self._clients.keys())

    def clients(self) -> list[DAPClient]:
        return list(self._clients.values())

    def register_on(self, parent_client: DAPClient) -> None:
        """Register the `debugpyAttach` reverse-request handler on the
        parent DAP client. Called from `controller._setup_event_handlers`.
        """
        parent_client.on_event("debugpyAttach", self._on_attach_event)

    async def close_all(self) -> None:
        """Disconnect + stop every tracked child concurrently with a
        short timeout. Used during controller.stop()."""
        from tdb._timeouts import DAP_DISCONNECT_CHILD

        async def _close_child(child: DAPClient) -> None:
            try:
                await asyncio.wait_for(
                    child.disconnect(terminate=False),
                    timeout=DAP_DISCONNECT_CHILD,
                )
            except (asyncio.TimeoutError, Exception):
                pass
            try:
                await child.stop()
            except Exception:
                pass

        if self._clients:
            await asyncio.gather(
                *(_close_child(c) for c in self._clients.values()),
                return_exceptions=True,
            )
        self._clients.clear()

    async def pause_all(self) -> None:
        """Pause every child. Best-effort: per-child failures are
        logged inside DAPClient and don't abort the others."""
        for _pid, child in list(self._clients.items()):
            try:
                threads = await child.threads()
                if threads:
                    await child.pause(threads[0].id)
            except Exception:
                pass

    # --- debugpyAttach handler ------------------------------------------

    def _on_attach_event(self, event: Event) -> None:
        """debugpyAttach: a child process is ready for debugging."""
        body = event.body
        host = body.get("connect", {}).get("host", "127.0.0.1")
        port = body.get("connect", {}).get("port")
        pid = body.get("subProcessId") or body.get("processId")
        if not port:
            log.warning("debugpyAttach missing port: %s", body)
            return
        log.info("Child process detected: pid=%s port=%s", pid, port)
        self._ctl._spawn_bg(self._attach(host, port, pid))

    async def _attach(
        self,
        host: str,
        port: int,
        pid: int | None = None,
    ) -> None:
        """Connect to the adapter for a child process and configure breakpoints.

        Each child gets its own DAPClient/TCP connection and its own
        per-connection DAP sequence numbers, so attaches can run fully
        in parallel — the only shared state is `_clients` (dict
        assignment is atomic in CPython) and read-only
        `controller._launch_params` / `state.breakpoints`. Previously
        serialized via `_attach_lock`; dropping that took 8-child
        attach from ~2.1s to ~0.3s.
        """
        if pid is not None and pid in self._clients:
            return  # already attached (duplicate debugpyAttach event)
        child = DAPClient()
        try:
            await child.connect(host, port)

            # Wait for 'initialized' event before configuring
            initialized = asyncio.Event()
            child.on_event("initialized", lambda e: initialized.set())

            # Forward stopped events: switch _active_client to the child
            # so subsequent step/continue go to the right process; ask
            # the parent to pause too so the user sees a coherent
            # "everything is paused" snapshot.
            def on_child_stopped(
                event: Event,
                _child: DAPClient = child,
            ) -> None:
                thread_id = event.body.get("threadId")
                reason = event.body.get("reason", "unknown")
                description = event.body.get("description")
                text = event.body.get("text")

                # Single state authority — same enter_stop the parent uses.
                self._ctl.state.enter_stop(thread_id, reason)

                self._ctl._active_client = _child
                if reason not in ("pause",):
                    self._ctl._spawn_bg(self._ctl._pause_parent())
                self._ctl.event_handler.on_stopped(
                    thread_id,
                    reason,
                    description,
                    text,
                )

            def on_child_terminated(event: Event) -> None:
                for child_pid, c in list(self._clients.items()):
                    if c is child:
                        self._clients.pop(child_pid, None)
                        break

            child.on_event("stopped", on_child_stopped)
            child.on_event("terminated", on_child_terminated)
            child.on_event("exited", on_child_terminated)
            child.on_event("output", self._ctl._on_output)
            # Cascade subprocess detection: when this child spawns its
            # own children (e.g. multiprocessing forkserver workers in
            # Python 3.14+, where Pool() workers are forked from the
            # forkserver child rather than the parent), their attach
            # events arrive on this child's DAP session, not the
            # parent's. Same handler routes them through to us.
            child.on_event("debugpyAttach", self._on_attach_event)

            await child.initialize()

            # Send attach with subProcessId (not processId) to route to
            # the right child without triggering ptrace injection.
            # Inherit parent's just_my_code setting.
            jmc = self._ctl._launch_params.get("just_my_code", True)
            attach_future = await child.attach(
                host=host,
                port=port,
                sub_process_id=pid,
                just_my_code=jmc,
            )

            await asyncio.wait_for(initialized.wait(), timeout=DAP_INITIALIZED)

            # Configure breakpoints — send per-source setBreakpoints
            # concurrently. Each call is an independent DAP round-trip
            # against the child's own session; running them sequentially
            # was the inner loop's contribution to the attach latency.
            await child.set_exception_breakpoints(["userUnhandled"])
            state = self._ctl.state
            if not state.breakpoints_disabled and state.breakpoints:
                enabled_bps = self._ctl._enabled_bps

                async def _set_bps(source_path: str, bps: list) -> None:
                    try:
                        await child.set_breakpoints(
                            source_path,
                            enabled_bps(bps),
                        )
                    except Exception:
                        pass

                await asyncio.gather(
                    *(_set_bps(sp, bps) for sp, bps in state.breakpoints.items())
                )

            await child.configuration_done()
            await asyncio.wait_for(attach_future, timeout=DAP_CHILD_ATTACH)

            key = pid or port
            self._clients[key] = child
            log.info("Attached to child process pid=%s (port %d)", pid, port)

        except Exception:
            log.exception("Failed to attach to child (port %d)", port)
            try:
                await child.stop()
            except Exception:
                pass

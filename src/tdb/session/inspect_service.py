"""Shared inspection workflows: fetch-and-parse logic used by both consumers.

The TUI (`tdb.app_handlers.inspection.InspectionWorkflows`) and the
JSON-RPC server (`tdb.server.handlers.RpcHandlers`) both implement the
same "what is the program doing right now?" queries:

  - enumerate asyncio tasks (evaluate ``TASK_COLLECT_EXPR`` in the
    debuggee, parse the JSON),
  - fetch one task's locals (evaluate ``TASK_LOCALS_EXPR`` against a
    real frame id, expand the resulting variables reference),
  - list threads, and fetch a thread's stack + scopes + variables,
  - enumerate child processes (evaluate ``PROCESS_COLLECT_EXPR`` on the
    parent, with a /proc fallback),
  - fetch a child process's stack + scopes + variables via its DAPClient.

Before this module existed, each consumer carried its own copy of those
sequences and they drifted: the RPC server evaluated the process-collect
expression on the *active* client (wrong when a child process is
active) and had no /proc fallback, while task locals were resolved
against the parent client instead of the active one. Centralizing the
mechanics here gives both consumers the corrected behavior and leaves
them with presentation only (textual modals / RpcResponse text).

Like `tdb.inspection`, this module is UI-free: no textual, no FastAPI.
It speaks in `tdb.dap.types` and `tdb.inspection` dataclasses, raising
`SessionGateError` instead of rendering "program is running" messages so
each consumer keeps its own wording (toast vs RPC error string).

The service holds a *provider* callable rather than a controller because
the RPC layer swaps controllers on restart (see
`tdb.server.handlers.ControllerRef`); reading through the provider on
every call keeps the service pointed at the live controller.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from tdb.dap.types import Scope, StackFrame, Thread, Variable
from tdb.inspection import (
    PROCESS_COLLECT_EXPR,
    ProcessInfo,
    TASK_COLLECT_EXPR,
    TASK_LOCALS_EXPR,
    AsyncTaskInfo,
    parse_process_json,
    parse_task_json,
)
from tdb.rust_concurrency.collector import RustConcurrencyCollector
from tdb.rust_concurrency.models import ConcurrencySnapshot
from tdb.session.errors import SessionGateError

__all__ = ["InspectService", "SessionGateError", "StackDetail"]

if TYPE_CHECKING:
    from tdb.session.controller import DebugController

log = logging.getLogger(__name__)


# Stack/scope/variable bundle returned by `thread_stack` and
# `process_stack`. `variables` is keyed by each scope's
# variables_reference — the shape the TUI modals consume directly; the
# RPC handlers flatten it for text output.
StackDetail = tuple[
    list[StackFrame],
    list[Scope],
    dict[int, list[Variable]],
]


class InspectService:
    """Controller-backed implementation of the shared inspection queries."""

    def __init__(self, controller_provider: Callable[[], "DebugController"]) -> None:
        self._provider = controller_provider
        self._rust_collector = RustConcurrencyCollector()

    @property
    def _ctrl(self) -> "DebugController":
        return self._provider()

    def _gate(self) -> None:
        """Raise SessionGateError unless the debuggee is inspectable."""
        state = self._ctrl.state
        if state.is_terminated:
            raise SessionGateError("terminated")
        if state.is_running:
            raise SessionGateError("running")

    def _require_task_inspection(self) -> None:
        """Task/process inspection injects language-specific code into
        the debuggee via DAP evaluate; only profiles that opt in
        (Python/asyncio today) support it."""
        if not self._ctrl.profile.capabilities.task_inspection:
            raise SessionGateError("unsupported")

    def _require_concurrency_inspection(self) -> None:
        if self._ctrl.profile.capabilities.concurrency_inspection != "rust":
            raise SessionGateError("unsupported")

    # --- asyncio tasks ---------------------------------------------------

    async def collect_tasks(self) -> list[AsyncTaskInfo]:
        """Enumerate asyncio tasks by evaluating in the debuggee.

        Raises SessionGateError when not inspectable; raises whatever
        `parse_task_json` raises when the evaluate result isn't the
        expected JSON (typically because the evaluate itself failed and
        `controller.evaluate` returned the error string).
        """
        self._require_task_inspection()
        self._gate()
        raw = await self._ctrl.evaluate(TASK_COLLECT_EXPR)
        tasks = parse_task_json(raw)
        if not tasks:
            # Surface the raw payload at debug level — the most common
            # cause is "program doesn't use asyncio", but a truncated or
            # error payload is worth being able to see.
            log.debug(
                "Task collection returned no tasks. Raw: %s",
                raw[:300] if raw else "(empty)",
            )
        return tasks

    async def task_locals(self, task_name: str) -> list[Variable]:
        """Fetch one task's local variables, expanded one level.

        Evaluates against the *active* client (parent or child —
        whichever process is being inspected) with a real frame id, so
        it works after synthetic-frame task navigation. Returns [] when
        the frame isn't available rather than raising: both consumers
        render that as a "(no variables)" state.
        """
        self._require_task_inspection()
        self._gate()
        ctrl = self._ctrl
        try:
            expr = TASK_LOCALS_EXPR.format(task_name=task_name)
            # Route around synthetic (negative) frame ids installed by
            # task navigation — debugpy rejects them.
            frame_id = await ctrl.resolve_evaluate_frame_id(ctrl.active_client)
            _result, var_ref = await ctrl.active_client.evaluate(
                expr,
                frame_id=frame_id,
                context="repl",
            )
            if var_ref > 0:
                return await ctrl.active_client.variables(var_ref)
        except Exception:
            log.debug("Failed to load variables for task %s", task_name)
        return []

    # --- threads ----------------------------------------------------------

    async def list_threads(self) -> list[Thread]:
        """List the debuggee's threads. Raises on gate or DAP failure."""
        self._gate()
        return await self._ctrl.client.threads()

    async def collect_rust_concurrency(self) -> ConcurrencySnapshot:
        """Collect the Rust wait graph while the debuggee is stopped."""
        self._require_concurrency_inspection()
        self._gate()
        return await self._rust_collector.collect_and_analyze(self._ctrl)

    async def thread_frames(
        self, thread_id: int, *, levels: int = 8
    ) -> list[StackFrame]:
        """A thread's stack only — no scopes/variables (cheap, for
        thread classification)."""
        self._gate()
        return await self._ctrl.client.stack_trace(thread_id, levels=levels)

    async def thread_stack(self, thread_id: int) -> StackDetail:
        """Fetch a thread's stack, plus scopes + variables of its top frame.

        Raises on gate failure or if the stack trace itself can't be
        fetched. Scope/variable expansion is best-effort: a failure
        there returns whatever was gathered so far.
        """
        self._gate()
        client = self._ctrl.client
        frames = await client.stack_trace(thread_id)
        scopes: list[Scope] = []
        variables: dict[int, list[Variable]] = {}
        if frames:
            try:
                scopes = await client.scopes(frames[0].id)
                for scope in scopes:
                    variables[scope.variables_reference] = await client.variables(
                        scope.variables_reference
                    )
            except Exception:
                log.debug("Failed to fetch variables for thread %d", thread_id)
        return frames, scopes, variables

    # --- child processes ---------------------------------------------------

    async def collect_processes(self) -> list[ProcessInfo]:
        """Enumerate multiprocessing children.

        Primary source: evaluate ``PROCESS_COLLECT_EXPR`` on the
        *parent* process (children don't know their siblings). Fallback:
        build entries from the controller's tracked child PIDs via
        /proc (Linux), which still works when the parent isn't paused in
        a frame we can evaluate against.
        """
        self._require_task_inspection()
        self._gate()
        try:
            raw = await self._ctrl.evaluate_on_parent(PROCESS_COLLECT_EXPR)
            processes = parse_process_json(raw)
            if processes:
                return processes
        except Exception:
            log.debug("Process collection via evaluate failed", exc_info=True)
        return self._processes_from_pids()

    def _processes_from_pids(self) -> list[ProcessInfo]:
        """Build ProcessInfo entries from tracked child PIDs via /proc.

        Linux-only fallback used when DAP `evaluate` against the parent
        is unavailable (e.g., the parent isn't the active process).
        """
        result: list[ProcessInfo] = []
        for pid in self._ctrl.get_child_pids():
            try:
                cmdline_path = Path(f"/proc/{pid}/cmdline")
                if not cmdline_path.exists():
                    continue
                cmdline = (
                    cmdline_path.read_bytes().replace(b"\x00", b" ").decode().strip()
                )
                status_path = Path(f"/proc/{pid}/status")
                name = f"Process-{pid}"
                if status_path.exists():
                    for line in status_path.read_text().splitlines():
                        if line.startswith("Name:"):
                            name = line.split(":", 1)[1].strip()
                            break
                result.append(
                    ProcessInfo(
                        name=name,
                        pid=pid,
                        alive=True,
                        exitcode=None,
                        daemon=False,
                        target=cmdline[:200] if cmdline else "unknown",
                        args="()",
                        kwargs="{}",
                        start_method="",
                    )
                )
            except Exception:
                pass
        return result

    async def process_stack(self, pid: int) -> StackDetail | None:
        """Fetch a child process's stack + scopes + variables.

        Returns None when the pid has no tracked DAPClient (PIDs from
        /proc may not match the controller's keys). Per-step failures
        inside the fetch are best-effort, mirroring `thread_stack`.
        """
        child = self._ctrl.get_child_client(pid)
        if child is None:
            return None
        frames: list[StackFrame] = []
        scopes: list[Scope] = []
        variables: dict[int, list[Variable]] = {}
        try:
            threads = await child.threads()
            if threads:
                frames = await child.stack_trace(threads[0].id)
                if frames:
                    scopes = await child.scopes(frames[0].id)
                    for scope in scopes:
                        variables[scope.variables_reference] = await child.variables(
                            scope.variables_reference
                        )
        except Exception:
            log.debug("Failed to fetch detail for child process %d", pid)
        return frames, scopes, variables

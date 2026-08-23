"""Bounded stopped-state collection of debugger-neutral Rust observations."""

from __future__ import annotations

import asyncio
import inspect
import sys
from dataclasses import replace
from typing import Any

from tdb.dap.types import StackFrame, Variable
from tdb.rust_concurrency.analyzer import analyze
from tdb.rust_concurrency.probes import probe_for_adapter
from tdb.rust_concurrency.models import (
    ProbeResult,
    RawFrame,
    RawSnapshot,
    RawThread,
    RawVariable,
)
from tdb.session.state import SessionPhase

# This is deliberately a cheap prefilter.  It prevents expanding locals for
# ordinary application frames while leaving the authoritative matching to the
# portable classifier after collection.
_RUST_WAIT_MARKERS = (
    "std::sync::poison::mutex::",
    "std::sync::poison::rwlock::",
    "std::sync::poison::condvar::",
    "mutex::lock",
    "rwlock::read",
    "rwlock::write",
    "condvar::wait",
    "mpsc::receiver::recv",
    "mpsc::syncsender::send",
    "joinhandle",
    "std::thread::park",
)


def _candidate_frame(frame_name: str) -> bool:
    lowered = frame_name.lower()
    return any(marker.lower() in lowered for marker in _RUST_WAIT_MARKERS)


def _raw_variable(variable: Variable) -> RawVariable:
    return RawVariable(
        name=variable.name,
        value=variable.value,
        type_name=variable.type,
        evaluate_name=variable.evaluate_name,
    )


def _raw_frame(frame: StackFrame, variables: tuple[RawVariable, ...] = ()) -> RawFrame:
    return RawFrame(
        frame_id=frame.id,
        name=frame.name,
        source_path=frame.source.path if frame.source is not None else None,
        line=frame.line,
        variables=variables,
    )


class RustConcurrencyCollector:
    """Collect a bounded snapshot while the debuggee remains stopped."""

    def __init__(
        self,
        *,
        max_threads: int = 256,
        max_frames: int = 64,
        max_variables: int = 256,
        probe: Any = None,
        probe_timeout: float = 2.0,
        snapshot_timeout: float = 10.0,
    ) -> None:
        self.max_threads = max(0, max_threads)
        self.max_frames = max(0, max_frames)
        self.max_variables = max(0, max_variables)
        self.probe = probe
        self.probe_timeout = max(0.0, probe_timeout)
        self.snapshot_timeout = max(0.0, snapshot_timeout)

    async def _await_stopped(
        self, controller: Any, awaitable: Any, *, deadline: float
    ) -> Any:
        """Await one operation while enforcing the original stop and deadline."""
        operation = asyncio.ensure_future(awaitable)
        generation = getattr(controller.state, "generation", None)

        async def resumed() -> None:
            while True:
                await asyncio.sleep(0.01)
                self._gate(controller)
                if getattr(controller.state, "generation", None) != generation:
                    from tdb.session.inspect_service import SessionGateError

                    raise SessionGateError("running")

        guard = asyncio.create_task(resumed())
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        try:
            done, _ = await asyncio.wait(
                (operation, guard),
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise asyncio.TimeoutError
            if guard in done:
                return guard.result()
            return operation.result()
        finally:
            for task in (operation, guard):
                if not task.done():
                    task.cancel()
            await asyncio.gather(operation, guard, return_exceptions=True)

    @staticmethod
    def _gate(controller: Any) -> None:
        # Import lazily to avoid coupling the Rust package to the inspection
        # service during module initialization.
        from tdb.session.inspect_service import SessionGateError

        state = controller.state
        if state.is_terminated:
            raise SessionGateError("terminated")
        if state.phase is not SessionPhase.STOPPED:
            raise SessionGateError("running")

    @staticmethod
    def _platform() -> str:
        return "darwin" if sys.platform == "darwin" else sys.platform

    async def _frame_variables(
        self, client: Any, frame: StackFrame, remaining: int
    ) -> tuple[tuple[RawVariable, ...], int, str | None]:
        if remaining <= 0:
            return (), 0, None
        try:
            scopes = await client.scopes(frame.id)
        except Exception as exc:
            return (), 0, f"frame {frame.id} scopes unavailable: {exc}"

        values: list[RawVariable] = []
        for scope in scopes:
            # Only locals/arguments can name Rust synchronization objects.
            # Registers/Globals/Statics scopes would both pollute address
            # extraction and burn the snapshot-wide variable budget.
            scope_kind = scope.name.lower()
            if not (scope_kind.startswith("local") or scope_kind.startswith("argument")):
                continue
            if remaining - len(values) <= 0:
                break
            try:
                variables = await client.variables(
                    scope.variables_reference,
                    start=0,
                    count=remaining - len(values),
                )
            except Exception as exc:
                return (
                    tuple(values),
                    len(values),
                    f"scope {scope.name} variables unavailable: {exc}",
                )
            capacity = remaining - len(values)
            values.extend(_raw_variable(variable) for variable in variables[:capacity])
        return tuple(values), len(values), None

    async def _collect_thread(
        self, controller: Any, thread: Any
    ) -> tuple[RawThread, tuple[str, ...]]:
        client = controller.client
        warnings: list[str] = []
        if self.max_frames == 0:
            return RawThread(thread.id, thread.name, (), None), ()
        try:
            frames = await client.stack_trace(
                thread.id, start_frame=0, levels=self.max_frames
            )
        except Exception as exc:
            return (
                RawThread(thread.id, thread.name, (), None),
                (f"thread {thread.id} stack unavailable: {exc}",),
            )

        raw_frames = [_raw_frame(frame) for frame in frames[: self.max_frames]]
        return (
            RawThread(thread.id, thread.name, tuple(raw_frames), None),
            tuple(warnings),
        )

    async def _collect(self, controller: Any, *, deadline: float) -> RawSnapshot:
        """Fetch bounded thread stacks and best-effort selected locals."""
        self._gate(controller)
        phase = controller.state.phase
        generation = getattr(controller.state, "generation", None)
        client = controller.client
        try:
            threads = sorted(
                await self._await_stopped(
                    controller, client.threads(), deadline=deadline
                ),
                key=lambda thread: thread.id,
            )[: self.max_threads]
        except asyncio.TimeoutError:
            return RawSnapshot(
                adapter=getattr(controller.profile.adapter, "id", ""),
                platform=self._platform(),
                rust_version=None,
                threads=(),
                warnings=(f"snapshot timed out after {self.snapshot_timeout:g}s",),
            )
        collected: dict[int, RawThread] = {}
        warnings: list[str] = []

        async def collect_one(thread: Any) -> None:
            raw_thread, thread_warnings = await self._collect_thread(controller, thread)
            collected[raw_thread.thread_id] = raw_thread
            warnings.extend(thread_warnings)

        async def collect_stacks() -> None:
            async with asyncio.TaskGroup() as group:
                for thread in threads:
                    group.create_task(collect_one(thread))

        try:
            await self._await_stopped(controller, collect_stacks(), deadline=deadline)
        except asyncio.TimeoutError:
            warnings.append(f"snapshot timed out after {self.snapshot_timeout:g}s")
            for thread in threads:
                collected.setdefault(
                    thread.id, RawThread(thread.id, thread.name, (), None)
                )
            return RawSnapshot(
                adapter=getattr(controller.profile.adapter, "id", ""),
                platform=self._platform(),
                rust_version=None,
                threads=tuple(collected[thread.id] for thread in threads),
                warnings=tuple(sorted(warnings)),
            )

        # Expand selected frames in stable thread/frame order, enforcing one
        # snapshot-wide variable ceiling rather than a per-thread ceiling.
        remaining = self.max_variables
        for thread in threads:
            raw_thread = collected[thread.id]
            expanded: list[RawFrame] = []
            for frame in raw_thread.frames:
                variables: tuple[RawVariable, ...] = ()
                if _candidate_frame(frame.name) and remaining:
                    try:
                        variables, used, warning = await self._await_stopped(
                            controller,
                            self._frame_variables(
                                client,
                                StackFrame(frame.frame_id, frame.name, line=frame.line),
                                remaining,
                            ),
                            deadline=deadline,
                        )
                    except asyncio.TimeoutError:
                        warnings.append(
                            f"snapshot timed out after {self.snapshot_timeout:g}s"
                        )
                        return RawSnapshot(
                            adapter=getattr(controller.profile.adapter, "id", ""),
                            platform=self._platform(),
                            rust_version=None,
                            threads=tuple(collected[item.id] for item in threads),
                            warnings=tuple(sorted(warnings)),
                        )
                    remaining -= used
                    if warning is not None:
                        warnings.append(f"thread {thread.id} {warning}")
                expanded.append(replace(frame, variables=variables))
            collected[thread.id] = replace(raw_thread, frames=tuple(expanded))

        self._gate(controller)
        if (
            controller.state.phase is not phase
            or getattr(controller.state, "generation", None) != generation
        ):
            from tdb.session.inspect_service import SessionGateError

            raise SessionGateError("running")
        return RawSnapshot(
            adapter=getattr(controller.profile.adapter, "id", ""),
            platform=self._platform(),
            rust_version=None,
            threads=tuple(collected[thread.id] for thread in threads),
            warnings=tuple(sorted(warnings)),
        )

    async def collect(self, controller: Any) -> RawSnapshot:
        """Fetch a snapshot within one stopped-state deadline."""
        deadline = asyncio.get_running_loop().time() + self.snapshot_timeout
        return await self._collect(controller, deadline=deadline)

    async def _run_probe(self, probe: Any, client: Any) -> ProbeResult | None:
        if probe is None:
            return None
        target = probe.collect if hasattr(probe, "collect") else probe
        result = target(client)
        if inspect.isawaitable(result):
            return await result
        return result

    async def collect_and_analyze(self, controller: Any):
        deadline = asyncio.get_running_loop().time() + self.snapshot_timeout
        raw = await self._collect(controller, deadline=deadline)
        stop_generation = getattr(controller.state, "generation", None)
        probe: ProbeResult | None = None
        warnings = list(raw.warnings)
        evidence_probe = (
            self.probe if self.probe is not None else probe_for_adapter(raw.adapter)
        )
        if evidence_probe is not None and asyncio.get_running_loop().time() < deadline:
            try:
                probe_deadline = min(
                    deadline,
                    asyncio.get_running_loop().time() + self.probe_timeout,
                )
                probe = await self._await_stopped(
                    controller,
                    self._run_probe(evidence_probe, controller.client),
                    deadline=probe_deadline,
                )
            except asyncio.TimeoutError:
                warnings.append(f"probe timed out after {self.probe_timeout:g}s")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                warnings.append(f"probe failed: {exc}")
        self._gate(controller)
        if getattr(controller.state, "generation", None) != stop_generation:
            from tdb.session.inspect_service import SessionGateError

            raise SessionGateError("running")
        if tuple(warnings) != raw.warnings:
            raw = replace(raw, warnings=tuple(sorted(warnings)))
        return analyze(raw, probe)

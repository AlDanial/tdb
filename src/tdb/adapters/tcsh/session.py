"""Lifecycle ownership for one instrumented tcsh debug session."""

from __future__ import annotations

import asyncio
import codecs
import errno
import os
import signal
import stat
import sys
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from tdb.adapters.tcsh.breakpoints import BoundBreakpoint, bind_breakpoints
from tdb.adapters.tcsh.guardian import process_table_snapshot
from tdb.adapters.tcsh.inspect import (
    InspectionError,
    Scope,
    ScopeKind,
    UnknownFrameError,
    Variable,
    VariableStore,
    parse_alias,
    parse_env,
    parse_set,
)
from tdb.adapters.tcsh.instrumenter import InstrumentationResult, Instrumenter, Probe
from tdb.adapters.tcsh.models import StackFrame, StopReason, ThreadInfo
from tdb.adapters.tcsh.runtime import (
    render_inspection_request,
    render_probe,
    render_source_event,
)
from tdb.adapters.tcsh.transport import (
    IncompleteResponseError,
    ProbeEvent,
    ProbeTransport,
    TransportError,
)

_TERMINATE_TIMEOUT_SECONDS = 2.0
_GUARDIAN_CONNECT_TIMEOUT_SECONDS = 30.0
_WORKSPACE_REDACTION = "<adapter-workspace>"
_OUTPUT_CHUNK_BYTES = 64 * 1024
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW


class LaunchError(Exception):
    """A session could not validate, prepare, or launch its debuggee."""


class InvalidStateError(Exception):
    """A debug operation is invalid in the session's current state."""


class EvaluationError(Exception):
    """A live-shell expression could not be evaluated safely."""


@dataclass(frozen=True, slots=True)
class LaunchConfig:
    """Validated inputs needed to launch one tcsh debuggee."""

    program: Path
    args: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    tcsh_path: Path
    stop_on_entry: bool
    external_terminal: bool = False


class SessionState(str, Enum):
    """The externally observable lifecycle of a debug session."""

    NEW = "new"
    CONFIGURED = "configured"
    RUNNING = "running"
    STOPPED = "stopped"
    TERMINATED = "terminated"


class RunMode(str, Enum):
    """How probes are selected after execution resumes."""

    CONTINUE = "continue"
    NEXT = "next"
    STEP_IN = "stepIn"
    STEP_OUT = "stepOut"


class _RootChildGeneration(Enum):
    ORIGINAL = "original"
    REAPED = "reaped"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class SessionEvent:
    """One event emitted by a debug session."""

    kind: str
    body: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    """The textual result of one live-shell evaluation."""

    result: str
    variables_reference: int = 0


EventSink = Callable[[SessionEvent], Awaitable[None]]
ProcessFactory = Callable[..., Awaitable[asyncio.subprocess.Process]]
RunInTerminal = Callable[[list[str], str, dict[str, str]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _SourceActivation:
    source_id: int
    caller: Probe


def should_stop_at_probe(
    mode: RunMode,
    start_depth: int,
    event_depth: int,
    is_breakpoint: bool,
) -> bool:
    """Return whether a probe should stop for the active execution mode."""

    if is_breakpoint:
        return True
    if mode is RunMode.CONTINUE:
        return False
    if mode is RunMode.STEP_IN:
        return True
    if mode is RunMode.NEXT:
        return event_depth <= start_depth
    return event_depth < start_depth


class DebugSession:
    """Prepare, launch, and clean up one owned tcsh process group."""

    def __init__(
        self,
        config: LaunchConfig,
        event_sink: EventSink,
        *,
        process_factory: ProcessFactory = asyncio.create_subprocess_exec,
        run_in_terminal: RunInTerminal | None = None,
    ) -> None:
        self.config = config
        self.state = SessionState.NEW
        self.workspace: Path | None = None
        self._workspace_descriptor: int | None = None
        self.transport: ProbeTransport | None = None
        self.instrumentation: InstrumentationResult | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.failure: BaseException | None = None
        self._process_group_id: int | None = None
        self._process_session_id: int | None = None
        self._guardian_control_descriptor: int | None = None
        self._guardian_status_descriptor: int | None = None
        self._guardian_status_buffer = bytearray()
        self._guardian_status_writer_seen = False
        self._guardian_armed = False
        self._guardian_termination_status: bytes | None = None
        self._guardian_termination_failure: RuntimeError | None = None
        self._guardian_ack_queue: asyncio.Queue[bytes] | None = None
        self._event_sink = event_sink
        self._process_factory = process_factory
        self._run_in_terminal = run_in_terminal
        self._wait_task: asyncio.Task[None] | None = None
        self._probe_task: asyncio.Task[None] | None = None
        self._output_pump_tasks: tuple[asyncio.Task[None], ...] = ()
        self._cleanup_lock = asyncio.Lock()
        self._completion_event = asyncio.Event()
        self._event_lock = asyncio.Lock()
        self._terminate_lock = asyncio.Lock()
        self._guardian_termination_lock = asyncio.Lock()
        self._execution_lock = asyncio.Lock()
        self._exited_attempted = False
        self._terminated_attempted = False
        self._breakpoint_probe_ids: dict[Path, frozenset[int]] = {}
        self._run_mode = RunMode.CONTINUE
        self._step_start_depth = 0
        self._entry_pending = config.stop_on_entry
        self._detach_requested = False
        self._source_stack: list[_SourceActivation] = []
        self._last_probe: Probe | None = None
        self._current_probe: Probe | None = None
        self._frames: tuple[StackFrame, ...] = ()
        self._next_frame_id = 1
        self._variable_store = VariableStore()
        self._inspection_errors: dict[ScopeKind, InspectionError] = {}

    async def prepare(self) -> None:
        """Validate launch inputs and create private instrumented sources."""

        if self.state is not SessionState.NEW:
            raise LaunchError(f"prepare requires NEW state, got {self.state.value}")

        original_program = self.config.program
        program = original_program.resolve(strict=False)
        cwd = self.config.cwd.resolve(strict=False)
        tcsh_path = self.config.tcsh_path.resolve(strict=False)
        self._validate_program(program, original_program)
        self._validate_cwd(cwd, self.config.cwd)
        self._validate_tcsh(tcsh_path, self.config.tcsh_path)
        self.config = LaunchConfig(
            program=program,
            args=tuple(self.config.args),
            cwd=cwd,
            env=dict(self.config.env),
            tcsh_path=tcsh_path,
            stop_on_entry=self.config.stop_on_entry,
            external_terminal=self.config.external_terminal,
        )

        self.workspace = Path(tempfile.mkdtemp(prefix="tcsh-dap-"))
        try:
            self._workspace_descriptor = os.open(self.workspace, _DIRECTORY_OPEN_FLAGS)
            self.transport = ProbeTransport.create(
                self.workspace,
                workspace_descriptor=self._workspace_descriptor,
            )
            paths = self.transport.paths
            instrumenter = Instrumenter(
                workspace=self.workspace,
                cwd=self.config.cwd,
                original_argv0=str(self.config.program),
                probe_renderer=lambda probe_id: render_probe(probe_id, paths),
                source_event_renderer=lambda kind, source_id: render_source_event(
                    kind, source_id, paths
                ),
            )
            self.instrumentation = instrumenter.instrument(self.config.program)
        except asyncio.CancelledError as error:
            await self._cleanup(primary_error=error)
            raise
        except BaseException as error:
            await self._cleanup(primary_error=error)
            raise LaunchError(
                f"Could not prepare program {original_program}: {error}"
            ) from error
        self.state = SessionState.CONFIGURED

    async def start(self) -> None:
        """Spawn tcsh with adapter stdin isolated from the debuggee."""

        if self.state is not SessionState.CONFIGURED:
            raise LaunchError(
                f"start requires CONFIGURED state, got {self.state.value}"
            )
        assert self.instrumentation is not None

        environment = dict(os.environ)
        environment.update(self.config.env)
        environment["__tcsh_dap_original_0"] = str(self.config.program)
        tcsh_argv = (
            str(self.config.tcsh_path),
            "-f",
            str(self.instrumentation.root),
            *self.config.args,
        )
        guardian = Path(__file__).with_name("guardian.py")
        status_writer: int | None = None
        control_reader: int | None = None
        try:
            if self.config.external_terminal:
                assert self._run_in_terminal is not None
                assert self.workspace is not None
                status_path = self.workspace / "guardian-status.fifo"
                control_path = self.workspace / "guardian-control.fifo"
                os.mkfifo(status_path, 0o600)
                os.mkfifo(control_path, 0o600)
                # Open our ends FIRST: status read non-blocking (matches the
                # pipe's set_blocking(False)) and control O_RDWR so the
                # guardian's own read-open of the control FIFO can never
                # block on us, and the control channel never spuriously
                # EOFs while the session lives -- exactly like the pipe it
                # replaces.
                self._guardian_status_descriptor = os.open(
                    status_path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC
                )
                self._guardian_control_descriptor = os.open(
                    control_path, os.O_RDWR | os.O_CLOEXEC
                )
                control_writer = self._guardian_control_descriptor
                self._guardian_ack_queue = asyncio.Queue()
                argv = (
                    sys.executable,
                    str(guardian),
                    "--status-path",
                    str(status_path),
                    "--control-path",
                    str(control_path),
                    "--",
                    *tcsh_argv,
                )
                await self._run_in_terminal(
                    list(argv), str(self.config.cwd), environment
                )
            else:
                status_reader, status_writer = os.pipe()
                self._guardian_status_descriptor = status_reader
                control_reader, control_writer = os.pipe()
                self._guardian_control_descriptor = control_writer
                os.set_blocking(status_reader, False)
                argv = (
                    sys.executable,
                    str(guardian),
                    "--status-fd",
                    str(status_writer),
                    "--control-fd",
                    str(control_reader),
                    "--",
                    *tcsh_argv,
                )
                try:
                    self.process = await self._process_factory(
                        *argv,
                        cwd=str(self.config.cwd),
                        env=environment,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        start_new_session=True,
                        pass_fds=(status_writer, control_reader),
                    )
                finally:
                    if status_writer is not None:
                        os.close(status_writer)
                        status_writer = None
                    if control_reader is not None:
                        os.close(control_reader)
                        control_reader = None
                self._process_group_id = self.process.pid
                self._process_session_id = self.process.pid
            status = await self._read_guardian_status()
            if status != b"armed\n":
                detail = status.decode(errors="replace").removeprefix("error ").strip()
                message = detail or "guardian exited before arming startup"
                if self.config.external_terminal:
                    message += " — did the external terminal window open?"
                raise OSError(message)
            self._guardian_armed = True
            os.write(control_writer, b"start\n")
            status = await self._read_guardian_status()
            if status != b"ok\n":
                detail = status.decode(errors="replace").removeprefix("error ").strip()
                message = detail or "guardian exited before launching tcsh"
                if self.config.external_terminal:
                    message += " — did the external terminal window open?"
                raise OSError(message)
            if self.config.external_terminal:
                pid_line = await self._read_guardian_status()
                if not pid_line.startswith(b"pid "):
                    raise OSError("guardian did not report its pid")
                guardian_pid = int(pid_line.split()[1])
                self._process_group_id = guardian_pid
                self._process_session_id = guardian_pid
        except asyncio.CancelledError as error:
            self.state = SessionState.TERMINATED
            try:
                await self._abort_launched_process()
                await self._cleanup(primary_error=error)
            finally:
                self._completion_event.set()
            raise
        except BaseException as error:
            self.failure = error
            self.state = SessionState.TERMINATED
            try:
                await self._abort_launched_process()
                await self._cleanup(primary_error=error)
            finally:
                self._completion_event.set()
            raise LaunchError(
                f"Could not launch program {self.config.program}: {error}"
            ) from error
        finally:
            if status_writer is not None:
                os.close(status_writer)
            if control_reader is not None:
                os.close(control_reader)

        self.state = SessionState.RUNNING
        self._probe_task = asyncio.create_task(self._run_probe_events())
        self._wait_task = asyncio.create_task(
            self._monitor_process()
            if self.process is not None
            else self._monitor_terminal()
        )

    async def _abort_launched_process(self) -> None:
        process = self.process
        if process is None or process.returncode is not None:
            return
        if not self._guardian_armed:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(
                    process.wait(), timeout=_TERMINATE_TIMEOUT_SECONDS + 1
                )
            except TimeoutError:
                pass
            return
        try:
            await self._request_guardian_termination()
        except (BrokenPipeError, RuntimeError, TimeoutError):
            pass
        try:
            await asyncio.wait_for(
                process.wait(), timeout=_TERMINATE_TIMEOUT_SECONDS + 1
            )
        except TimeoutError:
            pass

    async def wait(self) -> None:
        """Wait for session completion; cancellation does not abandon ownership."""

        task = self._wait_task
        if task is None:
            if self.state is SessionState.TERMINATED:
                await self._completion_event.wait()
                if self.failure is not None:
                    raise self.failure
                return
            raise LaunchError(
                f"wait requires RUNNING or TERMINATED state, got {self.state.value}"
            )
        await asyncio.shield(task)
        if self.failure is not None:
            raise self.failure

    def set_breakpoints(
        self,
        path: Path,
        lines: Sequence[int],
    ) -> tuple[BoundBreakpoint, ...]:
        """Replace and bind line breakpoints for one original source file."""

        if self.state in {SessionState.NEW, SessionState.TERMINATED}:
            raise InvalidStateError(
                "set_breakpoints requires a prepared active session"
            )
        assert self.instrumentation is not None
        canonical = path.resolve(strict=False)
        bound = bind_breakpoints(self.instrumentation.source_map, canonical, lines)
        self._breakpoint_probe_ids[canonical] = frozenset(
            item.probe_id
            for item in bound
            if item.verified and item.probe_id is not None
        )
        return bound

    async def continue_(self) -> None:
        """Resume until a bound breakpoint or process termination."""

        await self._request_resume(RunMode.CONTINUE)

    async def next(self) -> None:
        """Resume to the next probe at the current or a shallower source depth."""

        await self._request_resume(RunMode.NEXT)

    async def step_in(self) -> None:
        """Resume to the next encountered probe at any source depth."""

        await self._request_resume(RunMode.STEP_IN)

    async def step_out(self) -> None:
        """Resume until execution returns to the caller source depth."""

        await self._request_resume(RunMode.STEP_OUT)

    async def detach(self) -> None:
        """Disable debugger stops and let the owned process finish naturally."""

        task = asyncio.create_task(self._detach())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(task)
            raise

    async def _detach(self) -> None:
        async with self._execution_lock:
            if self._detach_requested:
                return
            if self.state is SessionState.TERMINATED:
                await self._completion_event.wait()
                self._detach_requested = True
                return
            if self.state is SessionState.CONFIGURED:
                await self._detach_configured()
                self._detach_requested = True
                return
            if self.state not in {SessionState.RUNNING, SessionState.STOPPED}:
                raise InvalidStateError(
                    "detach requires a configured, running, or stopped session"
                )
            self._entry_pending = False
            self._breakpoint_probe_ids.clear()
            self._run_mode = RunMode.CONTINUE
            if self.state is SessionState.RUNNING:
                self._detach_requested = True
                return
            assert self.transport is not None
            self._invalidate_stop_data()
            self.state = SessionState.RUNNING
            try:
                await self.transport.release()
                await self._emit(
                    SessionEvent(
                        "continued", {"threadId": 1, "allThreadsContinued": True}
                    )
                )
                self._detach_requested = True
            except BaseException as error:
                self.failure = error
                try:
                    await self._stop_process_group()
                except BaseException as termination_error:  # noqa: BLE001
                    error.add_note(
                        f"process termination also failed: {termination_error}"
                    )
                raise

    async def _detach_configured(self) -> None:
        self.state = SessionState.TERMINATED
        primary_error: BaseException | None = None
        try:
            await self._emit_terminated()
        except BaseException as error:  # noqa: BLE001
            self.failure = error
            primary_error = error
        try:
            await self._cleanup(primary_error=primary_error)
        except BaseException as error:  # noqa: BLE001
            if primary_error is None:
                self.failure = error
                primary_error = error
        finally:
            self._completion_event.set()
        if primary_error is not None:
            raise primary_error

    def threads(self) -> tuple[ThreadInfo, ...]:
        """Return the adapter's single logical tcsh thread."""

        return (ThreadInfo(1, "tcsh"),)

    def stack_trace(self) -> tuple[StackFrame, ...]:
        """Return stable, newest-first original-source frames for this stop."""

        if self.state is not SessionState.STOPPED:
            raise InvalidStateError("stack_trace requires a stopped session")
        return self._frames

    def scopes(self, frame_id: int) -> tuple[Scope, ...]:
        """Return the four live-shell scopes for a current stack frame."""

        if self.state is not SessionState.STOPPED:
            raise InvalidStateError("scopes requires a stopped session")
        return self._variable_store.scopes(frame_id)

    async def variables(self, reference: int) -> tuple[Variable, ...]:
        """Fetch and cache one live-shell scope for the current stop."""

        task = asyncio.create_task(self._variables(reference))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(task)
            raise

    async def evaluate(
        self,
        expression: str,
        frame_id: int | None,
    ) -> EvaluationResult:
        """Execute an expression directly in the shell retained at a stop."""

        task = asyncio.create_task(self._evaluate(expression, frame_id))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(task)
            raise

    async def _evaluate(
        self,
        expression: str,
        frame_id: int | None,
    ) -> EvaluationResult:
        async with self._execution_lock:
            if self.state is not SessionState.STOPPED:
                raise InvalidStateError("evaluate requires a stopped session")
            if frame_id is not None and all(
                frame.id != frame_id for frame in self._frames
            ):
                raise UnknownFrameError(f"unknown frame ID: {frame_id}")
            if "\x00" in expression:
                raise EvaluationError("evaluation expression cannot contain NUL")
            assert self.transport is not None
            try:
                output = await self.transport.send_request(
                    lambda request_id: render_inspection_request(
                        request_id,
                        expression,
                        self.transport.paths,
                    )
                )
            except IncompleteResponseError as error:
                evaluation_error = EvaluationError(f"evaluation failed: {error}")
                self.failure = evaluation_error
                self._invalidate_stop_data()
                try:
                    await self._stop_process_group()
                    if self._wait_task is not None:
                        await asyncio.shield(self._wait_task)
                except BaseException as termination_error:
                    if self.failure is not evaluation_error:
                        raise
                    evaluation_error.add_note(
                        f"session termination also failed: {termination_error}"
                    )
                raise evaluation_error from error
            except (TimeoutError, TransportError) as error:
                detail = "timed out" if isinstance(error, TimeoutError) else str(error)
                raise EvaluationError(
                    f"evaluation failed: {detail or type(error).__name__}"
                ) from error
            return EvaluationResult(output)

    async def _variables(self, reference: int) -> tuple[Variable, ...]:
        self._variable_store.scope_kind(reference)
        async with self._execution_lock:
            kind = self._variable_store.scope_kind(reference)
            if self.state is not SessionState.STOPPED:
                raise InvalidStateError("variables requires a stopped session")
            dependency = ScopeKind.SHELL if kind is ScopeKind.ARGUMENTS else kind
            cached_error = self._inspection_errors.get(dependency)
            if cached_error is not None:
                raise cached_error
            if not self._variable_store.is_loaded(kind):
                await self._load_variables(dependency)
            return self._variable_store.variables(reference)

    async def _load_variables(self, kind: ScopeKind) -> None:
        assert self.transport is not None
        command = kind.value
        try:
            output = await self.transport.send_request(
                lambda request_id: render_inspection_request(
                    request_id,
                    command,
                    self.transport.paths,
                )
            )
            if kind is ScopeKind.SHELL:
                self._variable_store.cache_set(parse_set(output))
            elif kind is ScopeKind.ENVIRONMENT:
                self._variable_store.cache_environment(parse_env(output))
            elif kind is ScopeKind.ALIASES:
                self._variable_store.cache_aliases(parse_alias(output))
            else:
                raise AssertionError(f"unsupported inspection scope: {kind}")
        except InspectionError as error:
            self._inspection_errors[kind] = error
            raise
        except IncompleteResponseError as error:
            inspection_error = InspectionError(f"{command} inspection failed: {error}")
            self.failure = inspection_error
            self._invalidate_stop_data()
            try:
                await self._stop_process_group()
                if self._wait_task is not None:
                    await asyncio.shield(self._wait_task)
            except BaseException as termination_error:
                if self.failure is not inspection_error:
                    raise
                inspection_error.add_note(
                    f"session termination also failed: {termination_error}"
                )
            raise inspection_error from error
        except (TimeoutError, TransportError) as error:
            detail = "timed out" if isinstance(error, TimeoutError) else str(error)
            inspection_error = InspectionError(
                f"{command} inspection failed: {detail or type(error).__name__}"
            )
            self._inspection_errors[kind] = inspection_error
            raise inspection_error from error

    async def _request_resume(self, mode: RunMode) -> None:
        task = asyncio.create_task(self._resume(mode))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(task)
            raise

    async def _resume(self, mode: RunMode) -> None:
        async with self._execution_lock:
            if self.state is not SessionState.STOPPED:
                raise InvalidStateError("execution requests require a stopped session")
            if mode is RunMode.STEP_OUT and not self._source_stack:
                raise InvalidStateError("cannot step out from the root source frame")
            assert self.transport is not None
            self._run_mode = mode
            self._step_start_depth = len(self._source_stack)
            self._invalidate_stop_data()
            self.state = SessionState.RUNNING
            try:
                await self.transport.release()
                await self._emit(
                    SessionEvent(
                        "continued", {"threadId": 1, "allThreadsContinued": True}
                    )
                )
            except BaseException as error:
                self.failure = error
                try:
                    await self._stop_process_group()
                except BaseException as termination_error:  # noqa: BLE001
                    error.add_note(
                        f"process termination also failed: {termination_error}"
                    )
                raise

    async def _run_probe_events(self) -> None:
        assert self.transport is not None
        try:
            while self.state is not SessionState.TERMINATED:
                event = await self.transport.next_event()
                await self._handle_runtime_event(event)
        except asyncio.CancelledError:
            raise
        except TransportError as error:
            if self.state is SessionState.TERMINATED:
                return
            self.failure = error
            await self._stop_process_group()
        except BaseException as error:  # noqa: BLE001
            self.failure = error
            await self._stop_process_group()

    async def _handle_runtime_event(self, event: ProbeEvent) -> None:
        if event.kind == "enter":
            source_id = event.source_id
            caller = self._last_probe
            if source_id is None or caller is None:
                raise TransportError("source enter has no matching caller probe")
            self._source_stack.append(_SourceActivation(source_id, caller))
            return
        if event.kind == "leave":
            source_id = event.source_id
            if not self._source_stack or self._source_stack[-1].source_id != source_id:
                raise TransportError(f"unmatched source leave: {source_id}")
            self._source_stack.pop()
            return
        probe_id = event.probe_id
        if probe_id is None:
            raise TransportError("probe event is missing its probe ID")
        async with self._execution_lock:
            await self._handle_probe(probe_id)

    async def _handle_probe(self, probe_id: int) -> None:
        assert self.instrumentation is not None
        assert self.transport is not None
        try:
            probe = self.instrumentation.source_map.probe(probe_id)
        except KeyError as error:
            raise TransportError(f"unknown probe ID: {probe_id}") from error
        event_depth = len(self._source_stack)
        self._last_probe = probe
        reason: StopReason | None = None
        if self._entry_pending and event_depth == 0:
            self._entry_pending = False
            reason = StopReason.ENTRY
        elif probe.id in self._breakpoint_probe_ids.get(probe.span.path, frozenset()):
            reason = StopReason.BREAKPOINT
        elif should_stop_at_probe(
            self._run_mode,
            self._step_start_depth,
            event_depth,
            False,
        ):
            reason = StopReason.STEP

        if reason is None:
            await self.transport.release()
            return
        self._current_probe = probe
        self._frames = self._build_stack_frames(probe)
        self._inspection_errors.clear()
        self._variable_store.begin_stop(tuple(frame.id for frame in self._frames))
        self.state = SessionState.STOPPED
        await self._emit(
            SessionEvent(
                "stopped",
                {
                    "reason": reason.value,
                    "threadId": 1,
                    "allThreadsStopped": True,
                },
            )
        )

    def _build_stack_frames(self, current: Probe) -> tuple[StackFrame, ...]:
        locations = [
            current,
            *(activation.caller for activation in reversed(self._source_stack)),
        ]
        frames: list[StackFrame] = []
        for probe in locations:
            path = probe.span.path.resolve(strict=False)
            frames.append(
                StackFrame(
                    id=self._next_frame_id,
                    name=path.name,
                    path=path,
                    line=probe.span.start_line,
                    end_line=probe.span.end_line,
                )
            )
            self._next_frame_id += 1
        return tuple(frames)

    def _invalidate_stop_data(self) -> None:
        self._inspection_errors.clear()
        self._variable_store.invalidate()
        self._current_probe = None
        self._frames = ()

    async def terminate(self) -> None:
        """Terminate the owned process group and clean all session resources."""

        async with self._terminate_lock:
            if self.state is SessionState.TERMINATED:
                await self._completion_event.wait()
                return
            if self.state is SessionState.NEW:
                raise LaunchError(
                    "terminate requires CONFIGURED, RUNNING, or TERMINATED state"
                )
            if self.state is SessionState.CONFIGURED:
                self.state = SessionState.TERMINATED
                try:
                    await self._emit_terminated()
                except BaseException as error:
                    self.failure = error
                    raise
                finally:
                    try:
                        await self._cleanup(primary_error=self.failure)
                    finally:
                        self._completion_event.set()
                return

            task = self._wait_task
            try:
                await self._stop_process_group()
            except BaseException as error:
                if task is not None:
                    try:
                        await asyncio.shield(task)
                    except BaseException as monitor_error:  # noqa: BLE001
                        if monitor_error is not error:
                            error.add_note(
                                f"session monitor also failed: {monitor_error}"
                            )
                raise
            if task is not None:
                await asyncio.shield(task)

    async def _monitor_process(self) -> None:
        process = self.process
        assert process is not None
        assert process.stdout is not None
        assert process.stderr is not None
        process_wait = asyncio.create_task(process.wait())
        pumps = (
            asyncio.create_task(self._pump_output(process.stdout, "stdout")),
            asyncio.create_task(self._pump_output(process.stderr, "stderr")),
        )
        self._output_pump_tasks = pumps
        tasks = (process_wait, *pumps)
        try:
            await asyncio.gather(*tasks)
            assert process.returncode is not None
            await self._emit_process_termination(process.returncode)
        except BaseException as caught_error:  # noqa: BLE001
            error = self._guardian_termination_failure or caught_error
            self.failure = error
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                returncode = await self._stop_process_group()
                if not self._exited_attempted:
                    await self._emit_process_termination(returncode)
            except BaseException as termination_error:  # noqa: BLE001
                error.add_note(
                    f"terminal event recovery also failed: {termination_error}"
                )
            self.state = SessionState.TERMINATED
            raise error
        finally:
            self.state = SessionState.TERMINATED
            try:
                await self._cleanup(primary_error=self.failure)
            finally:
                self._completion_event.set()

    async def _monitor_terminal(self) -> None:
        """The `_monitor_process()` equivalent for terminal-mode launches,
        where the guardian's status FIFO (not a reaped child process
        object) is the only source of the debuggee's final exit status.

        Interleaved non-exit status lines (e.g. `terminated`/`escalating`/
        `failed ...` acknowledgements from an in-flight
        `_request_guardian_termination`) are forwarded to
        `self._guardian_ack_queue` rather than consumed here -- the
        termination requester reads its own acknowledgement from that
        queue instead of racing this loop for the same descriptor.
        """
        try:
            while True:
                line = await self._read_guardian_status()
                if line.startswith(b"exit "):
                    returncode = int(line.split()[1])
                    break
                if line.startswith(b"signal "):
                    returncode = -int(line.split()[1])
                    break
                if line == b"":
                    returncode = -1  # status FIFO closed with no report
                    break
                assert self._guardian_ack_queue is not None
                await self._guardian_ack_queue.put(line)
            await self._emit_process_termination(returncode)
        except BaseException as error:  # noqa: BLE001
            self.failure = self.failure or error
            self.state = SessionState.TERMINATED
            raise
        finally:
            # _monitor_process's own finally does the same cleanup+wake
            # for the fd-mode path (see above); terminal mode has no
            # process object to gather, but the same workspace/transport
            # teardown and _completion_event wake are required here too --
            # otherwise terminate()'s "already TERMINATED" branch
            # (`await self._completion_event.wait()`) would hang forever
            # once the debuggee exits on its own.
            self.state = SessionState.TERMINATED
            try:
                await self._cleanup(primary_error=self.failure)
            finally:
                self._completion_event.set()

    async def _pump_output(
        self,
        stream: asyncio.StreamReader,
        category: str,
    ) -> None:
        buffer = bytearray()
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        workspace_bytes = (
            str(self.workspace).encode("utf-8", errors="surrogateescape")
            if self.workspace is not None
            else b""
        )
        redaction_bytes = _WORKSPACE_REDACTION.encode()
        retained_suffix = max(0, len(workspace_bytes) - 1)

        async def emit(data: bytes, *, final: bool = False) -> None:
            output = decoder.decode(data, final=final)
            if output:
                await self._emit_output_text(category, output)

        while chunk := await stream.read(64 * 1024):
            buffer.extend(chunk)
            if workspace_bytes:
                buffer[:] = buffer.replace(workspace_bytes, redaction_bytes)
            while True:
                newline = buffer.find(b"\n")
                if newline >= 0:
                    length = min(newline + 1, _OUTPUT_CHUNK_BYTES)
                elif len(buffer) - retained_suffix >= _OUTPUT_CHUNK_BYTES:
                    length = _OUTPUT_CHUNK_BYTES
                else:
                    break
                data = bytes(buffer[:length])
                del buffer[:length]
                await emit(data)
        if workspace_bytes:
            buffer[:] = buffer.replace(workspace_bytes, redaction_bytes)
        while buffer:
            length = min(len(buffer), _OUTPUT_CHUNK_BYTES)
            data = bytes(buffer[:length])
            del buffer[:length]
            await emit(data)
        await emit(b"", final=True)

    async def _emit_output(self, category: str, data: bytes) -> None:
        await self._emit_output_text(category, data.decode("utf-8", errors="replace"))

    async def _emit_output_text(self, category: str, output: str) -> None:
        if self.workspace is not None:
            output = output.replace(str(self.workspace), _WORKSPACE_REDACTION)
        await self._emit(
            SessionEvent("output", {"category": category, "output": output})
        )

    async def _emit_process_termination(self, returncode: int) -> None:
        if not self._exited_attempted:
            self._exited_attempted = True
            await self._emit(SessionEvent("exited", {"exitCode": returncode}))
        self.state = SessionState.TERMINATED
        await self._emit_terminated()

    async def _emit_terminated(self) -> None:
        if self._terminated_attempted:
            return
        self._terminated_attempted = True
        await self._emit(SessionEvent("terminated", {}))

    async def _emit(self, event: SessionEvent) -> None:
        async with self._event_lock:
            await self._event_sink(event)

    async def _stop_process_group(self) -> int:
        process = self.process
        # process is None in terminal mode (the debuggee is owned by the
        # guardian, not spawned as our own child); route on the control
        # descriptor instead of `process is None` so a terminal-mode
        # termination still reaches the guardian. `_process_group_id` holds
        # the guardian's own pid in that mode (see start()), so the
        # killpg fallback elsewhere that keys off it is unaffected.
        if process is None and self._guardian_control_descriptor is None:
            return 0
        if process is None or process.returncode is None:
            try:
                await self._request_guardian_termination()
            except RuntimeError:
                if process is not None:
                    await asyncio.wait_for(
                        process.wait(), timeout=_TERMINATE_TIMEOUT_SECONDS + 1
                    )
                raise
            if process is not None:
                await asyncio.wait_for(
                    process.wait(), timeout=_TERMINATE_TIMEOUT_SECONDS + 1
                )
        if process is None:
            return 0
        assert process.returncode is not None
        return process.returncode

    async def _request_guardian_termination(self) -> bytes:
        async with self._guardian_termination_lock:
            if self._guardian_termination_status is not None:
                if self._guardian_termination_failure is not None:
                    raise self._guardian_termination_failure
                return self._guardian_termination_status
            descriptor = self._guardian_control_descriptor
            if descriptor is None:
                raise RuntimeError("guardian control descriptor is closed")
            try:
                os.write(descriptor, b"terminate\n")
            except OSError:
                # Can't signal the guardian in-band at all -- in terminal
                # mode this is the only place with any independent OS-level
                # handle on it (self._process_group_id, adopted from the
                # guardian's own "pid " status line during the handshake),
                # so fall it back to a direct SIGKILL rather than leaving
                # the debuggee to run unsupervised. Still re-raise: callers
                # (e.g. _stop_process_group) need to know the clean in-band
                # handshake didn't happen.
                self._force_kill_terminal_guardian()
                raise
            try:
                # In terminal mode _monitor_terminal() owns the status
                # descriptor (it's the one reading the guardian's status
                # lines to learn the debuggee's final exit/signal code), so
                # reading here directly would race it for the same bytes.
                # It forwards every non-exit line (including this
                # acknowledgement) onto the queue instead.
                if self._guardian_ack_queue is not None:
                    status = await asyncio.wait_for(
                        self._guardian_ack_queue.get(),
                        timeout=_TERMINATE_TIMEOUT_SECONDS + 1,
                    )
                else:
                    status = await asyncio.wait_for(
                        self._read_guardian_status(),
                        timeout=_TERMINATE_TIMEOUT_SECONDS + 1,
                    )
            except TimeoutError as error:
                # The in-band acknowledgement never arrived -- same
                # last-resort force-kill as the write failure above (see
                # its comment); the guardian may simply be wedged or the
                # client-spawned process tree may be unresponsive.
                self._force_kill_terminal_guardian()
                raise TimeoutError(
                    "guardian termination acknowledgement timed out"
                ) from error
            if status not in {b"terminated\n", b"escalating\n"}:
                detail = status.decode(errors="replace").strip()
                failure = RuntimeError(
                    f"guardian termination failed: {detail or 'closed status pipe'}"
                )
                self._guardian_termination_status = status
                self._guardian_termination_failure = failure
                for pump in self._output_pump_tasks:
                    pump.cancel()
                raise failure
            self._guardian_termination_status = status
            return status

    def _force_kill_terminal_guardian(self) -> None:
        """Last-resort SIGKILL of the guardian's own process group, used
        only when the in-band control-FIFO route has just failed (the
        `terminate\\n` write itself raised, or the acknowledgement never
        arrived) -- see the two call sites in _request_guardian_termination.

        Terminal mode only: `self._process_group_id` holds the *adopted
        guardian pid* there (start()'s handshake reads a `pid <pid>` status
        line and assigns it), and the guardian is its own session/
        process-group leader (it never calls setsid/setpgid itself, but
        whatever spawned it -- a real terminal emulator, or this task's
        test harness passing start_new_session=True -- put it in a fresh
        one), so killpg reaches it directly even though the adapter never
        held it as a reapable child. Pipe mode has `self.process` (a real
        subprocess.Process) to fall back on instead and isn't handled
        here -- this is deliberately narrow, matching the in-band route's
        being tried first and this being reached only on its failure.
        """
        if not self.config.external_terminal:
            return
        process_group_id = self._process_group_id
        if process_group_id is None:
            return
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    async def _read_guardian_status(self) -> bytes:
        deadline: float | None = None
        while True:
            newline = self._guardian_status_buffer.find(b"\n")
            if newline >= 0:
                status = bytes(self._guardian_status_buffer[: newline + 1])
                del self._guardian_status_buffer[: newline + 1]
                return status
            descriptor = self._guardian_status_descriptor
            if descriptor is None:
                return b""
            try:
                chunk = os.read(descriptor, 64)
            except BlockingIOError:
                await asyncio.sleep(0.01)
                continue
            if not chunk:
                if (
                    self._guardian_status_writer_seen
                    or not self.config.external_terminal
                ):
                    # Real EOF: return whatever partial (unterminated) line
                    # is left in the buffer, but clear it first. Without
                    # this, a guardian that dies mid-line (no trailing
                    # newline) would leave that partial data sitting in
                    # the buffer forever -- the next call finds no b"\n"
                    # (line 1165's `find` still misses) and, since a
                    # writer has already been seen, immediately re-hits
                    # this same branch and returns the identical bytes
                    # again, spinning _monitor_terminal on the same
                    # partial line forever instead of ever reporting the
                    # closed status channel (`b""`) that tells it the
                    # guardian is gone.
                    status = bytes(self._guardian_status_buffer)
                    self._guardian_status_buffer.clear()
                    return status
                # Terminal mode only: our own read-open of the status FIFO
                # is non-blocking (start() can't block on the guardian's
                # write-open -- it hasn't even been asked to spawn yet at
                # that point), and on Linux a non-blocking read on a FIFO
                # with NO writer currently connected returns b"" -- the
                # exact same result a real EOF (writer connected, then all
                # writers closed) produces. Until real data has actually
                # been observed, an empty read is ambiguous ("not started
                # yet" vs. "already gone"); assume the former and keep
                # polling, bounded so a guardian that genuinely never
                # connects can't hang start() forever. Once
                # _guardian_status_writer_seen flips True the ambiguity is
                # gone -- a later empty read is unambiguous real EOF.
                loop = asyncio.get_running_loop()
                if deadline is None:
                    deadline = loop.time() + _GUARDIAN_CONNECT_TIMEOUT_SECONDS
                elif loop.time() >= deadline:
                    return bytes(self._guardian_status_buffer)
                await asyncio.sleep(0.01)
                continue
            self._guardian_status_writer_seen = True
            self._guardian_status_buffer.extend(chunk)

    def _close_guardian_descriptors(self) -> None:
        if self.config.external_terminal:
            self._abandon_terminal_guardian()
            return
        for attribute in (
            "_guardian_control_descriptor",
            "_guardian_status_descriptor",
        ):
            descriptor = getattr(self, attribute)
            if descriptor is None:
                continue
            setattr(self, attribute, None)
            try:
                os.close(descriptor)
            except OSError:
                pass

    def _abandon_terminal_guardian(self) -> None:
        """Release the guardian FIFO descriptors for a terminal-mode
        session without ever leaving a still-running, client-spawned
        guardian process permanently blocked inside its own
        `os.open(control_path, os.O_RDONLY)`.

        The guardian may be in one of three states when a terminal-mode
        start() aborts (fails or is cancelled) or the session otherwise
        tears down: (a) it hasn't even reached its own
        `os.open(status_path, os.O_WRONLY)` yet (still starting up), (b)
        it's between that open and its `os.open(control_path,
        os.O_RDONLY)` -- or literally blocked inside the latter -- or (c)
        it's already past both opens and reading its own control loop.
        `self._guardian_control_descriptor` is opened O_RDWR (see
        start()), which means it ALWAYS counts as both a reader and a
        writer for that FIFO from the kernel's point of view -- while it
        stays open, ANY blocking open() of the same path by another
        process (state (b)) is satisfied immediately by our fd being
        present, and a write here is always legal (state (c) has
        something to read it; even (b), not yet reading, still has our fd
        as "a reader" so the write doesn't raise EPIPE). The bug this
        fixes: the previous code closed control THEN status THEN (later,
        via _remove_owned_workspace) unlinked the FIFOs -- so a guardian
        in state (b) whose open() call hadn't happened yet by the time we
        closed our own fd would block forever on a path that, once
        unlinked, can never gain a fresh writer either (unlink doesn't
        wake an already-blocked open(), and no NEW open by path is
        possible once the guardian's own os.open() call resolved the
        directory entry before the unlink -- moot for a fresh call, fatal
        for one already waiting).

        Fix, in order (all synchronous, none of this ever waits on the
        guardian -- an unreachable/never-connecting guardian, state (a),
        must not be able to hang cleanup on our side either):
        1. Best-effort write "terminate\\n" into the still-open control
           descriptor. A guardian already reading its control loop (state
           (c)) sees it directly; one that's about to open (state (b))
           finds it queued the instant it does.
        2. Unlink both FIFO paths now, NOT later in
           _remove_owned_workspace() -- while our control descriptor is
           STILL open. This closes the remaining gap for a guardian in
           state (b) that hasn't reached its control-path open() yet by
           this point: once it does, either our fd is still open (below)
           and satisfies it, or the path is already gone and it gets
           ENOENT -- a crash, not a wedge, "proceeding to its own exit"
           just as ungracefully as state (a) reaching a now-unlinked
           status_path does.
        3. Close status.
        4. Close control LAST -- only after every state above already had
           its chance to either see the terminate command or fail fast.
        """
        control_descriptor = self._guardian_control_descriptor
        if control_descriptor is not None:
            try:
                os.write(control_descriptor, b"terminate\n")
            except OSError:
                pass
        if self.workspace is not None and self._workspace_descriptor is not None:
            for name in ("guardian-status.fifo", "guardian-control.fifo"):
                try:
                    os.unlink(name, dir_fd=self._workspace_descriptor)
                except OSError:
                    pass
        status_descriptor = self._guardian_status_descriptor
        self._guardian_status_descriptor = None
        if status_descriptor is not None:
            try:
                os.close(status_descriptor)
            except OSError:
                pass
        self._guardian_control_descriptor = None
        if control_descriptor is not None:
            try:
                os.close(control_descriptor)
            except OSError:
                pass

    @staticmethod
    def _signal_process_groups(process_groups: set[int], sig: signal.Signals) -> None:
        for process_group_id in process_groups:
            try:
                os.killpg(process_group_id, sig)
            except ProcessLookupError:
                pass

    def _owned_process_group_ids(self) -> set[int]:
        process = self.process
        process_group_id = self._process_group_id
        process_session_id = self._process_session_id
        if process is None or process_group_id is None or process_session_id is None:
            return set()
        generation_before = self._root_child_generation()
        if generation_before is _RootChildGeneration.REAPED:
            return set()
        if generation_before is _RootChildGeneration.UNAVAILABLE:
            return self._validated_live_root_group()
        if (
            generation_before is _RootChildGeneration.ORIGINAL
            and not self._live_root_identity_matches()
        ):
            return set()
        snapshot = process_table_snapshot()
        if snapshot is None:
            return self._validated_live_root_group()
        groups: set[int] = set()
        root_pid_present = False
        for line in snapshot.splitlines():
            fields = line.split()
            if len(fields) < 2:
                continue
            try:
                candidate_pid, candidate_group = map(int, fields[:2])
            except ValueError:
                continue
            if candidate_pid <= 0 or candidate_group <= 0:
                continue
            if candidate_pid == process.pid:
                root_pid_present = True
            try:
                candidate_session = os.getsid(candidate_pid)
            except OSError:
                continue
            if candidate_session == process_session_id:
                groups.add(candidate_group)
        if (
            generation_before is _RootChildGeneration.ORIGINAL
            and not self._live_root_identity_matches()
        ):
            return set()
        generation_after = self._root_child_generation()
        if generation_after is not _RootChildGeneration.ORIGINAL:
            return set()
        if not root_pid_present:
            return set()
        return groups

    def _validated_live_root_group(self) -> set[int]:
        process = self.process
        process_group_id = self._process_group_id
        if process is None or process_group_id is None:
            return set()
        generation_before = self._root_child_generation()
        if generation_before is _RootChildGeneration.REAPED:
            return set()
        if generation_before is _RootChildGeneration.UNAVAILABLE:
            return set()
        if not self._live_root_identity_matches():
            return set()
        if self._root_child_generation() is not _RootChildGeneration.ORIGINAL:
            return set()
        return {process_group_id}

    def _live_root_identity_matches(self) -> bool:
        process = self.process
        process_group_id = self._process_group_id
        process_session_id = self._process_session_id
        if process is None or process_group_id is None or process_session_id is None:
            return False
        try:
            live_session = os.getsid(process.pid)
        except OSError:
            return False
        try:
            live_group = os.getpgid(process.pid)
        except OSError:
            return False
        return live_group == process_group_id and live_session == process_session_id

    def _root_child_generation(self) -> _RootChildGeneration:
        process = self.process
        if process is None:
            return _RootChildGeneration.UNAVAILABLE
        try:
            waitid = os.waitid
            idtype = os.P_PID
            options = os.WEXITED | os.WNOHANG | os.WNOWAIT
        except AttributeError:
            return _RootChildGeneration.UNAVAILABLE
        try:
            result = waitid(idtype, process.pid, options)
        except ChildProcessError:
            return _RootChildGeneration.REAPED
        except OSError as error:
            if error.errno == errno.ECHILD:
                return _RootChildGeneration.REAPED
            return _RootChildGeneration.UNAVAILABLE
        if result is not None:
            return _RootChildGeneration.REAPED
        return _RootChildGeneration.ORIGINAL

    async def _cleanup(self, primary_error: BaseException | None = None) -> None:
        async with self._cleanup_lock:
            async with self._guardian_termination_lock:
                self._close_guardian_descriptors()
            restore_error: BaseException | None = None
            try:
                self._restore_workspace_access()
            except BaseException as error:  # noqa: BLE001
                restore_error = error
            transport_error: BaseException | None = None
            if self.transport is not None:
                try:
                    await self.transport.close()
                except BaseException as error:  # noqa: BLE001
                    transport_error = error
            try:
                self._remove_owned_workspace()
            except BaseException as removal_error:
                if primary_error is not None:
                    if restore_error is not None:
                        primary_error.add_note(
                            f"workspace access restoration also failed: {restore_error}"
                        )
                    if transport_error is not None:
                        primary_error.add_note(
                            f"transport close also failed: {transport_error}"
                        )
                    primary_error.add_note(
                        f"workspace cleanup also failed: {removal_error}"
                    )
                    return
                if restore_error is not None:
                    removal_error.add_note(
                        f"workspace access restoration also failed: {restore_error}"
                    )
                if transport_error is not None:
                    removal_error.add_note(
                        f"transport close also failed: {transport_error}"
                    )
                raise
            cleanup_error = restore_error or transport_error
            if cleanup_error is not None:
                if restore_error is not None and transport_error is not None:
                    cleanup_error.add_note(
                        f"transport close also failed: {transport_error}"
                    )
                if primary_error is not None:
                    primary_error.add_note(
                        f"session cleanup also failed: {cleanup_error}"
                    )
                    return
                self.failure = cleanup_error
                raise cleanup_error

    def _restore_workspace_access(self) -> None:
        workspace = self.workspace
        descriptor = self._workspace_descriptor
        if workspace is None or descriptor is None:
            return
        descriptor_status = os.fstat(descriptor)
        try:
            path_status = workspace.stat(follow_symlinks=False)
        except FileNotFoundError:
            return
        if (
            not stat.S_ISDIR(path_status.st_mode)
            or path_status.st_dev != descriptor_status.st_dev
            or path_status.st_ino != descriptor_status.st_ino
        ):
            raise RuntimeError("owned workspace path identity changed")
        os.fchmod(descriptor, 0o700)

    def _remove_owned_workspace(self) -> None:
        workspace = self.workspace
        descriptor = self._workspace_descriptor
        if workspace is None or descriptor is None:
            return
        try:
            try:
                path_status = workspace.stat(follow_symlinks=False)
            except FileNotFoundError:
                return
            descriptor_status = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(path_status.st_mode)
                or path_status.st_dev != descriptor_status.st_dev
                or path_status.st_ino != descriptor_status.st_ino
            ):
                raise RuntimeError("refusing to remove a replaced workspace path")
            self._clear_directory(descriptor)
            os.rmdir(workspace)
        finally:
            os.close(descriptor)
            self._workspace_descriptor = None

    @classmethod
    def _clear_directory(cls, descriptor: int) -> None:
        for entry in os.scandir(descriptor):
            if not entry.is_dir(follow_symlinks=False):
                os.unlink(entry.name, dir_fd=descriptor)
                continue
            os.chmod(
                entry.name,
                0o700,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            child_descriptor = os.open(
                entry.name,
                _DIRECTORY_OPEN_FLAGS,
                dir_fd=descriptor,
            )
            try:
                cls._clear_directory(child_descriptor)
            finally:
                os.close(child_descriptor)
            os.rmdir(entry.name, dir_fd=descriptor)

    @staticmethod
    def _validate_program(program: Path, original: Path) -> None:
        if not program.exists():
            raise LaunchError(f"Program does not exist: {original}")
        if not program.is_file():
            raise LaunchError(f"Program is not a regular file: {original}")
        if not os.access(program, os.R_OK):
            raise LaunchError(f"Program is not readable: {original}")

    @staticmethod
    def _validate_cwd(cwd: Path, original: Path) -> None:
        if not cwd.exists() or not cwd.is_dir():
            raise LaunchError(f"Working directory does not exist: {original}")

    @staticmethod
    def _validate_tcsh(tcsh_path: Path, original: Path) -> None:
        if not tcsh_path.is_file() or not os.access(tcsh_path, os.X_OK):
            raise LaunchError(f"tcsh is not executable: {original}")

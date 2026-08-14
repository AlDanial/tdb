"""Private FIFO ownership and framing for an instrumented tcsh process."""

from __future__ import annotations

import asyncio
import errno
import os
import re
import secrets
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

_FIFO_MODE = 0o600
_WORKSPACE_MODE = 0o700
_CONTROL_OPEN_RETRY_SECONDS = 0.005
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW


class TransportError(Exception):
    """The private runtime channel violated its lifecycle or wire protocol."""


class RuntimeResponseError(TransportError):
    """The runtime reported an error payload for an inspection request."""

    def __init__(self, payload: str) -> None:
        self.payload = payload
        super().__init__(payload)


class IncompleteResponseError(TransportError):
    """A request exchanged partial bytes and cannot be retried safely."""


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """Runtime endpoints and framing nonce for one debug session."""

    event_fifo: Path
    control_fifo: Path
    response_fifo: Path
    nonce: str
    workspace_descriptor: int | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ProbeEvent:
    """One location or source-stack event from the instrumented shell."""

    kind: Literal["probe", "enter", "leave"]
    probe_id: int | None
    source_id: int | None


@dataclass(slots=True)
class _PersistentReader:
    path: Path
    descriptor: int
    keeper_descriptor: int
    buffer: bytearray = field(default_factory=bytearray)


@dataclass(frozen=True, slots=True)
class _ResponseFrame:
    request_id: int
    status: bytes
    payload: bytes


@dataclass(slots=True)
class _ResponseReadState:
    consumed: bool = False


@dataclass(slots=True)
class _RequestLifecycle:
    body_written: bool = False
    submission_state: Literal["zero", "indeterminate", "submitted"] = "zero"
    response_task: asyncio.Task[_ResponseFrame] | None = None
    response_state: _ResponseReadState | None = None
    response_complete: bool = False
    request_owner: object | None = None
    caller_abandoned: bool = False


class ProbeTransport:
    """Own one session's FIFOs and coordinate probe handshakes."""

    def __init__(
        self,
        paths: RuntimePaths,
        event_reader: _PersistentReader,
        response_reader: _PersistentReader,
        fifo_identities: dict[str, tuple[int, int]],
        control_pin_descriptor: int | None = None,
    ) -> None:
        self.paths = paths
        self._event_reader: _PersistentReader | None = event_reader
        self._response_reader: _PersistentReader | None = response_reader
        self._fifo_identities = fifo_identities
        self._control_pin_descriptor = control_pin_descriptor
        self._control_writer: int | None = None
        self._next_request_id = 1
        self._completed_response_ids: set[int] = set()
        self._io_waiters: set[asyncio.Future[None]] = set()
        self._event_record_task: asyncio.Task[ProbeEvent] | None = None
        self._response_frame_task: asyncio.Task[_ResponseFrame] | None = None
        self._response_frame_state: _ResponseReadState | None = None
        self._request_tasks: set[asyncio.Task[str]] = set()
        self._request_owners: dict[int, object] = {}
        self._fatal_error: BaseException | None = None
        self._closed = False
        self._event_read_lock = asyncio.Lock()
        self._response_read_lock = asyncio.Lock()
        self._control_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()

    @classmethod
    def create(
        cls,
        workspace: Path,
        *,
        workspace_descriptor: int | None = None,
    ) -> ProbeTransport:
        """Create private session FIFOs below *workspace*."""

        cls._require_descriptor_relative_apis()
        if workspace_descriptor is None:
            try:
                workspace.mkdir(mode=_WORKSPACE_MODE, parents=True)
            except FileExistsError:
                pass
            try:
                owned_workspace_descriptor = os.open(workspace, _DIRECTORY_OPEN_FLAGS)
            except OSError as error:
                raise TransportError(
                    f"runtime workspace is not a private directory: {workspace}"
                ) from error
        else:
            try:
                owned_workspace_descriptor = os.dup(workspace_descriptor)
            except OSError as error:
                raise TransportError(
                    "runtime workspace descriptor is unavailable"
                ) from error

        paths = RuntimePaths(
            event_fifo=workspace / "events.fifo",
            control_fifo=workspace / "control.fifo",
            response_fifo=workspace / "responses.fifo",
            nonce=secrets.token_hex(16),
            workspace_descriptor=owned_workspace_descriptor,
        )
        created: dict[str, tuple[int, int]] = {}
        opened: list[_PersistentReader] = []
        control_pin_descriptor: int | None = None
        try:
            os.fchmod(owned_workspace_descriptor, _WORKSPACE_MODE)
            _verify_workspace_path_identity(workspace, owned_workspace_descriptor)
            for fifo in (paths.event_fifo, paths.control_fifo, paths.response_fifo):
                os.mkfifo(fifo.name, _FIFO_MODE, dir_fd=owned_workspace_descriptor)
                entry_status = os.stat(
                    fifo.name,
                    dir_fd=owned_workspace_descriptor,
                    follow_symlinks=False,
                )
                if not stat.S_ISFIFO(entry_status.st_mode):
                    raise TransportError(f"runtime FIFO identity changed: {fifo}")
                identity = (entry_status.st_dev, entry_status.st_ino)
                created[fifo.name] = identity
                descriptor = cls._open_owned_fifo(
                    fifo,
                    owned_workspace_descriptor,
                    os.O_RDONLY | os.O_NONBLOCK,
                    identity,
                )
                try:
                    os.fchmod(descriptor, _FIFO_MODE)
                    cls._verify_owned_fifo(
                        fifo,
                        owned_workspace_descriptor,
                        descriptor,
                        identity,
                    )
                finally:
                    cls._close_descriptor(descriptor)
            control_pin_descriptor = cls._pin_owned_fifo(
                paths.control_fifo,
                owned_workspace_descriptor,
                created[paths.control_fifo.name],
            )
            event_reader = cls._open_persistent_reader(
                paths.event_fifo,
                owned_workspace_descriptor,
                created[paths.event_fifo.name],
            )
            opened.append(event_reader)
            response_reader = cls._open_persistent_reader(
                paths.response_fifo,
                owned_workspace_descriptor,
                created[paths.response_fifo.name],
            )
            opened.append(response_reader)
        except BaseException as error:
            for persistent_reader in reversed(opened):
                cls._close_descriptor(persistent_reader.descriptor)
                cls._close_descriptor(persistent_reader.keeper_descriptor)
            if control_pin_descriptor is not None:
                cls._close_descriptor(control_pin_descriptor)
            for basename, identity in reversed(tuple(created.items())):
                try:
                    cls._unlink_owned_fifo(
                        basename,
                        owned_workspace_descriptor,
                        identity,
                    )
                except OSError as cleanup_error:
                    error.add_note(
                        f"runtime FIFO rollback also failed: {cleanup_error}"
                    )
            cls._close_descriptor(owned_workspace_descriptor)
            raise
        return cls(
            paths, event_reader, response_reader, created, control_pin_descriptor
        )

    @staticmethod
    def _require_descriptor_relative_apis() -> None:
        if (
            not {os.mkfifo, os.open, os.stat, os.unlink}.issubset(os.supports_dir_fd)
            or os.stat not in os.supports_follow_symlinks
        ):
            raise RuntimeError(
                "runtime transport requires descriptor-relative POSIX file APIs"
            )

    @staticmethod
    def _verify_fifo_status(status: os.stat_result, path: Path) -> None:
        if (
            not stat.S_ISFIFO(status.st_mode)
            or stat.S_IMODE(status.st_mode) != _FIFO_MODE
        ):
            raise TransportError(f"runtime FIFO has unsafe permissions: {path}")

    @classmethod
    def _verify_owned_fifo(
        cls,
        path: Path,
        workspace_descriptor: int,
        descriptor: int,
        identity: tuple[int, int],
    ) -> None:
        descriptor_status = os.fstat(descriptor)
        entry_status = os.stat(
            path.name,
            dir_fd=workspace_descriptor,
            follow_symlinks=False,
        )
        cls._verify_fifo_status(descriptor_status, path)
        cls._verify_fifo_status(entry_status, path)
        if (descriptor_status.st_dev, descriptor_status.st_ino) != identity or (
            entry_status.st_dev,
            entry_status.st_ino,
        ) != identity:
            raise TransportError(f"runtime FIFO identity changed: {path}")

    @classmethod
    def _open_owned_fifo(
        cls,
        path: Path,
        workspace_descriptor: int,
        flags: int,
        identity: tuple[int, int],
    ) -> int:
        try:
            descriptor = os.open(
                path.name,
                flags | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=workspace_descriptor,
            )
        except OSError as error:
            raise TransportError(f"could not open runtime FIFO: {path}") from error
        try:
            cls._verify_owned_fifo(path, workspace_descriptor, descriptor, identity)
        except BaseException:
            cls._close_descriptor(descriptor)
            raise
        return descriptor

    @classmethod
    def _pin_owned_fifo(
        cls,
        path: Path,
        workspace_descriptor: int,
        identity: tuple[int, int],
    ) -> int | None:
        """Hold the FIFO's inode open for the transport's lifetime.

        The (st_dev, st_ino) identity comparisons are only sound while
        the original inode cannot be recycled: filesystems such as
        overlayfs reuse a freed inode number immediately, so an
        unlink-and-recreate would otherwise be indistinguishable from
        the FIFO we created. O_PATH carries no FIFO reader/writer
        semantics; where it does not exist (e.g. macOS) the identity
        checks keep their existing best-effort behavior. The event and
        response FIFOs need no pin — their persistent readers already
        hold their inodes open.
        """

        pin_flags = getattr(os, "O_PATH", 0)
        if not pin_flags:
            return None
        try:
            descriptor = os.open(
                path.name,
                pin_flags | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=workspace_descriptor,
            )
        except OSError as error:
            raise TransportError(f"could not open runtime FIFO: {path}") from error
        try:
            status = os.fstat(descriptor)
            if (
                not stat.S_ISFIFO(status.st_mode)
                or (
                    status.st_dev,
                    status.st_ino,
                )
                != identity
            ):
                raise TransportError(f"runtime FIFO identity changed: {path}")
        except BaseException:
            cls._close_descriptor(descriptor)
            raise
        return descriptor

    @staticmethod
    def _unlink_owned_fifo(
        basename: str,
        workspace_descriptor: int,
        identity: tuple[int, int],
    ) -> None:
        try:
            status = os.stat(
                basename,
                dir_fd=workspace_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        if stat.S_ISFIFO(status.st_mode) and (status.st_dev, status.st_ino) == identity:
            os.unlink(basename, dir_fd=workspace_descriptor)

    async def next_event(self) -> ProbeEvent:
        """Read the next runtime event and establish a probe control writer."""

        self._ensure_open()
        async with self._event_read_lock:
            task = self._event_record_task
            if task is None:
                task = asyncio.create_task(self._read_event_record())
                self._event_record_task = task
            try:
                event = await asyncio.shield(task)
            except asyncio.CancelledError:
                if self._closed and task.cancelled():
                    raise TransportError("transport is closed") from None
                raise
            except BaseException:
                if self._event_record_task is task:
                    self._event_record_task = None
                raise
            else:
                if self._event_record_task is task:
                    self._event_record_task = None
                return event

    async def _read_event_record(self) -> ProbeEvent:
        reader = await self._ensure_event_reader()
        line = await self._readline(reader, timeout=None)
        try:
            record = line.decode("ascii")
        except UnicodeDecodeError as error:
            raise TransportError("event record is not ASCII") from error
        match = re.fullmatch(r"([PEL]) ([0-9]+)\n", record)
        if match is None:
            raise TransportError(f"malformed event record: {record!r}")

        marker, identifier_text = match.groups()
        identifier = int(identifier_text)
        if marker == "P":
            await self._open_control_writer()
            return ProbeEvent("probe", probe_id=identifier, source_id=None)
        if marker == "E":
            return ProbeEvent("enter", probe_id=None, source_id=identifier)
        return ProbeEvent("leave", probe_id=None, source_id=identifier)

    async def send_request(
        self,
        body_factory: Callable[[int], str],
        timeout: float = 5.0,
    ) -> str:
        """Write one serialized control request and return its framed payload."""

        self._ensure_open()
        deadline = time.monotonic() + timeout
        lifecycle = _RequestLifecycle()
        task = asyncio.create_task(
            self._run_request_lifecycle(body_factory, deadline, lifecycle)
        )
        self._request_tasks.add(task)
        task.add_done_callback(self._request_task_done)
        try:
            async with asyncio.timeout(timeout):
                return await asyncio.shield(task)
        except TimeoutError as error:
            incomplete = await self._cancel_abandoned_request_if_safe(task, lifecycle)
            if incomplete:
                raise IncompleteResponseError(
                    "inspection response is incomplete; runtime channel is desynchronized"
                ) from error
            raise
        except asyncio.CancelledError as error:
            if self._closed and task.cancelled():
                raise TransportError("transport is closed") from None
            incomplete = await self._cancel_abandoned_request_if_safe(task, lifecycle)
            if task.done() and not task.cancelled():
                task_error = task.exception()
                if isinstance(task_error, IncompleteResponseError):
                    raise task_error
            if incomplete and not lifecycle.body_written:
                raise IncompleteResponseError(
                    "inspection request is incomplete; runtime channel is desynchronized"
                ) from error
            raise

    async def _run_request_lifecycle(
        self,
        body_factory: Callable[[int], str],
        deadline: float,
        lifecycle: _RequestLifecycle,
    ) -> str:
        async with self._request_lock, self._control_lock:
            self._ensure_open()
            if self._control_writer is None:
                raise TransportError("inspection request requires an active probe")
            request_id = self._next_request_id
            self._next_request_id += 1
            body = body_factory(request_id)
            if not isinstance(body, str):
                raise TypeError("request body factory must return str")
            cleanup = getattr(body, "cleanup", None)
            if callable(cleanup):
                lifecycle.request_owner = body
                self._request_owners[id(body)] = body
            primary_error: BaseException | None = None
            try:
                data = body.encode("utf-8", errors="surrogateescape")
                await self._ensure_response_reader()
                descriptor = self._control_writer
                try:
                    await self._write_all(descriptor, data, deadline, lifecycle)
                except BaseException:
                    if self._control_writer == descriptor:
                        self._close_control_writer()
                    raise
                lifecycle.body_written = True
                return await self._wait_for_response(
                    request_id,
                    timeout=None,
                    lifecycle=lifecycle,
                )
            except BaseException as error:
                primary_error = error
                if (
                    lifecycle.caller_abandoned
                    and lifecycle.body_written
                    and not lifecycle.response_complete
                    and not self._closed
                ):
                    self._fatal_error = error
                if (
                    not self._closed
                    and not lifecycle.body_written
                    and lifecycle.submission_state != "zero"
                    and not isinstance(error, IncompleteResponseError)
                ):
                    incomplete_error = IncompleteResponseError(
                        "inspection request is incomplete; runtime channel is desynchronized"
                    )
                    primary_error = incomplete_error
                    raise incomplete_error from error
                raise
            finally:
                if lifecycle.submission_state == "zero" or lifecycle.response_complete:
                    self._finish_request_owner(lifecycle, primary_error)

    async def read_response(self, request_id: int, timeout: float = 5.0) -> str:
        """Read the response frame for *request_id*, preserving payload text."""

        return await self._wait_for_response(request_id, timeout=timeout)

    async def _wait_for_response(
        self,
        request_id: int,
        *,
        timeout: float | None,
        lifecycle: _RequestLifecycle | None = None,
    ) -> str:
        """Wait for one framed response, optionally owned by a request lifecycle."""

        self._ensure_open()
        if request_id in self._completed_response_ids:
            raise TransportError(f"duplicate response request ID: {request_id}")
        async with asyncio.timeout(timeout):
            async with self._response_read_lock:
                task = self._response_frame_task
                if task is None:
                    state = _ResponseReadState()
                    task = asyncio.create_task(self._read_response_frame(state))
                    self._response_frame_task = task
                    self._response_frame_state = state
                else:
                    state = self._response_frame_state
                    assert state is not None
                if lifecycle is not None:
                    lifecycle.response_task = task
                    lifecycle.response_state = state
                try:
                    frame = await asyncio.shield(task)
                except asyncio.CancelledError:
                    if self._closed and task.cancelled():
                        raise TransportError("transport is closed") from None
                    raise
                except BaseException:
                    if self._response_frame_task is task:
                        self._response_frame_task = None
                        self._response_frame_state = None
                    raise
                else:
                    if self._response_frame_task is task:
                        self._response_frame_task = None
                        self._response_frame_state = None

        actual_id = frame.request_id
        if actual_id != request_id:
            raise TransportError(
                f"response request ID {actual_id} does not match expected {request_id}"
            )
        if actual_id in self._completed_response_ids:
            raise TransportError(f"duplicate response request ID: {actual_id}")
        self._completed_response_ids.add(request_id)
        if lifecycle is not None:
            lifecycle.response_complete = True
        decoded = frame.payload.decode("utf-8", errors="surrogateescape")
        if frame.status == b"error":
            raise RuntimeResponseError(decoded)
        return decoded

    async def _read_response_frame(self, state: _ResponseReadState) -> _ResponseFrame:
        reader = await self._ensure_response_reader()
        header = await self._readline(reader, response_state=state)
        nonce = self.paths.nonce.encode("ascii")
        header_match = re.fullmatch(
            re.escape(nonce) + rb" BEGIN ([0-9]+) (ok|error)\n", header
        )
        if header_match is None:
            raise TransportError(f"malformed response header: {header!r}")
        request_id = int(header_match.group(1))
        status = header_match.group(2)

        payload = bytearray()
        marker_prefix = nonce + b" "
        while True:
            line = await self._readline(reader, response_state=state)
            if line.startswith(nonce + b" BEGIN "):
                raise TransportError("duplicate response BEGIN marker")
            if line.startswith(nonce + b" END "):
                end_match = re.fullmatch(re.escape(nonce) + rb" END ([0-9]+)\n", line)
                if end_match is None:
                    raise TransportError(f"malformed response END marker: {line!r}")
                end_id = int(end_match.group(1))
                if end_id != request_id:
                    raise TransportError(
                        f"response END request ID {end_id} does not match BEGIN {request_id}"
                    )
                break
            if line.startswith(marker_prefix) and (
                b" BEGIN " in line or b" END " in line
            ):
                raise TransportError(f"malformed response marker: {line!r}")
            payload.extend(line)

        if not payload.endswith(b"\n"):
            raise TransportError("response frame is missing its payload separator")
        del payload[-1:]
        return _ResponseFrame(request_id, status, bytes(payload))

    async def _cancel_abandoned_request_if_safe(
        self,
        task: asyncio.Task[str],
        lifecycle: _RequestLifecycle,
    ) -> bool:
        """Cancel only a resettable request; otherwise retain its response drain."""

        if task.done():
            return False
        if lifecycle.submission_state == "zero":
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return False

        response_task = lifecycle.response_task
        response_state = lifecycle.response_state
        if response_task is None or response_state is None or response_state.consumed:
            lifecycle.caller_abandoned = True
            return True

        if lifecycle.body_written:
            lifecycle.caller_abandoned = True
            return False

        response_task.cancel()
        if self._response_frame_task is response_task:
            self._response_frame_task = None
            self._response_frame_state = None
        task.cancel()
        await asyncio.gather(response_task, task, return_exceptions=True)
        return False

    def _request_task_done(self, task: asyncio.Task[str]) -> None:
        self._request_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def release(self) -> None:
        """Close only the active probe's control writer, if any."""

        async with self._control_lock:
            descriptor = self._control_writer
            self._control_writer = None
            if descriptor is not None:
                os.close(descriptor)

    async def close(self) -> None:
        """Close and unlink owned endpoints; repeated calls are safe."""

        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._close_control_writer()
            self._wake_io_waiters()
            owner_tasks = tuple(
                task
                for task in (
                    self._event_record_task,
                    self._response_frame_task,
                    *self._request_tasks,
                )
                if task is not None
            )
            self._event_record_task = None
            self._response_frame_task = None
            self._response_frame_state = None
            self._request_tasks.clear()
            for task in owner_tasks:
                task.cancel()
            try:
                if owner_tasks:
                    await asyncio.gather(*owner_tasks, return_exceptions=True)
            finally:
                cleanup_error: BaseException | None = None
                for owner in tuple(self._request_owners.values()):
                    try:
                        owner.cleanup()
                    except BaseException as error:  # noqa: BLE001
                        if cleanup_error is None:
                            cleanup_error = error
                self._request_owners.clear()
                for attribute in ("_event_reader", "_response_reader"):
                    reader = getattr(self, attribute)
                    setattr(self, attribute, None)
                    if reader is not None:
                        self._close_descriptor(reader.descriptor)
                        self._close_descriptor(reader.keeper_descriptor)
                workspace_descriptor = self.paths.workspace_descriptor
                if workspace_descriptor is not None:
                    for basename, identity in self._fifo_identities.items():
                        try:
                            self._unlink_owned_fifo(
                                basename,
                                workspace_descriptor,
                                identity,
                            )
                        except OSError as error:
                            if cleanup_error is None:
                                cleanup_error = error
                            else:
                                cleanup_error.add_note(
                                    f"runtime FIFO cleanup also failed: {error}"
                                )
                    pin_descriptor = self._control_pin_descriptor
                    self._control_pin_descriptor = None
                    if pin_descriptor is not None:
                        self._close_descriptor(pin_descriptor)
                    try:
                        os.close(workspace_descriptor)
                    except OSError as error:
                        if cleanup_error is None:
                            cleanup_error = error
                        else:
                            cleanup_error.add_note(
                                f"workspace descriptor close also failed: {error}"
                            )
                if cleanup_error is not None:
                    raise TransportError(
                        "could not safely close runtime transport"
                    ) from cleanup_error

    def _finish_request_owner(
        self,
        lifecycle: _RequestLifecycle,
        primary_error: BaseException | None,
    ) -> None:
        owner = lifecycle.request_owner
        if owner is None:
            return
        lifecycle.request_owner = None
        self._request_owners.pop(id(owner), None)
        try:
            owner.cleanup()
        except BaseException as cleanup_error:
            wrapped = TransportError("could not safely remove a runtime request file")
            if primary_error is not None:
                primary_error.add_note(f"request cleanup also failed: {cleanup_error}")
                return
            raise wrapped from cleanup_error

    def _ensure_open(self) -> None:
        if self._closed:
            raise TransportError("transport is closed")
        if self._fatal_error is not None:
            raise IncompleteResponseError(
                "timed-out inspection response could not be drained; "
                "runtime channel is desynchronized"
            ) from self._fatal_error

    async def _ensure_event_reader(self) -> _PersistentReader:
        self._ensure_open()
        assert self._event_reader is not None
        return self._event_reader

    async def _ensure_response_reader(self) -> _PersistentReader:
        self._ensure_open()
        assert self._response_reader is not None
        return self._response_reader

    @classmethod
    def _open_persistent_reader(
        cls,
        path: Path,
        workspace_descriptor: int,
        identity: tuple[int, int],
    ) -> _PersistentReader:
        reader = cls._open_owned_fifo(
            path,
            workspace_descriptor,
            os.O_RDONLY | os.O_NONBLOCK,
            identity,
        )
        try:
            keeper = cls._open_owned_fifo(
                path,
                workspace_descriptor,
                os.O_WRONLY | os.O_NONBLOCK,
                identity,
            )
        except BaseException:
            os.close(reader)
            raise
        return _PersistentReader(path, reader, keeper)

    async def _open_control_writer(self) -> None:
        async with self._control_lock:
            if self._control_writer is not None:
                raise TransportError(
                    "received a probe before the prior probe was released"
                )
            while True:
                self._ensure_open()
                workspace_descriptor = self.paths.workspace_descriptor
                assert workspace_descriptor is not None
                _verify_workspace_path_identity(
                    self.paths.control_fifo.parent,
                    workspace_descriptor,
                )
                try:
                    descriptor = self._open_owned_fifo(
                        self.paths.control_fifo,
                        workspace_descriptor,
                        os.O_WRONLY | os.O_NONBLOCK,
                        self._fifo_identities[self.paths.control_fifo.name],
                    )
                except TransportError as error:
                    if (
                        not isinstance(error.__cause__, OSError)
                        or error.__cause__.errno != errno.ENXIO
                    ):
                        raise TransportError(
                            "could not open the probe control FIFO"
                        ) from error
                    await asyncio.sleep(_CONTROL_OPEN_RETRY_SECONDS)
                    continue
                self._control_writer = descriptor
                return

    async def _readline(
        self,
        reader: _PersistentReader,
        timeout: float | None = None,
        *,
        deadline: float | None = None,
        response_state: _ResponseReadState | None = None,
    ) -> bytes:
        if deadline is None and timeout is not None:
            deadline = time.monotonic() + timeout
        while True:
            if response_state is not None and reader.buffer:
                response_state.consumed = True
            newline = reader.buffer.find(b"\n")
            if newline >= 0:
                line = bytes(reader.buffer[: newline + 1])
                del reader.buffer[: newline + 1]
                return line
            self._ensure_open()
            try:
                chunk = os.read(reader.descriptor, 64 * 1024)
            except BlockingIOError:
                await self._wait_readable(reader.descriptor, reader.path, deadline)
                continue
            except OSError as error:
                if self._closed:
                    raise TransportError("transport is closed") from error
                raise TransportError(
                    f"could not read runtime FIFO: {reader.path}"
                ) from error
            if chunk is None:
                continue
            if not chunk:
                raise TransportError(f"runtime FIFO closed unexpectedly: {reader.path}")
            if response_state is not None:
                response_state.consumed = True
            reader.buffer.extend(chunk)

    async def _write_all(
        self,
        descriptor: int,
        data: bytes,
        deadline: float,
        lifecycle: _RequestLifecycle,
    ) -> None:
        view = memoryview(data)
        while view:
            self._ensure_open()
            prior_state = lifecycle.submission_state
            lifecycle.submission_state = "indeterminate"
            try:
                written = os.write(descriptor, view)
            except BlockingIOError:
                lifecycle.submission_state = prior_state
                await self._wait_writable(descriptor, deadline)
                continue
            except OSError as error:
                raise TransportError(
                    "control FIFO closed while writing a request"
                ) from error
            if written == 0:
                lifecycle.submission_state = prior_state
                raise TransportError("control FIFO accepted no request bytes")
            lifecycle.submission_state = "submitted"
            view = view[written:]

    async def _wait_readable(
        self,
        descriptor: int,
        path: Path,
        deadline: float | None,
    ) -> None:
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[None] = loop.create_future()
        self._io_waiters.add(ready)
        loop.add_reader(descriptor, self._mark_ready, ready)
        try:
            if deadline is None:
                await ready
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"timed out reading {path}")
                try:
                    await asyncio.wait_for(ready, timeout=remaining)
                except TimeoutError:
                    raise TimeoutError(f"timed out reading {path}") from None
        finally:
            loop.remove_reader(descriptor)
            self._io_waiters.discard(ready)

    async def _wait_writable(self, descriptor: int, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("timed out writing the control request")
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[None] = loop.create_future()
        self._io_waiters.add(ready)
        loop.add_writer(descriptor, self._mark_ready, ready)
        try:
            await asyncio.wait_for(ready, timeout=remaining)
        finally:
            loop.remove_writer(descriptor)
            self._io_waiters.discard(ready)

    @staticmethod
    def _mark_ready(ready: asyncio.Future[None]) -> None:
        if not ready.done():
            ready.set_result(None)

    def _wake_io_waiters(self) -> None:
        for waiter in tuple(self._io_waiters):
            if not waiter.done():
                waiter.set_exception(TransportError("transport is closed"))

    def _close_control_writer(self) -> None:
        descriptor = self._control_writer
        self._control_writer = None
        if descriptor is not None:
            self._close_descriptor(descriptor)

    @staticmethod
    def _close_descriptor(descriptor: int) -> None:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _verify_workspace_path_identity(path: Path, descriptor: int) -> None:
    """Verify that *path* still names the retained private workspace directory."""

    try:
        descriptor_status = os.fstat(descriptor)
        path_status = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise TransportError(
            f"runtime workspace path identity changed: {path}"
        ) from error
    if (
        not stat.S_ISDIR(descriptor_status.st_mode)
        or not stat.S_ISDIR(path_status.st_mode)
        or descriptor_status.st_dev != path_status.st_dev
        or descriptor_status.st_ino != path_status.st_ino
    ):
        raise TransportError(f"runtime workspace path identity changed: {path}")
    if (
        stat.S_IMODE(descriptor_status.st_mode) != _WORKSPACE_MODE
        or stat.S_IMODE(path_status.st_mode) != _WORKSPACE_MODE
    ):
        raise TransportError(f"runtime workspace has unsafe permissions: {path}")


def duplicate_workspace_descriptor(paths: RuntimePaths) -> int:
    """Duplicate and validate the workspace authority carried by *paths*."""

    descriptor = paths.workspace_descriptor
    if descriptor is None:
        raise TransportError("runtime workspace descriptor is unavailable")
    try:
        duplicate = os.dup(descriptor)
    except OSError as error:
        raise TransportError("runtime workspace descriptor is unavailable") from error
    try:
        verify_workspace_descriptor(paths, duplicate)
    except BaseException:
        ProbeTransport._close_descriptor(duplicate)
        raise
    return duplicate


def verify_workspace_descriptor(paths: RuntimePaths, descriptor: int) -> None:
    """Verify endpoint placement and the current name of a retained workspace."""

    workspace = paths.control_fifo.parent
    if paths.event_fifo.parent != workspace or paths.response_fifo.parent != workspace:
        raise TransportError("runtime endpoints are not rooted in one workspace")
    _verify_workspace_path_identity(workspace, descriptor)

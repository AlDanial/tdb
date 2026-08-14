from __future__ import annotations

import asyncio
import errno
import os
import select
import shutil
import signal
import stat
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tdb.adapters.tcsh.runtime import (
    render_inspection_request,
    render_probe,
    render_source_event,
)
from tdb.adapters.tcsh.transport import (
    IncompleteResponseError,
    ProbeEvent,
    ProbeTransport,
    RuntimePaths,
    RuntimeResponseError,
    TransportError,
)

_IO_TIMEOUT = 1.0
TCSH = shutil.which("tcsh")
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW


async def write_fifo(path: Path, data: str | bytes) -> None:
    deadline = time.monotonic() + _IO_TIMEOUT
    while True:
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
            break
        except OSError as error:
            if error.errno != errno.ENXIO or time.monotonic() >= deadline:
                raise
            await asyncio.sleep(0.005)
    try:
        encoded = (
            data.encode("utf-8", errors="surrogateescape")
            if isinstance(data, str)
            else data
        )
        view = memoryview(encoded)
        while view:
            try:
                written = os.write(descriptor, view)
            except BlockingIOError:
                await _wait_for_descriptor(descriptor, writable=True, deadline=deadline)
                continue
            view = view[written:]
    finally:
        os.close(descriptor)


async def _wait_for_descriptor(
    descriptor: int,
    *,
    writable: bool,
    deadline: float,
) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("timed out waiting for FIFO readiness")
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[None] = loop.create_future()
    callback = loop.add_writer if writable else loop.add_reader
    remove = loop.remove_writer if writable else loop.remove_reader
    callback(descriptor, _mark_ready, ready)
    try:
        await asyncio.wait_for(ready, timeout=remaining)
    finally:
        remove(descriptor)


def _mark_ready(ready: asyncio.Future[None]) -> None:
    if not ready.done():
        ready.set_result(None)


def _descriptor_is_closed(descriptor: int) -> bool:
    try:
        os.fstat(descriptor)
    except OSError as error:
        if error.errno == errno.EBADF:
            return True
        raise
    return False


def _read_line(descriptor: int, timeout: float = _IO_TIMEOUT) -> bytes:
    deadline = time.monotonic() + timeout
    data = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("timed out reading FIFO")
        readable, _, _ = select.select([descriptor], [], [], remaining)
        if not readable:
            raise TimeoutError("timed out reading FIFO")
        chunk = os.read(descriptor, 1)
        if not chunk:
            time.sleep(0.005)
            continue
        data.extend(chunk)
        if chunk == b"\n":
            return bytes(data)


async def read_line(descriptor: int) -> str:
    deadline = time.monotonic() + _IO_TIMEOUT
    data = bytearray()
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError("timed out reading FIFO")
        try:
            chunk = os.read(descriptor, 1)
        except BlockingIOError:
            await _wait_for_descriptor(descriptor, writable=False, deadline=deadline)
            continue
        if not chunk:
            await asyncio.sleep(0.005)
            continue
        data.extend(chunk)
        if chunk == b"\n":
            return data.decode()


async def feed_event(transport: ProbeTransport, record: str) -> None:
    await write_fifo(transport.paths.event_fifo, record)


async def feed_response(
    transport: ProbeTransport,
    request_id: int,
    *,
    status: str = "ok",
    payload: str = "",
) -> None:
    nonce = transport.paths.nonce
    await write_fifo(
        transport.paths.response_fifo,
        f"{nonce} BEGIN {request_id} {status}\n{payload}\n{nonce} END {request_id}\n",
    )


def open_control_reader(transport: ProbeTransport) -> int:
    return os.open(transport.paths.control_fifo, os.O_RDONLY | os.O_NONBLOCK)


def decode_literal_tcsh_word(word: str) -> str:
    """Decode the conservative literal-word syntax expected from the renderer."""

    if word == "''":
        return ""
    decoded: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(word):
        character = word[index]
        if character == "\\":
            index += 1
            assert index < len(word), "trailing backslash is unsafe"
            decoded.append(word[index])
        elif character in {"'", '"'}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            else:
                decoded.append(character)
        else:
            assert not (character in "$`" and quote != "'")
            assert character != "!"
            assert not (character.isspace() and quote is None)
            decoded.append(character)
        index += 1
    assert quote is None
    return "".join(decoded)


@pytest.fixture
def runtime_paths(tmp_path: Path) -> Iterator[RuntimePaths]:
    unusual = tmp_path / "fifo space $d `cmd` !*?[]{}~%'\\\""
    unusual.mkdir()
    unusual.chmod(0o700)
    workspace_descriptor = os.open(unusual, _DIRECTORY_OPEN_FLAGS)
    try:
        yield RuntimePaths(
            event_fifo=unusual / "event stream",
            control_fifo=unusual / "control's stream",
            response_fifo=unusual / "response stream",
            nonce="0123456789abcdef",
            workspace_descriptor=workspace_descriptor,
        )
    finally:
        os.close(workspace_descriptor)


@pytest.mark.asyncio
async def test_create_makes_private_fifos(tmp_path: Path) -> None:
    workspace = tmp_path / "session"
    workspace.mkdir(mode=0o777)
    workspace.chmod(0o777)

    transport = ProbeTransport.create(workspace)
    paths = (
        transport.paths.event_fifo,
        transport.paths.control_fifo,
        transport.paths.response_fifo,
    )
    try:
        assert stat.S_IMODE(workspace.stat().st_mode) == 0o700
        for fifo in paths:
            assert stat.S_ISFIFO(fifo.stat().st_mode)
            assert stat.S_IMODE(fifo.stat().st_mode) == 0o600
        assert len(transport.paths.nonce) >= 32
        int(transport.paths.nonce, 16)
    finally:
        await asyncio.wait_for(transport.close(), timeout=_IO_TIMEOUT)
    assert all(not fifo.exists() for fifo in paths)


@pytest.mark.asyncio
async def test_event_parser_accepts_grammar_and_keeps_reader_alive(
    tmp_path: Path,
) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    control_reader = open_control_reader(transport)
    try:
        probe_task = asyncio.create_task(transport.next_event())
        await feed_event(transport, "P 17\n")
        assert await asyncio.wait_for(probe_task, timeout=_IO_TIMEOUT) == ProbeEvent(
            kind="probe", probe_id=17, source_id=None
        )
        await transport.release()

        enter_task = asyncio.create_task(transport.next_event())
        await feed_event(transport, "E 2\n")
        assert await asyncio.wait_for(enter_task, timeout=_IO_TIMEOUT) == ProbeEvent(
            kind="enter", probe_id=None, source_id=2
        )

        leave_task = asyncio.create_task(transport.next_event())
        await feed_event(transport, "L 2\n")
        assert await asyncio.wait_for(leave_task, timeout=_IO_TIMEOUT) == ProbeEvent(
            kind="leave", probe_id=None, source_id=2
        )
    finally:
        os.close(control_reader)
        await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "record",
    ["\n", "X 1\n", "P\n", "P nope\n", "P -1\n", "E 1 extra\n", " L 1\n"],
)
async def test_event_parser_rejects_malformed_records(
    tmp_path: Path, record: str
) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    try:
        event_task = asyncio.create_task(transport.next_event())
        await feed_event(transport, record)
        with pytest.raises(TransportError, match="event"):
            await asyncio.wait_for(event_task, timeout=_IO_TIMEOUT)
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_cancelled_event_read_does_not_discard_the_next_record(
    tmp_path: Path,
) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    try:
        cancelled_read = asyncio.create_task(transport.next_event())
        await asyncio.sleep(0.02)
        cancelled_read.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_read

        await feed_event(transport, "E 7\n")
        await asyncio.sleep(0.15)

        assert await asyncio.wait_for(
            transport.next_event(), timeout=0.25
        ) == ProbeEvent(kind="enter", probe_id=None, source_id=7)
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_cancelled_event_read_preserves_probe_while_control_open_is_pending(
    tmp_path: Path,
) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    control_reader = -1
    try:
        cancelled_read = asyncio.create_task(transport.next_event())
        await feed_event(transport, "P 9\n")
        await asyncio.sleep(0.02)
        assert not cancelled_read.done()

        cancelled_read.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_read

        control_reader = open_control_reader(transport)
        assert await asyncio.wait_for(
            transport.next_event(), timeout=0.25
        ) == ProbeEvent(kind="probe", probe_id=9, source_id=None)
    finally:
        await transport.release()
        if control_reader >= 0:
            os.close(control_reader)
        await transport.close()


@pytest.mark.asyncio
async def test_response_parser_preserves_multiline_payload(tmp_path: Path) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    try:
        response_task = asyncio.create_task(
            transport.read_response(3, timeout=_IO_TIMEOUT)
        )
        await feed_response(transport, request_id=3, payload="one\ntwo\n")
        assert (
            await asyncio.wait_for(response_task, timeout=_IO_TIMEOUT) == "one\ntwo\n"
        )
    finally:
        await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [b"", b"tail", b"tail\n", b"tail\n\n", b"\xfftail"])
async def test_response_parser_removes_only_adapter_payload_separator(
    tmp_path: Path,
    payload: bytes,
) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    nonce = transport.paths.nonce.encode("ascii")
    try:
        response_task = asyncio.create_task(
            transport.read_response(3, timeout=_IO_TIMEOUT)
        )
        await write_fifo(
            transport.paths.response_fifo,
            nonce + b" BEGIN 3 ok\n" + payload + b"\n" + nonce + b" END 3\n",
        )
        response = await asyncio.wait_for(response_task, timeout=_IO_TIMEOUT)

        assert response.encode("utf-8", errors="surrogateescape") == payload
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_cancelled_response_read_does_not_discard_the_frame(
    tmp_path: Path,
) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    try:
        cancelled_read = asyncio.create_task(
            transport.read_response(3, timeout=_IO_TIMEOUT)
        )
        await asyncio.sleep(0.02)
        cancelled_read.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_read

        await feed_response(transport, request_id=3, payload="preserved\n")
        await asyncio.sleep(0.15)

        assert await transport.read_response(3, timeout=0.25) == "preserved\n"
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_cancelled_response_read_preserves_partially_consumed_frame(
    tmp_path: Path,
) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    nonce = transport.paths.nonce
    try:
        cancelled_read = asyncio.create_task(
            transport.read_response(3, timeout=_IO_TIMEOUT)
        )
        await write_fifo(
            transport.paths.response_fifo,
            f"{nonce} BEGIN 3 ok\nfirst\n",
        )
        await asyncio.sleep(0.02)
        assert not cancelled_read.done()

        cancelled_read.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_read

        await write_fifo(
            transport.paths.response_fifo,
            f"second\n\n{nonce} END 3\n",
        )
        assert await transport.read_response(3, timeout=0.25) == "first\nsecond\n"
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_response_parser_preserves_large_single_line_payload(
    tmp_path: Path,
) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    payload = "x" * (128 * 1024) + "\n"
    try:
        response_task = asyncio.create_task(
            transport.read_response(3, timeout=_IO_TIMEOUT)
        )
        await feed_response(transport, request_id=3, payload=payload)
        assert await asyncio.wait_for(response_task, timeout=_IO_TIMEOUT) == payload
    finally:
        await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frame",
    [
        "wrong BEGIN 3 ok\nwrong END 3\n",
        "{nonce} BEGIN nope ok\n{nonce} END 3\n",
        "{nonce} BEGIN 3 maybe\n{nonce} END 3\n",
        "{nonce} BEGIN 3 ok\n{nonce} BEGIN 3 ok\n{nonce} END 3\n",
        "{nonce} BEGIN 3 ok\npayload\n{nonce} END 4\n",
        "{nonce} BEGIN 3 ok\npayload\n{nonce} END nope\n",
    ],
)
async def test_response_parser_rejects_malformed_frames(
    tmp_path: Path, frame: str
) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    try:
        response_task = asyncio.create_task(
            transport.read_response(3, timeout=_IO_TIMEOUT)
        )
        await write_fifo(
            transport.paths.response_fifo, frame.format(nonce=transport.paths.nonce)
        )
        with pytest.raises(TransportError, match="response|marker|request"):
            await asyncio.wait_for(response_task, timeout=_IO_TIMEOUT)
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_response_parser_rejects_mismatched_begin_id(tmp_path: Path) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    try:
        response_task = asyncio.create_task(
            transport.read_response(3, timeout=_IO_TIMEOUT)
        )
        await feed_response(transport, request_id=4, payload="wrong request\n")
        with pytest.raises(TransportError, match="request ID"):
            await asyncio.wait_for(response_task, timeout=_IO_TIMEOUT)
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_response_parser_rejects_duplicate_request_id(tmp_path: Path) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    try:
        first = asyncio.create_task(transport.read_response(3, timeout=_IO_TIMEOUT))
        await feed_response(transport, request_id=3, payload="first\n")
        assert await asyncio.wait_for(first, timeout=_IO_TIMEOUT) == "first\n"

        with pytest.raises(TransportError, match="duplicate"):
            await transport.read_response(3, timeout=_IO_TIMEOUT)
    finally:
        await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    ["", "bad command\n", "first\nsecond\n\n", "bad byte: \udcff\n"],
)
async def test_error_response_preserves_exact_payload_in_typed_exception(
    tmp_path: Path,
    payload: str,
) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    try:
        response_task = asyncio.create_task(
            transport.read_response(8, timeout=_IO_TIMEOUT)
        )
        await feed_response(transport, request_id=8, status="error", payload=payload)
        with pytest.raises(RuntimeResponseError) as caught:
            await asyncio.wait_for(response_task, timeout=_IO_TIMEOUT)
        assert caught.value.payload == payload
        assert str(caught.value) == payload
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_response_timeout_is_bounded_and_transport_remains_usable(
    tmp_path: Path,
) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await transport.read_response(1, timeout=0.05)
        assert time.monotonic() - started < 0.5

        response_task = asyncio.create_task(
            transport.read_response(2, timeout=_IO_TIMEOUT)
        )
        await feed_response(transport, request_id=2, payload="after timeout\n")
        assert (
            await asyncio.wait_for(response_task, timeout=_IO_TIMEOUT)
            == "after timeout\n"
        )
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_send_request_requires_an_active_probe(tmp_path: Path) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    try:
        with pytest.raises(TransportError, match="active probe"):
            await transport.send_request(lambda request_id: f"request {request_id}\n")
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_send_request_serializes_monotonic_request_ids(tmp_path: Path) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    control_reader = open_control_reader(transport)
    allocated: list[int] = []

    def body(request_id: int) -> str:
        allocated.append(request_id)
        return f"request {request_id}\n"

    try:
        event_task = asyncio.create_task(transport.next_event())
        await feed_event(transport, "P 1\n")
        await asyncio.wait_for(event_task, timeout=_IO_TIMEOUT)

        first = asyncio.create_task(transport.send_request(body, timeout=_IO_TIMEOUT))
        second = asyncio.create_task(transport.send_request(body, timeout=_IO_TIMEOUT))

        assert await read_line(control_reader) == "request 1\n"
        assert allocated == [1]
        await feed_response(transport, request_id=1, payload="first\n")
        assert await asyncio.wait_for(first, timeout=_IO_TIMEOUT) == "first\n"

        assert await read_line(control_reader) == "request 2\n"
        assert allocated == [1, 2]
        await feed_response(transport, request_id=2, payload="second\n")
        assert await asyncio.wait_for(second, timeout=_IO_TIMEOUT) == "second\n"
    finally:
        await transport.release()
        os.close(control_reader)
        await transport.close()


@pytest.mark.asyncio
async def test_cancelled_response_wait_keeps_next_request_serialized(
    tmp_path: Path,
) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    control_reader = open_control_reader(transport)
    first: asyncio.Task[str] | None = None
    second: asyncio.Task[str] | None = None
    try:
        event_task = asyncio.create_task(transport.next_event())
        await feed_event(transport, "P 1\n")
        await asyncio.wait_for(event_task, timeout=_IO_TIMEOUT)

        first = asyncio.create_task(
            transport.send_request(
                lambda request_id: f"request {request_id}\n", timeout=0.5
            )
        )
        assert await read_line(control_reader) == "request 1\n"
        nonce = transport.paths.nonce
        await write_fifo(
            transport.paths.response_fifo,
            f"{nonce} BEGIN 1 ok\npartial",
        )
        await asyncio.sleep(0.01)

        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        second = asyncio.create_task(
            transport.send_request(
                lambda request_id: f"request {request_id}\n", timeout=0.5
            )
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(read_line(control_reader), timeout=0.05)

        await write_fifo(
            transport.paths.response_fifo,
            f"\n{nonce} END 1\n",
        )
        assert (
            await asyncio.wait_for(read_line(control_reader), timeout=0.25)
            == "request 2\n"
        )
        await feed_response(transport, request_id=2, payload="second\n")
        assert await asyncio.wait_for(second, timeout=0.25) == "second\n"
    finally:
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (first, second) if task is not None),
            return_exceptions=True,
        )
        await transport.release()
        os.close(control_reader)
        await transport.close()


@pytest.mark.asyncio
async def test_response_timeout_keeps_serialization_until_late_frame(
    tmp_path: Path,
) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    control_reader = open_control_reader(transport)
    first: asyncio.Task[str] | None = None
    second: asyncio.Task[str] | None = None
    try:
        event_task = asyncio.create_task(transport.next_event())
        await feed_event(transport, "P 1\n")
        await asyncio.wait_for(event_task, timeout=_IO_TIMEOUT)

        first = asyncio.create_task(
            transport.send_request(
                lambda request_id: f"request {request_id}\n", timeout=0.05
            )
        )
        assert await read_line(control_reader) == "request 1\n"
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(first, timeout=0.25)

        second = asyncio.create_task(
            transport.send_request(
                lambda request_id: f"request {request_id}\n", timeout=0.5
            )
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(read_line(control_reader), timeout=0.05)

        await feed_response(transport, request_id=1, payload="late\n")
        assert (
            await asyncio.wait_for(read_line(control_reader), timeout=0.25)
            == "request 2\n"
        )
        await feed_response(transport, request_id=2, payload="after timeout\n")
        assert await asyncio.wait_for(second, timeout=0.25) == "after timeout\n"
    finally:
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (first, second) if task is not None),
            return_exceptions=True,
        )
        await transport.release()
        os.close(control_reader)
        await transport.close()


@pytest.mark.asyncio
async def test_zero_byte_response_timeout_drains_late_frame_before_next_request(
    tmp_path: Path,
    tcsh_path: Path,
) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    process = await asyncio.create_subprocess_exec(
        str(tcsh_path),
        "-f",
        "-c",
        render_probe(1, transport.paths),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    second: asyncio.Task[str] | None = None
    stopped = False
    try:
        assert await asyncio.wait_for(
            transport.next_event(), timeout=_IO_TIMEOUT
        ) == ProbeEvent(
            kind="probe",
            probe_id=1,
            source_id=None,
        )
        os.kill(process.pid, signal.SIGSTOP)
        async with asyncio.timeout(_IO_TIMEOUT):
            while True:
                waited_pid, wait_status = os.waitpid(  # noqa: ASYNC222 - WNOHANG
                    process.pid,
                    os.WUNTRACED | os.WNOHANG,
                )
                if waited_pid == process.pid:
                    break
                await asyncio.sleep(0.005)
        assert waited_pid == process.pid
        assert os.WIFSTOPPED(wait_status)
        stopped = True

        with pytest.raises(TimeoutError):
            await transport.send_request(
                lambda request_id: render_inspection_request(
                    request_id,
                    "echo first",
                    transport.paths,
                ),
                timeout=0.05,
            )

        second = asyncio.create_task(
            transport.send_request(
                lambda request_id: render_inspection_request(
                    request_id,
                    "echo second",
                    transport.paths,
                ),
                timeout=1.0,
            )
        )
        await asyncio.sleep(0.02)
        os.kill(process.pid, signal.SIGCONT)
        stopped = False

        assert await asyncio.wait_for(second, timeout=_IO_TIMEOUT) == "second\n"
    finally:
        if stopped:
            os.kill(process.pid, signal.SIGCONT)
        if second is not None and not second.done():
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)
        await transport.release()
        try:
            await asyncio.wait_for(process.communicate(), timeout=_IO_TIMEOUT)
        except TimeoutError:
            process.kill()
            await process.communicate()
        await transport.close()


@pytest.mark.asyncio
async def test_failed_late_response_drain_marks_transport_incomplete(
    tmp_path: Path,
) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    control_reader = open_control_reader(transport)
    second: asyncio.Task[str] | None = None
    try:
        event_task = asyncio.create_task(transport.next_event())
        await feed_event(transport, "P 1\n")
        await asyncio.wait_for(event_task, timeout=_IO_TIMEOUT)

        first = asyncio.create_task(
            transport.send_request(
                lambda request_id: f"request {request_id}\n", timeout=0.05
            )
        )
        assert await read_line(control_reader) == "request 1\n"
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(first, timeout=0.25)

        second = asyncio.create_task(
            transport.send_request(
                lambda request_id: f"request {request_id}\n", timeout=0.5
            )
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(read_line(control_reader), timeout=0.05)
        await write_fifo(transport.paths.response_fifo, "malformed late frame\n")

        with pytest.raises(
            IncompleteResponseError, match="could not be drained"
        ) as raised:
            await asyncio.wait_for(second, timeout=0.25)
        assert isinstance(raised.value.__cause__, TransportError)
    finally:
        if second is not None and not second.done():
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)
        await transport.release()
        os.close(control_reader)
        await transport.close()


@pytest.mark.asyncio
async def test_completed_request_cleanup_failure_is_a_transport_error(
    tmp_path: Path,
) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    control_reader = open_control_reader(transport)

    class FailingCleanupBody(str):
        def cleanup(self) -> None:
            raise OSError("cleanup failed")

    try:
        event_task = asyncio.create_task(transport.next_event())
        await feed_event(transport, "P 1\n")
        await asyncio.wait_for(event_task, timeout=_IO_TIMEOUT)
        request = asyncio.create_task(
            transport.send_request(
                lambda request_id: FailingCleanupBody(f"request {request_id}\n"),
                timeout=0.5,
            )
        )
        assert await read_line(control_reader) == "request 1\n"
        await feed_response(transport, request_id=1, payload="complete\n")

        with pytest.raises(TransportError, match="remove") as raised:
            await asyncio.wait_for(request, timeout=0.25)

        assert isinstance(raised.value.__cause__, OSError)
        await asyncio.wait_for(transport.release(), timeout=0.25)
    finally:
        os.close(control_reader)
        await transport.close()


@pytest.mark.asyncio
async def test_request_error_precedes_completed_cleanup_failure(tmp_path: Path) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    control_reader = open_control_reader(transport)

    class FailingCleanupBody(str):
        def cleanup(self) -> None:
            raise OSError("cleanup failed")

    try:
        event_task = asyncio.create_task(transport.next_event())
        await feed_event(transport, "P 1\n")
        await asyncio.wait_for(event_task, timeout=_IO_TIMEOUT)
        request = asyncio.create_task(
            transport.send_request(
                lambda request_id: FailingCleanupBody(f"request {request_id}\n"),
                timeout=0.5,
            )
        )
        assert await read_line(control_reader) == "request 1\n"
        await feed_response(
            transport, request_id=1, status="error", payload="primary\n"
        )

        with pytest.raises(RuntimeResponseError, match="primary") as raised:
            await asyncio.wait_for(request, timeout=0.25)

        assert raised.value.__notes__ == ["request cleanup also failed: cleanup failed"]
        await asyncio.wait_for(transport.release(), timeout=0.25)
    finally:
        os.close(control_reader)
        await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "error_number"),
    [
        pytest.param(InterruptedError, errno.EINTR, id="interrupted"),
        pytest.param(BrokenPipeError, errno.EPIPE, id="epipe"),
        pytest.param(OSError, errno.EIO, id="oserror"),
    ],
)
async def test_partial_control_write_failure_is_incomplete_and_retains_owner_until_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[OSError],
    error_number: int,
) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    control_reader = open_control_reader(transport)
    cleanup_calls: list[str] = []

    class OwnedBody(str):
        def cleanup(self) -> None:
            cleanup_calls.append("cleanup")

    try:
        event_task = asyncio.create_task(transport.next_event())
        await feed_event(transport, "P 1\n")
        await asyncio.wait_for(event_task, timeout=_IO_TIMEOUT)
        control_writer = transport._control_writer
        original_write = os.write
        write_calls = 0

        def write_source_line_then_fail(
            descriptor: int,
            data: bytes | memoryview,
        ) -> int:
            nonlocal write_calls
            if descriptor != control_writer:
                return original_write(descriptor, data)
            write_calls += 1
            if write_calls == 1:
                source_end = bytes(data).index(b"\n") + 1
                return original_write(descriptor, data[:source_end])
            raise error_type(error_number, "injected partial write")

        monkeypatch.setattr(os, "write", write_source_line_then_fail)

        with pytest.raises(IncompleteResponseError, match="desynchronized") as raised:
            await transport.send_request(
                lambda request_id: OwnedBody(
                    f"source request-{request_id}.csh\necho done\n"
                ),
                timeout=0.5,
            )

        assert isinstance(raised.value.__cause__, TransportError)
        assert isinstance(raised.value.__cause__.__cause__, error_type)
        assert await read_line(control_reader) == "source request-1.csh\n"
        assert cleanup_calls == []
    finally:
        os.close(control_reader)
        await transport.close()

    assert cleanup_calls == ["cleanup"]


@pytest.mark.asyncio
async def test_partial_response_timeout_keeps_next_request_serialized(
    tmp_path: Path,
) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    control_reader = open_control_reader(transport)
    first: asyncio.Task[str] | None = None
    second: asyncio.Task[str] | None = None
    try:
        event_task = asyncio.create_task(transport.next_event())
        await feed_event(transport, "P 1\n")
        await asyncio.wait_for(event_task, timeout=_IO_TIMEOUT)

        first = asyncio.create_task(
            transport.send_request(
                lambda request_id: f"request {request_id}\n", timeout=0.05
            )
        )
        assert await read_line(control_reader) == "request 1\n"
        nonce = transport.paths.nonce
        await write_fifo(
            transport.paths.response_fifo,
            f"{nonce} BEGIN 1 ok\npartial",
        )
        with pytest.raises(IncompleteResponseError, match="incomplete"):
            await asyncio.wait_for(first, timeout=0.25)

        second = asyncio.create_task(
            transport.send_request(
                lambda request_id: f"request {request_id}\n", timeout=0.5
            )
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(read_line(control_reader), timeout=0.05)

        await write_fifo(
            transport.paths.response_fifo,
            f"\n{nonce} END 1\n",
        )
        assert (
            await asyncio.wait_for(read_line(control_reader), timeout=0.25)
            == "request 2\n"
        )
        await feed_response(transport, request_id=2, payload="second\n")
        assert await asyncio.wait_for(second, timeout=0.25) == "second\n"
    finally:
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (first, second) if task is not None),
            return_exceptions=True,
        )
        await transport.release()
        os.close(control_reader)
        await transport.close()


@pytest.mark.asyncio
async def test_close_cancels_partial_response_lifecycle_and_queued_request(
    tmp_path: Path,
) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    control_reader = open_control_reader(transport)
    first: asyncio.Task[str] | None = None
    second: asyncio.Task[str] | None = None
    try:
        event_task = asyncio.create_task(transport.next_event())
        await feed_event(transport, "P 1\n")
        await asyncio.wait_for(event_task, timeout=_IO_TIMEOUT)

        first = asyncio.create_task(
            transport.send_request(
                lambda request_id: f"request {request_id}\n", timeout=0.05
            )
        )
        assert await read_line(control_reader) == "request 1\n"
        nonce = transport.paths.nonce
        await write_fifo(
            transport.paths.response_fifo,
            f"{nonce} BEGIN 1 ok\npartial",
        )
        with pytest.raises(IncompleteResponseError, match="incomplete"):
            await asyncio.wait_for(first, timeout=0.25)

        second = asyncio.create_task(
            transport.send_request(
                lambda request_id: f"request {request_id}\n", timeout=5.0
            )
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(read_line(control_reader), timeout=0.05)

        await asyncio.wait_for(transport.close(), timeout=0.25)
        with pytest.raises(TransportError, match="closed"):
            await asyncio.wait_for(second, timeout=0.25)
    finally:
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (first, second) if task is not None),
            return_exceptions=True,
        )
        os.close(control_reader)
        await transport.close()


async def _respond_before_draining_request(
    control_reader: int,
    transport: ProbeTransport,
    request_size: int,
) -> None:
    deadline = time.monotonic() + _IO_TIMEOUT
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError("timed out waiting for the control request")
        try:
            received = len(os.read(control_reader, 1))
        except BlockingIOError:
            await _wait_for_descriptor(
                control_reader, writable=False, deadline=deadline
            )
            continue
        if received:
            break
        await asyncio.sleep(0.005)
    nonce = transport.paths.nonce
    await write_fifo(
        transport.paths.response_fifo,
        f"{nonce} BEGIN 1 ok\nready\n\n{nonce} END 1\n",
    )
    while received < request_size:
        try:
            chunk = os.read(control_reader, min(64 * 1024, request_size - received))
        except BlockingIOError:
            await _wait_for_descriptor(
                control_reader, writable=False, deadline=deadline
            )
            continue
        if not chunk:
            raise EOFError("control writer closed before the request was complete")
        received += len(chunk)


@pytest.mark.asyncio
async def test_send_request_opens_response_reader_before_large_control_write(
    tmp_path: Path,
) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    control_reader = open_control_reader(transport)
    request = "x" * (256 * 1024)
    try:
        event_task = asyncio.create_task(transport.next_event())
        await feed_event(transport, "P 1\n")
        await asyncio.wait_for(event_task, timeout=_IO_TIMEOUT)

        peer = asyncio.create_task(
            _respond_before_draining_request(
                control_reader, transport, len(request.encode())
            )
        )
        response = await asyncio.wait_for(
            transport.send_request(lambda request_id: request, timeout=_IO_TIMEOUT),
            timeout=_IO_TIMEOUT * 2,
        )

        assert response == "ready\n"
        await asyncio.wait_for(peer, timeout=_IO_TIMEOUT)
    finally:
        await transport.release()
        os.close(control_reader)
        await transport.close()


@pytest.mark.asyncio
async def test_partial_control_write_timeout_marks_transport_incomplete(
    tmp_path: Path,
) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    control_reader = open_control_reader(transport)
    request_task: asyncio.Task[str] | None = None
    try:
        event_task = asyncio.create_task(transport.next_event())
        await feed_event(transport, "P 1\n")
        await asyncio.wait_for(event_task, timeout=_IO_TIMEOUT)

        request_task = asyncio.create_task(
            transport.send_request(
                lambda request_id: "x" * (2 * 1024 * 1024), timeout=0.05
            )
        )
        done, _ = await asyncio.wait({request_task}, timeout=0.25)

        assert request_task in done, "control write ignored the request timeout"
        with pytest.raises(IncompleteResponseError, match="desynchronized"):
            await request_task
    finally:
        if request_task is not None and not request_task.done():
            request_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request_task
        os.close(control_reader)
        await asyncio.wait_for(transport.close(), timeout=_IO_TIMEOUT)


@pytest.mark.asyncio
async def test_cancelled_partial_control_write_marks_transport_incomplete(
    tmp_path: Path,
) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    control_reader = open_control_reader(transport)
    cleanup_calls: list[str] = []
    request_task: asyncio.Task[str] | None = None

    class OwnedBody(str):
        def cleanup(self) -> None:
            cleanup_calls.append("cleanup")

    try:
        event_task = asyncio.create_task(transport.next_event())
        await feed_event(transport, "P 1\n")
        await asyncio.wait_for(event_task, timeout=_IO_TIMEOUT)

        request_task = asyncio.create_task(
            transport.send_request(
                lambda request_id: OwnedBody(
                    f"source request-{request_id}.csh\n" + "x" * (2 * 1024 * 1024)
                ),
                timeout=5.0,
            )
        )
        assert await read_line(control_reader) == "source request-1.csh\n"

        request_task.cancel()
        with pytest.raises(IncompleteResponseError, match="desynchronized"):
            await request_task
        assert cleanup_calls == []
    finally:
        if request_task is not None and not request_task.done():
            request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)
        os.close(control_reader)
        await asyncio.wait_for(transport.close(), timeout=_IO_TIMEOUT)

    assert cleanup_calls == ["cleanup"]


@pytest.mark.asyncio
async def test_partial_write_failure_wins_race_with_caller_cancellation(
    tmp_path: Path,
) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    control_reader = open_control_reader(transport)
    cleanup_calls: list[str] = []
    request_task: asyncio.Task[str] | None = None

    class OwnedBody(str):
        def cleanup(self) -> None:
            cleanup_calls.append("cleanup")

    try:
        event_task = asyncio.create_task(transport.next_event())
        await feed_event(transport, "P 1\n")
        await asyncio.wait_for(event_task, timeout=_IO_TIMEOUT)

        request_task = asyncio.create_task(
            transport.send_request(
                lambda request_id: OwnedBody(
                    f"source request-{request_id}.csh\n" + "x" * (2 * 1024 * 1024)
                ),
                timeout=5.0,
            )
        )
        assert await read_line(control_reader) == "source request-1.csh\n"
        async with asyncio.timeout(0.25):
            while not transport._request_tasks:
                await asyncio.sleep(0)
        lifecycle_task = next(iter(transport._request_tasks))
        lifecycle_task.add_done_callback(lambda _: request_task.cancel())

        os.close(control_reader)
        control_reader = -1
        with pytest.raises(IncompleteResponseError, match="desynchronized") as raised:
            await request_task

        assert isinstance(raised.value.__cause__, TransportError)
        assert isinstance(raised.value.__cause__.__cause__, BrokenPipeError)
        assert cleanup_calls == []
    finally:
        if request_task is not None and not request_task.done():
            request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)
        if control_reader >= 0:
            os.close(control_reader)
        await asyncio.wait_for(transport.close(), timeout=_IO_TIMEOUT)

    assert cleanup_calls == ["cleanup"]


@pytest.mark.asyncio
async def test_proven_zero_byte_control_write_failure_is_retryable_and_cleans_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    first_reader = open_control_reader(transport)
    second_reader = -1
    cleanup_calls: list[str] = []

    class OwnedBody(str):
        def cleanup(self) -> None:
            cleanup_calls.append("cleanup")

    try:
        first_event = asyncio.create_task(transport.next_event())
        await feed_event(transport, "P 1\n")
        await asyncio.wait_for(first_event, timeout=_IO_TIMEOUT)
        control_writer = transport._control_writer
        original_write = os.write

        def accept_no_bytes(descriptor: int, data: bytes | memoryview) -> int:
            if descriptor == control_writer:
                return 0
            return original_write(descriptor, data)

        monkeypatch.setattr(os, "write", accept_no_bytes)

        with pytest.raises(TransportError, match="accepted no request bytes") as raised:
            await transport.send_request(
                lambda request_id: OwnedBody(f"request {request_id}\n"),
                timeout=0.5,
            )

        assert not isinstance(raised.value, IncompleteResponseError)
        assert cleanup_calls == ["cleanup"]
        monkeypatch.setattr(os, "write", original_write)

        os.close(first_reader)
        first_reader = -1
        second_reader = open_control_reader(transport)
        second_event = asyncio.create_task(transport.next_event())
        await feed_event(transport, "P 2\n")
        assert await asyncio.wait_for(second_event, timeout=0.25) == ProbeEvent(
            kind="probe", probe_id=2, source_id=None
        )
    finally:
        await transport.release()
        if first_reader >= 0:
            os.close(first_reader)
        if second_reader >= 0:
            os.close(second_reader)
        await transport.close()


@pytest.mark.asyncio
async def test_zero_byte_cancelled_control_write_clears_writer_for_the_next_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    first_reader = open_control_reader(transport)
    second_reader = -1
    request_task: asyncio.Task[str] | None = None
    try:
        first_event = asyncio.create_task(transport.next_event())
        await feed_event(transport, "P 1\n")
        await asyncio.wait_for(first_event, timeout=_IO_TIMEOUT)
        control_writer = transport._control_writer
        original_write = os.write

        def block_without_writing(descriptor: int, data: bytes | memoryview) -> int:
            if descriptor == control_writer:
                raise BlockingIOError
            return original_write(descriptor, data)

        monkeypatch.setattr(os, "write", block_without_writing)

        request_task = asyncio.create_task(
            transport.send_request(lambda request_id: "request body", timeout=5.0)
        )
        await asyncio.sleep(0.02)
        assert not request_task.done()
        request_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request_task
        monkeypatch.setattr(os, "write", original_write)

        os.close(first_reader)
        first_reader = -1
        second_reader = open_control_reader(transport)
        second_event = asyncio.create_task(transport.next_event())
        await feed_event(transport, "P 2\n")
        assert await asyncio.wait_for(second_event, timeout=0.25) == ProbeEvent(
            kind="probe", probe_id=2, source_id=None
        )
    finally:
        if request_task is not None and not request_task.done():
            request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)
        await transport.release()
        if first_reader >= 0:
            os.close(first_reader)
        if second_reader >= 0:
            os.close(second_reader)
        await transport.close()


@pytest.mark.asyncio
async def test_close_interrupts_stalled_control_write(tmp_path: Path) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    control_reader = open_control_reader(transport)
    request_task: asyncio.Task[str] | None = None
    try:
        event_task = asyncio.create_task(transport.next_event())
        await feed_event(transport, "P 1\n")
        await asyncio.wait_for(event_task, timeout=_IO_TIMEOUT)

        request_task = asyncio.create_task(
            transport.send_request(
                lambda request_id: "x" * (2 * 1024 * 1024), timeout=5.0
            )
        )
        await asyncio.sleep(0.02)
        assert not request_task.done()

        await asyncio.wait_for(transport.close(), timeout=0.25)
        with pytest.raises(TransportError, match="closed"):
            await asyncio.wait_for(request_task, timeout=0.25)
    finally:
        if request_task is not None and not request_task.done():
            request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)
        os.close(control_reader)
        await transport.close()


@pytest.mark.asyncio
async def test_cancelled_close_still_releases_owned_endpoints(tmp_path: Path) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    paths = (
        transport.paths.event_fifo,
        transport.paths.control_fifo,
        transport.paths.response_fifo,
    )
    pending_event = asyncio.create_task(transport.next_event())
    await asyncio.sleep(0)

    close_task = asyncio.create_task(transport.close())
    asyncio.get_running_loop().call_soon(close_task.cancel)
    with pytest.raises(asyncio.CancelledError):
        await close_task
    await asyncio.gather(pending_event, return_exceptions=True)

    assert all(not path.exists() for path in paths)
    await transport.close()


@pytest.mark.asyncio
async def test_release_only_closes_current_control_writer_and_is_idempotent(
    tmp_path: Path,
) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    first_reader = open_control_reader(transport)
    try:
        first_event = asyncio.create_task(transport.next_event())
        await feed_event(transport, "P 1\n")
        await asyncio.wait_for(first_event, timeout=_IO_TIMEOUT)
        await asyncio.wait_for(transport.release(), timeout=_IO_TIMEOUT)
        await asyncio.wait_for(transport.release(), timeout=_IO_TIMEOUT)
        os.close(first_reader)
        first_reader = -1

        second_reader = open_control_reader(transport)
        try:
            second_event = asyncio.create_task(transport.next_event())
            await feed_event(transport, "P 2\n")
            assert await asyncio.wait_for(
                second_event, timeout=_IO_TIMEOUT
            ) == ProbeEvent(kind="probe", probe_id=2, source_id=None)
            await transport.release()
        finally:
            os.close(second_reader)

        source_event = asyncio.create_task(transport.next_event())
        await feed_event(transport, "E 1\n")
        assert (
            await asyncio.wait_for(source_event, timeout=_IO_TIMEOUT)
        ).kind == "enter"
    finally:
        if first_reader >= 0:
            os.close(first_reader)
        await transport.close()


@pytest.mark.asyncio
async def test_close_is_idempotent(tmp_path: Path) -> None:
    transport = ProbeTransport.create(tmp_path / "session")

    await asyncio.wait_for(transport.close(), timeout=_IO_TIMEOUT)
    await asyncio.wait_for(transport.close(), timeout=_IO_TIMEOUT)

    with pytest.raises(TransportError, match="closed"):
        await transport.next_event()


def test_probe_fragment_reports_before_waiting(runtime_paths: RuntimePaths) -> None:
    fragment = render_probe(17, runtime_paths)

    event_line, control_line = fragment.splitlines()
    event_word = event_line.removeprefix("echo P\\ 17 >! ")
    control_word = control_line.removeprefix("source ")
    assert decode_literal_tcsh_word(event_word) == str(runtime_paths.event_fifo)
    assert decode_literal_tcsh_word(control_word) == str(runtime_paths.control_fifo)
    assert fragment.index("P\\ 17") < fragment.index("source ")


@pytest.mark.parametrize(("kind", "record"), [("enter", "E\\ 4"), ("leave", "L\\ 4")])
def test_source_event_fragment_writes_without_waiting(
    runtime_paths: RuntimePaths, kind: str, record: str
) -> None:
    fragment = render_source_event(kind, 4, runtime_paths)

    line = fragment.rstrip("\n")
    fifo_word = line.removeprefix(f"echo {record} >! ")
    assert decode_literal_tcsh_word(fifo_word) == str(runtime_paths.event_fifo)
    assert "source " not in fragment


def test_source_event_fragment_rejects_unknown_kind(
    runtime_paths: RuntimePaths,
) -> None:
    with pytest.raises(ValueError, match="event kind"):
        render_source_event("exit", 4, runtime_paths)


def test_inspection_fragment_frames_verbatim_command_and_combines_stderr(
    runtime_paths: RuntimePaths,
) -> None:
    command = "echo $value; echo problem >&2"

    fragment = render_inspection_request(9, command, runtime_paths)

    begin_line, source_line, separator_line, end_line = fragment.splitlines()
    begin_prefix = "echo 0123456789abcdef\\ BEGIN\\ 9\\ ok >! "
    end_prefix = "echo 0123456789abcdef\\ END\\ 9 >>! "
    assert decode_literal_tcsh_word(begin_line.removeprefix(begin_prefix)) == str(
        runtime_paths.response_fifo
    )
    source_word, response_word = source_line.removeprefix("source ").split(" >>&! ")
    request_path = Path(decode_literal_tcsh_word(source_word))
    assert request_path.read_text() == command + "\n"
    assert stat.S_IMODE(request_path.stat().st_mode) == 0o600
    assert decode_literal_tcsh_word(response_word) == str(runtime_paths.response_fifo)
    assert decode_literal_tcsh_word(separator_line.removeprefix("echo >>! ")) == str(
        runtime_paths.response_fifo
    )
    assert decode_literal_tcsh_word(end_line.removeprefix(end_prefix)) == str(
        runtime_paths.response_fifo
    )
    fragment.cleanup()
    assert not request_path.exists()


def test_request_creation_write_failure_preserves_primary_and_closes_descriptors(
    runtime_paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_close = os.close
    descriptors_before = {int(name) for name in os.listdir("/dev/fd")}

    def failing_write(descriptor: int, data: bytes | memoryview) -> int:
        del descriptor, data
        raise OSError("primary write failure")

    def failing_unlink(
        path: str | os.PathLike[str],
        *,
        dir_fd: int | None = None,
    ) -> None:
        del path, dir_fd
        raise PermissionError("rollback unlink failure")

    monkeypatch.setattr(os, "write", failing_write)
    monkeypatch.setattr(os, "unlink", failing_unlink)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {failing_unlink})
    leaked_descriptors: set[int] = set()
    try:
        with pytest.raises(OSError, match="primary write failure") as raised:
            render_inspection_request(41, "set value = x", runtime_paths)

        assert raised.value.__notes__ == [
            "request rollback also failed: rollback unlink failure"
        ]
        descriptors_after = {int(name) for name in os.listdir("/dev/fd")}
        leaked_descriptors = {
            descriptor
            for descriptor in descriptors_after - descriptors_before
            if not _descriptor_is_closed(descriptor)
        }
        assert leaked_descriptors == set()
    finally:
        monkeypatch.undo()
        for descriptor in leaked_descriptors:
            if not _descriptor_is_closed(descriptor):
                original_close(descriptor)


def test_request_owner_cleanup_attempts_both_descriptor_closes(
    runtime_paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fragment = render_inspection_request(42, "set value = x", runtime_paths)
    original_close = os.close
    close_calls: list[int] = []

    def close_then_fail(descriptor: int) -> None:
        close_calls.append(descriptor)
        original_close(descriptor)
        raise OSError(f"close failure {len(close_calls)}")

    monkeypatch.setattr(os, "close", close_then_fail)

    with pytest.raises(OSError, match="close failure 1") as raised:
        fragment.cleanup()

    assert len(close_calls) == 2
    assert raised.value.__notes__ == [
        "request directory close also failed: close failure 2"
    ]
    fragment.cleanup()
    assert len(close_calls) == 2


def test_request_creation_requires_nonfollowing_stat_support(
    runtime_paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        os,
        "supports_follow_symlinks",
        os.supports_follow_symlinks - {os.stat},
    )

    with pytest.raises(RuntimeError, match="descriptor-relative POSIX"):
        render_inspection_request(43, "set value = x", runtime_paths)

    assert not (runtime_paths.control_fifo.parent / "requests").exists()


@pytest.mark.asyncio
async def test_request_creation_rejects_replaced_workspace_path(tmp_path: Path) -> None:
    workspace = tmp_path / "session"
    transport = ProbeTransport.create(workspace)
    retained_workspace = tmp_path / "retained-session"
    victim = tmp_path / "victim"
    victim.mkdir()
    fragment = None
    try:
        workspace.rename(retained_workspace)
        workspace.symlink_to(victim, target_is_directory=True)

        with pytest.raises(TransportError, match="workspace path identity"):
            fragment = render_inspection_request(44, "echo safe", transport.paths)

        assert not (victim / "requests").exists()
    finally:
        if fragment is not None:
            fragment.cleanup()
        if workspace.is_symlink():
            workspace.unlink()
        if retained_workspace.exists():
            retained_workspace.rename(workspace)
        await transport.close()


@pytest.mark.asyncio
async def test_control_open_rejects_replaced_workspace_path(tmp_path: Path) -> None:
    workspace = tmp_path / "session"
    transport = ProbeTransport.create(workspace)
    retained_workspace = tmp_path / "retained-session"
    victim = tmp_path / "victim"
    victim.mkdir()
    attacker_fifo = victim / "control.fifo"
    os.mkfifo(attacker_fifo, 0o600)
    attacker_reader = os.open(attacker_fifo, os.O_RDONLY | os.O_NONBLOCK)
    restored = False
    try:
        workspace.rename(retained_workspace)
        workspace.symlink_to(victim, target_is_directory=True)

        event_task = asyncio.create_task(transport.next_event())
        await write_fifo(retained_workspace / "events.fifo", "P 45\n")
        with pytest.raises(TransportError, match="workspace path identity"):
            await asyncio.wait_for(event_task, timeout=_IO_TIMEOUT)

        assert os.read(attacker_reader, 1) == b""
    finally:
        os.close(attacker_reader)
        if workspace.is_symlink():
            workspace.unlink()
        if retained_workspace.exists():
            retained_workspace.rename(workspace)
            restored = True
        await transport.close()
        if not restored and retained_workspace.exists():
            retained_workspace.rename(workspace)


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_kind", ["symlink", "fifo"])
async def test_control_open_rejects_replaced_fifo_entry(
    tmp_path: Path,
    replacement_kind: str,
) -> None:
    transport = ProbeTransport.create(tmp_path / "session")
    control_fifo = transport.paths.control_fifo
    victim_fifo = tmp_path / "victim.fifo"
    control_fifo.unlink()
    if replacement_kind == "symlink":
        os.mkfifo(victim_fifo, 0o600)
        control_fifo.symlink_to(victim_fifo)
        attacker_fifo = victim_fifo
    else:
        os.mkfifo(control_fifo, 0o600)
        attacker_fifo = control_fifo
    attacker_reader = os.open(attacker_fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        event_task = asyncio.create_task(transport.next_event())
        await feed_event(transport, "P 46\n")
        with pytest.raises(TransportError, match="control FIFO"):
            await asyncio.wait_for(event_task, timeout=_IO_TIMEOUT)

        assert os.read(attacker_reader, 1) == b""
    finally:
        os.close(attacker_reader)
        control_fifo.unlink(missing_ok=True)
        await transport.close()


def test_inspection_fragment_emits_adapter_payload_separator(
    runtime_paths: RuntimePaths,
) -> None:
    fragment = render_inspection_request(9, "printf tail", runtime_paths)

    assert fragment.splitlines()[-2].startswith("echo >>! ")


def test_inspection_fragment_keeps_trailing_comment_inside_request_file(
    runtime_paths: RuntimePaths,
) -> None:
    command = "echo kept # trailing comment"

    fragment = render_inspection_request(9, command, runtime_paths)
    source_line = fragment.splitlines()[1]
    request_word = source_line.removeprefix("source ").split(" >>&! ", maxsplit=1)[0]
    request_path = Path(decode_literal_tcsh_word(request_word))

    assert request_path.read_text() == command + "\n"


def test_rendered_inspection_executes_before_control_fifo_is_released(
    tmp_path: Path,
    tcsh_path: Path,
) -> None:
    control_fifo = tmp_path / "control.fifo"
    response_fifo = tmp_path / "response.fifo"
    event_fifo = tmp_path / "event.fifo"
    for fifo in (control_fifo, response_fifo, event_fifo):
        os.mkfifo(fifo, 0o600)
    response_reader = os.open(response_fifo, os.O_RDONLY | os.O_NONBLOCK)
    response_keeper = os.open(response_fifo, os.O_WRONLY | os.O_NONBLOCK)
    process = subprocess.Popen(
        [str(tcsh_path), "-f", "-c", f"source {control_fifo}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    control_writer = -1
    workspace_descriptor = os.open(tmp_path, _DIRECTORY_OPEN_FLAGS)
    os.fchmod(workspace_descriptor, 0o700)
    try:
        deadline = time.monotonic() + _IO_TIMEOUT
        while control_writer < 0:
            try:
                control_writer = os.open(control_fifo, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as error:
                if error.errno != errno.ENXIO or time.monotonic() >= deadline:
                    raise
                time.sleep(0.005)
        paths = RuntimePaths(
            event_fifo,
            control_fifo,
            response_fifo,
            "live123",
            workspace_descriptor,
        )

        command = (
            "set live_value = changed\n"
            "echo $live_value # trailing comment\n"
            "/bin/sh -c 'echo captured-stderr >&2'"
        )
        fragment = render_inspection_request(7, command, paths)
        os.write(control_writer, fragment.encode())

        assert _read_line(response_reader) == b"live123 BEGIN 7 ok\n"
        assert _read_line(response_reader) == b"changed\n"
        assert _read_line(response_reader) == b"captured-stderr\n"
        assert _read_line(response_reader) == b"\n"
        assert _read_line(response_reader) == b"live123 END 7\n"
        fragment.cleanup()
        assert not (tmp_path / "requests" / "request-7.csh").exists()
        os.write(
            control_writer, f"echo current=$live_value >>&! {response_fifo}\n".encode()
        )
        assert _read_line(response_reader) == b"current=changed\n"
        assert process.poll() is None
    finally:
        if control_writer >= 0:
            os.close(control_writer)
        stdout, stderr = process.communicate(timeout=_IO_TIMEOUT)
        os.close(response_reader)
        os.close(response_keeper)
        os.close(workspace_descriptor)
    assert process.returncode == 0, (stdout, stderr)


def test_rendered_inspection_removes_request_file_after_command_failure(
    tmp_path: Path,
    tcsh_path: Path,
) -> None:
    control_fifo = tmp_path / "control.fifo"
    response_fifo = tmp_path / "response.fifo"
    event_fifo = tmp_path / "event.fifo"
    for fifo in (control_fifo, response_fifo, event_fifo):
        os.mkfifo(fifo, 0o600)
    response_reader = os.open(response_fifo, os.O_RDONLY | os.O_NONBLOCK)
    response_keeper = os.open(response_fifo, os.O_WRONLY | os.O_NONBLOCK)
    process = subprocess.Popen(
        [str(tcsh_path), "-f", "-c", f"source {control_fifo}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    control_writer = -1
    workspace_descriptor = os.open(tmp_path, _DIRECTORY_OPEN_FLAGS)
    os.fchmod(workspace_descriptor, 0o700)
    try:
        deadline = time.monotonic() + _IO_TIMEOUT
        while control_writer < 0:
            try:
                control_writer = os.open(control_fifo, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as error:
                if error.errno != errno.ENXIO or time.monotonic() >= deadline:
                    raise
                time.sleep(0.005)
        paths = RuntimePaths(
            event_fifo,
            control_fifo,
            response_fifo,
            "error123",
            workspace_descriptor,
        )

        fragment = render_inspection_request(8, "/bin/false", paths)
        os.write(control_writer, fragment.encode())

        assert _read_line(response_reader) == b"error123 BEGIN 8 ok\n"
        assert _read_line(response_reader) == b"\n"
        assert _read_line(response_reader) == b"error123 END 8\n"
        fragment.cleanup()
        assert not (tmp_path / "requests" / "request-8.csh").exists()
        assert process.poll() is None
    finally:
        if control_writer >= 0:
            os.close(control_writer)
        stdout, stderr = process.communicate(timeout=_IO_TIMEOUT)
        os.close(response_reader)
        os.close(response_keeper)
        os.close(workspace_descriptor)
    assert process.returncode == 0, (stdout, stderr)


def test_request_cleanup_cannot_follow_replaced_directory_to_victim(
    tmp_path: Path,
    tcsh_path: Path,
) -> None:
    control_fifo = tmp_path / "control.fifo"
    response_fifo = tmp_path / "response.fifo"
    event_fifo = tmp_path / "event.fifo"
    for fifo in (control_fifo, response_fifo, event_fifo):
        os.mkfifo(fifo, 0o600)
    victim_directory = tmp_path / "victim"
    victim_directory.mkdir()
    victim_file = victim_directory / "request-9.csh"
    victim_file.write_text("do not delete")
    moved_requests = tmp_path / "original-requests"
    response_reader = os.open(response_fifo, os.O_RDONLY | os.O_NONBLOCK)
    response_keeper = os.open(response_fifo, os.O_WRONLY | os.O_NONBLOCK)
    process = subprocess.Popen(
        [str(tcsh_path), "-f", "-c", f"source {control_fifo}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    control_writer = -1
    workspace_descriptor = os.open(tmp_path, _DIRECTORY_OPEN_FLAGS)
    os.fchmod(workspace_descriptor, 0o700)
    try:
        deadline = time.monotonic() + _IO_TIMEOUT
        while control_writer < 0:
            try:
                control_writer = os.open(control_fifo, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as error:
                if error.errno != errno.ENXIO or time.monotonic() >= deadline:
                    raise
                time.sleep(0.005)
        paths = RuntimePaths(
            event_fifo,
            control_fifo,
            response_fifo,
            "secure123",
            workspace_descriptor,
        )
        command = (
            f"/bin/mv {tmp_path / 'requests'} {moved_requests}\n"
            f"/bin/ln -s {victim_directory} {tmp_path / 'requests'}"
        )

        fragment = render_inspection_request(9, command, paths)
        os.write(control_writer, fragment.encode())

        assert _read_line(response_reader) == b"secure123 BEGIN 9 ok\n"
        assert _read_line(response_reader) == b"\n"
        assert _read_line(response_reader) == b"secure123 END 9\n"
        fragment.cleanup()
        assert victim_file.read_text() == "do not delete"
        assert not (moved_requests / "request-9.csh").exists()
    finally:
        if control_writer >= 0:
            os.close(control_writer)
        stdout, stderr = process.communicate(timeout=_IO_TIMEOUT)
        os.close(response_reader)
        os.close(response_keeper)
        os.close(workspace_descriptor)
    assert process.returncode == 0, (stdout, stderr)


def test_runtime_fragments_force_redirection_to_existing_fifos(
    runtime_paths: RuntimePaths,
) -> None:
    probe_lines = render_probe(1, runtime_paths).splitlines()
    source_lines = render_source_event("enter", 2, runtime_paths).splitlines()
    inspection_lines = render_inspection_request(
        3, "echo value", runtime_paths
    ).splitlines()

    assert " >! " in probe_lines[0]
    assert " >! " in source_lines[0]
    assert " >! " in inspection_lines[0]
    assert inspection_lines[1].startswith("source ")
    assert " >>&! " in inspection_lines[1]
    assert inspection_lines[-2].startswith("echo >>! ")
    assert " >>! " in inspection_lines[-1]


@pytest.mark.parametrize("invalid", [False, True])
@pytest.mark.parametrize("renderer", ["probe", "source", "inspection"])
def test_runtime_renderers_reject_boolean_ids(
    runtime_paths: RuntimePaths,
    renderer: str,
    invalid: bool,
) -> None:
    with pytest.raises(TypeError, match="integer"):
        if renderer == "probe":
            render_probe(invalid, runtime_paths)
        elif renderer == "source":
            render_source_event("enter", invalid, runtime_paths)
        else:
            render_inspection_request(invalid, "set", runtime_paths)


@pytest.mark.parametrize("invalid", [-1, -100])
@pytest.mark.parametrize("renderer", ["probe", "source", "inspection"])
def test_runtime_renderers_reject_negative_ids(
    runtime_paths: RuntimePaths,
    renderer: str,
    invalid: int,
) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        if renderer == "probe":
            render_probe(invalid, runtime_paths)
        elif renderer == "source":
            render_source_event("enter", invalid, runtime_paths)
        else:
            render_inspection_request(invalid, "set", runtime_paths)


@pytest.mark.parametrize("invalid", ["bad\npath", "bad\rpath", "bad\x00path"])
@pytest.mark.parametrize("renderer", ["probe", "source", "inspection"])
def test_runtime_fragments_reject_unsafe_path_operands(
    invalid: str, renderer: str
) -> None:
    paths = RuntimePaths(Path(invalid), Path(invalid), Path(invalid), "safe")

    with pytest.raises(ValueError, match="NUL|newline"):
        if renderer == "probe":
            render_probe(1, paths)
        elif renderer == "source":
            render_source_event("enter", 1, paths)
        else:
            render_inspection_request(1, "set", paths)


def test_inspection_fragment_rejects_nul_command(runtime_paths: RuntimePaths) -> None:
    with pytest.raises(ValueError, match="NUL"):
        render_inspection_request(1, "echo before\x00after", runtime_paths)


@pytest.mark.skipif(TCSH is None, reason="tcsh is not installed")
def test_rendered_source_event_executes_with_noclobber(tmp_path: Path) -> None:
    event_fifo = tmp_path / "events.fifo"
    os.mkfifo(event_fifo, 0o600)
    reader = os.open(event_fifo, os.O_RDONLY | os.O_NONBLOCK)
    paths = RuntimePaths(
        event_fifo, tmp_path / "control", tmp_path / "response", "abc123"
    )
    try:
        completed = subprocess.run(
            [
                TCSH,
                "-f",
                "-c",
                "set noclobber\n" + render_source_event("enter", 7, paths),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=_IO_TIMEOUT,
        )
        assert completed.returncode == 0, completed.stderr
        assert _read_line(reader) == b"E 7\n"
    finally:
        os.close(reader)


@pytest.mark.skipif(TCSH is None, reason="tcsh is not installed")
def test_rendered_source_event_executes_with_metacharacters_in_fifo_path(
    tmp_path: Path,
) -> None:
    unusual = tmp_path / "fifo space $d `cmd` !*?[]{}~%'\\\""
    unusual.mkdir()
    event_fifo = unusual / "event stream"
    os.mkfifo(event_fifo, 0o600)
    reader = os.open(event_fifo, os.O_RDONLY | os.O_NONBLOCK)
    paths = RuntimePaths(
        event_fifo, unusual / "control", unusual / "response", "abc123"
    )
    try:
        completed = subprocess.run(
            [TCSH, "-f", "-c", render_source_event("enter", 7, paths)],
            check=False,
            capture_output=True,
            text=True,
            timeout=_IO_TIMEOUT,
        )
        assert completed.returncode == 0, completed.stderr
        assert _read_line(reader) == b"E 7\n"
    finally:
        os.close(reader)


@pytest.mark.asyncio
async def test_control_fifo_inode_is_pinned_for_transport_lifetime(
    tmp_path: Path,
) -> None:
    """The pin makes identity checks sound on inode-recycling filesystems.

    Overlayfs (docker) reuses a freed inode number immediately, so
    without a held descriptor an unlink-and-recreate of control.fifo
    would be indistinguishable from the FIFO the transport created.
    """

    transport = ProbeTransport.create(tmp_path / "session")
    try:
        if not hasattr(os, "O_PATH"):
            pytest.skip("inode pinning requires O_PATH")
        pin = transport._control_pin_descriptor
        assert pin is not None
        status = os.fstat(pin)
        assert stat.S_ISFIFO(status.st_mode)
        assert (status.st_dev, status.st_ino) == transport._fifo_identities[
            transport.paths.control_fifo.name
        ]
    finally:
        await transport.close()
    assert transport._control_pin_descriptor is None

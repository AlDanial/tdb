"""Render adapter-owned tcsh fragments for the private FIFO runtime."""

from __future__ import annotations

import os
import stat
import string
from contextlib import suppress
from pathlib import Path
from typing import Self

from tdb.adapters.tcsh.transport import (
    RuntimePaths,
    TransportError,
    duplicate_workspace_descriptor,
    verify_workspace_descriptor,
)

_SAFE_TCSH_WORD_CHARACTERS = frozenset(string.ascii_letters + string.digits + "/._-:")
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW


class _RequestFileOwner:
    def __init__(
        self, path: Path, directory_descriptor: int, file_descriptor: int
    ) -> None:
        self.path = path
        self._directory_descriptor = directory_descriptor
        self._file_descriptor = file_descriptor
        self._cleaned = False

    def cleanup(self) -> None:
        """Unlink only the exact request file created through the retained directory."""

        if self._cleaned:
            return
        self._cleaned = True
        primary_error: BaseException | None = None
        try:
            original = os.fstat(self._file_descriptor)
            try:
                current = os.stat(
                    self.path.name,
                    dir_fd=self._directory_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                current = None
            if current is not None and (
                stat.S_ISREG(current.st_mode)
                and current.st_dev == original.st_dev
                and current.st_ino == original.st_ino
            ):
                os.unlink(self.path.name, dir_fd=self._directory_descriptor)
        except OSError as error:
            primary_error = error
        finally:
            primary_error = _close_descriptors(
                primary_error,
                (
                    (self._file_descriptor, "request file close also failed"),
                    (self._directory_descriptor, "request directory close also failed"),
                ),
            )
        if primary_error is not None:
            raise primary_error


class _InspectionRequest(str):
    """A string-compatible control body retaining safe request-file ownership."""

    def __new__(cls, body: str, owner: _RequestFileOwner) -> Self:
        request = super().__new__(cls, body)
        request._owner = owner
        return request

    def cleanup(self) -> None:
        self._owner.cleanup()

    def __del__(self) -> None:
        with suppress(Exception):
            self.cleanup()


def render_probe(probe_id: int, paths: RuntimePaths) -> str:
    """Report *probe_id* before sourcing the probe's control channel."""

    _validate_identifier(probe_id, "probe ID")
    event_fifo = _quote_tcsh_word(str(paths.event_fifo))
    control_fifo = _quote_tcsh_word(str(paths.control_fifo))
    record = _quote_tcsh_word(f"P {probe_id}")
    return f"echo {record} >! {event_fifo}\nsource {control_fifo}\n"


def render_source_event(kind: str, source_id: int, paths: RuntimePaths) -> str:
    """Render a nonblocking source-stack event write."""

    _validate_identifier(source_id, "source ID")
    markers = {"enter": "E", "leave": "L"}
    try:
        marker = markers[kind]
    except KeyError as error:
        raise ValueError(f"unknown source event kind: {kind!r}") from error
    event_fifo = _quote_tcsh_word(str(paths.event_fifo))
    record = _quote_tcsh_word(f"{marker} {source_id}")
    return f"echo {record} >! {event_fifo}\n"


def render_inspection_request(
    request_id: int, command: str, paths: RuntimePaths
) -> str:
    """Frame one verbatim adapter-owned command and its combined output."""

    _validate_identifier(request_id, "request ID")
    if "\x00" in command:
        raise ValueError("tcsh commands cannot contain NUL")
    response_fifo = _quote_tcsh_word(str(paths.response_fifo))
    request_owner = _write_request_file(request_id, command, paths)
    request_word = _quote_tcsh_word(str(request_owner.path))
    begin = _quote_tcsh_word(f"{paths.nonce} BEGIN {request_id} ok")
    end = _quote_tcsh_word(f"{paths.nonce} END {request_id}")
    body = (
        f"echo {begin} >! {response_fifo}\n"
        f"source {request_word} >>&! {response_fifo}\n"
        f"echo >>! {response_fifo}\n"
        f"echo {end} >>! {response_fifo}\n"
    )
    return _InspectionRequest(body, request_owner)


def _write_request_file(
    request_id: int,
    command: str,
    paths: RuntimePaths,
) -> _RequestFileOwner:
    if (
        not {os.mkdir, os.open, os.stat, os.unlink}.issubset(os.supports_dir_fd)
        or os.stat not in os.supports_follow_symlinks
    ):
        raise RuntimeError(
            "inspection requests require descriptor-relative POSIX file APIs"
        )
    request_directory = paths.control_fifo.parent / "requests"
    workspace_descriptor = duplicate_workspace_descriptor(paths)
    created_directory = False
    try:
        os.mkdir("requests", mode=0o700, dir_fd=workspace_descriptor)
        created_directory = True
    except FileExistsError:
        pass
    except BaseException as error:
        _close_descriptors(
            error,
            ((workspace_descriptor, "workspace descriptor close also failed"),),
        )
        raise
    try:
        directory_descriptor = os.open(
            "requests",
            _DIRECTORY_OPEN_FLAGS,
            dir_fd=workspace_descriptor,
        )
    except OSError as error:
        _close_descriptors(
            error,
            ((workspace_descriptor, "workspace descriptor close also failed"),),
        )
        raise ValueError("inspection request directory is not private") from error
    try:
        if created_directory:
            os.fchmod(directory_descriptor, 0o700)
        descriptor_status = os.fstat(directory_descriptor)
        entry_status = os.stat(
            "requests",
            dir_fd=workspace_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(descriptor_status.st_mode)
            or stat.S_IMODE(descriptor_status.st_mode) != 0o700
            or not stat.S_ISDIR(entry_status.st_mode)
            or descriptor_status.st_dev != entry_status.st_dev
            or descriptor_status.st_ino != entry_status.st_ino
        ):
            raise ValueError("inspection request directory is not private")
    except BaseException as error:
        _close_descriptors(
            error,
            (
                (directory_descriptor, "request directory close also failed"),
                (workspace_descriptor, "workspace descriptor close also failed"),
            ),
        )
        raise
    basename = f"request-{request_id}.csh"
    request_path = request_directory / basename
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(basename, flags, 0o600, dir_fd=directory_descriptor)
    except BaseException as error:
        _close_descriptors(
            error,
            (
                (directory_descriptor, "request directory close also failed"),
                (workspace_descriptor, "workspace descriptor close also failed"),
            ),
        )
        raise
    try:
        os.fchmod(descriptor, 0o600)
        data = (command + "\n").encode("utf-8", errors="surrogateescape")
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written == 0:
                raise OSError("request file accepted no bytes")
            view = view[written:]
        os.fsync(descriptor)
        file_status = os.fstat(descriptor)
        entry_status = os.stat(
            basename, dir_fd=directory_descriptor, follow_symlinks=False
        )
        if (
            not stat.S_ISREG(file_status.st_mode)
            or stat.S_IMODE(file_status.st_mode) != 0o600
            or file_status.st_dev != entry_status.st_dev
            or file_status.st_ino != entry_status.st_ino
        ):
            raise ValueError("inspection request file identity changed during creation")
        verify_workspace_descriptor(paths, workspace_descriptor)
    except BaseException as error:
        try:
            os.unlink(basename, dir_fd=directory_descriptor)
        except FileNotFoundError:
            pass
        except OSError as cleanup_error:
            error.add_note(f"request rollback also failed: {cleanup_error}")
        finally:
            _close_descriptors(
                error,
                (
                    (descriptor, "request file close also failed"),
                    (directory_descriptor, "request directory close also failed"),
                    (workspace_descriptor, "workspace descriptor close also failed"),
                ),
            )
        raise
    close_error = _close_descriptors(
        None,
        ((workspace_descriptor, "workspace descriptor close also failed"),),
    )
    if close_error is not None:
        try:
            os.unlink(basename, dir_fd=directory_descriptor)
        except FileNotFoundError:
            pass
        except OSError as cleanup_error:
            close_error.add_note(f"request rollback also failed: {cleanup_error}")
        _close_descriptors(
            close_error,
            (
                (descriptor, "request file close also failed"),
                (directory_descriptor, "request directory close also failed"),
            ),
        )
        raise TransportError(
            "could not close runtime workspace authority"
        ) from close_error
    return _RequestFileOwner(request_path, directory_descriptor, descriptor)


def _close_descriptors(
    primary_error: BaseException | None,
    descriptors: tuple[tuple[int, str], ...],
) -> BaseException | None:
    """Attempt every close while preserving the first failure deterministically."""

    for descriptor, note in descriptors:
        try:
            os.close(descriptor)
        except OSError as close_error:
            if primary_error is None:
                primary_error = close_error
            else:
                primary_error.add_note(f"{note}: {close_error}")
    return primary_error


def _validate_identifier(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")


def _quote_tcsh_word(value: str) -> str:
    """Encode one literal tcsh word without expansion or word splitting."""

    _validate_path_operand(value)
    if not value:
        return "''"
    return "".join(
        character if character in _SAFE_TCSH_WORD_CHARACTERS else "\\" + character
        for character in value
    )


def _validate_path_operand(value: str | Path) -> None:
    text = str(value)
    if "\x00" in text:
        raise ValueError("tcsh words cannot contain NUL")
    if "\n" in text or "\r" in text:
        raise ValueError("tcsh words cannot contain a newline")

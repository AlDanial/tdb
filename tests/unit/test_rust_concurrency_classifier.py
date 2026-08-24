"""Portable stack classification contracts for Rust standard-library waits."""

from __future__ import annotations

import pytest

from tdb.rust_concurrency.classifier import classify_snapshot, classify_thread
from tdb.rust_concurrency.models import (
    Confidence,
    Evidence,
    PrimitiveKind,
    RawFrame,
    RawSnapshot,
    RawThread,
    RawVariable,
    ThreadState,
)


def raw_with_top_frame(
    name: str, variables: tuple[RawVariable, ...] = ()
) -> RawSnapshot:
    """Build a one-thread, debugger-neutral stopped snapshot."""
    return RawSnapshot(
        adapter="gdb",
        platform="linux",
        rust_version="1.98.0",
        threads=(
            RawThread(
                thread_id=7,
                name="worker",
                frames=(
                    RawFrame(
                        frame_id=1,
                        name=name,
                        source_path=None,
                        line=0,
                        variables=variables,
                    ),
                ),
            ),
        ),
    )


@pytest.mark.parametrize(
    ("frame_name", "expected_operation", "expected_kind"),
    [
        ("std::thread::JoinHandle<T>::join", "join", PrimitiveKind.THREAD),
        (
            "std::thread::join_handle::JoinHandle<()>::join<()>",
            "join",
            PrimitiveKind.THREAD,
        ),
        (
            "std::sync::poison::mutex::Mutex<T>::lock",
            "mutex-lock",
            PrimitiveKind.MUTEX,
        ),
        (
            "std::sync::poison::rwlock::RwLock<T>::read",
            "rwlock-read",
            PrimitiveKind.RWLOCK,
        ),
        (
            "std::sync::poison::rwlock::RwLock<T>::write",
            "rwlock-write",
            PrimitiveKind.RWLOCK,
        ),
        (
            "std::sync::poison::condvar::Condvar::wait",
            "condvar-wait",
            PrimitiveKind.CONDVAR,
        ),
        ("std::sync::mpsc::Receiver<T>::recv", "mpsc-recv", PrimitiveKind.CHANNEL),
        (
            "std::sync::mpsc::SyncSender<T>::send",
            "mpsc-send",
            PrimitiveKind.CHANNEL,
        ),
        ("std::thread::park", "park", PrimitiveKind.PARKER),
        ("std::thread::functions::park", "park", PrimitiveKind.PARKER),
    ],
)
def test_classifies_supported_waits(frame_name, expected_operation, expected_kind):
    analyses, primitives, edges = classify_snapshot(raw_with_top_frame(frame_name))

    assert analyses[0].state is ThreadState.BLOCKED
    assert edges[0].operation == expected_operation
    assert primitives[0].kind is expected_kind
    assert edges[0].primitive_id == f"unknown:{expected_kind.value}:7"
    assert edges[0].evidence == (Evidence(Confidence.UNKNOWN, "stack", frame_name),)


@pytest.mark.parametrize(
    "frame_name",
    [
        "my_app::parking_lot::park",
        "futex_waitv",
        "_RNvNtCs7R42Foo4main4park",
        "std::::thread::park",
        "std::sync::mpsc::Sender<T>::send",
    ],
)
def test_unrecognized_application_and_platform_frames_are_not_waits(frame_name):
    analyses, primitives, edges = classify_snapshot(raw_with_top_frame(frame_name))

    assert analyses == (classify_thread(raw_with_top_frame(frame_name).threads[0]),)
    assert analyses[0].state is ThreadState.UNKNOWN
    assert primitives == ()
    assert edges == ()


@pytest.mark.parametrize(
    "frame_name",
    [
        "std::sync::poison::mutex::Mutex::<u8>::lock",
        "std::sync::poison::mutex::Mutex$LT$u8$GT$::lock",
    ],
)
def test_generic_demangling_variants_normalize_before_matching(frame_name):
    analyses, primitives, edges = classify_snapshot(raw_with_top_frame(frame_name))

    assert analyses[0].state is ThreadState.BLOCKED
    assert primitives[0].kind is PrimitiveKind.MUTEX
    assert edges[0].operation == "mutex-lock"


def test_address_requires_a_typed_variable_and_full_hexadecimal_token():
    raw = raw_with_top_frame(
        "std::sync::poison::mutex::Mutex<T>::lock",
        (
            RawVariable("untyped", "Mutex { ptr: 0xdeadbeef }", ""),
            RawVariable("decimal", "Mutex { ptr: 1234 }", "Mutex<u8>"),
            RawVariable("mutex", "Mutex { ptr: 0x00aBcD }", "Mutex<u8>"),
        ),
    )

    analyses, primitives, edges = classify_snapshot(raw)

    assert analyses[0].state is ThreadState.BLOCKED
    assert primitives[0].address == "0x00aBcD"
    assert primitives[0].primitive_id == "mutex:0x00aBcD"
    assert edges[0].evidence == (
        Evidence(Confidence.PROBABLE, "frame-variable", "mutex=0x00aBcD"),
    )


def test_specific_rust_wait_takes_precedence_over_its_park_implementation():
    raw = RawSnapshot(
        adapter="gdb",
        platform="linux",
        rust_version="1.98.0",
        threads=(
            RawThread(
                thread_id=7,
                name="worker",
                frames=(
                    RawFrame(1, "std::thread::park", None, 0),
                    RawFrame(2, "std::sync::mpsc::Receiver<T>::recv", None, 0),
                ),
            ),
        ),
    )

    analyses, primitives, edges = classify_snapshot(raw)

    assert analyses[0].state is ThreadState.BLOCKED
    assert primitives[0].kind is PrimitiveKind.CHANNEL
    assert edges[0].operation == "mpsc-recv"


def test_platform_wait_only_corroborates_an_already_recognized_rust_wait():
    raw = RawSnapshot(
        adapter="gdb",
        platform="linux",
        rust_version="1.98.0",
        threads=(
            RawThread(
                thread_id=7,
                name="worker",
                frames=(
                    RawFrame(1, "futex_wait", None, 0),
                    RawFrame(
                        2,
                        "std::sync::poison::mutex::Mutex<T>::lock",
                        None,
                        0,
                    ),
                ),
            ),
        ),
    )

    analyses, _, edges = classify_snapshot(raw)

    assert analyses[0].state is ThreadState.BLOCKED
    assert edges[0].evidence == (
        Evidence(
            Confidence.UNKNOWN,
            "stack",
            "std::sync::poison::mutex::Mutex<T>::lock",
        ),
        Evidence(
            Confidence.PROBABLE,
            "platform-wait",
            "futex_wait",
        ),
    )


def test_application_frame_named_like_futex_does_not_corroborate_a_rust_wait():
    raw = RawSnapshot(
        adapter="gdb",
        platform="linux",
        rust_version="1.98.0",
        threads=(
            RawThread(
                thread_id=7,
                name="worker",
                frames=(
                    RawFrame(1, "my_app::futex_wait", None, 0),
                    RawFrame(
                        2,
                        "std::sync::poison::mutex::Mutex<T>::lock",
                        None,
                        0,
                    ),
                ),
            ),
        ),
    )

    _, _, edges = classify_snapshot(raw)

    assert edges[0].evidence == (
        Evidence(
            Confidence.UNKNOWN,
            "stack",
            "std::sync::poison::mutex::Mutex<T>::lock",
        ),
    )

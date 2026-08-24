"""Contracts for Rust concurrency observations and analysis results."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from tdb.rust_concurrency.models import (
    Confidence,
    ConcurrencySnapshot,
    Evidence,
    Finding,
    FindingKind,
    Primitive,
    PrimitiveKind,
    ThreadAnalysis,
    ThreadState,
    WaitEdge,
)


def test_snapshot_serialization_preserves_the_public_graph_schema():
    """A transport consumer receives stable JSON-ready scalar values."""
    evidence = Evidence(Confidence.CONFIRMED, "frame-argument", "self=0x10")
    edge = WaitEdge(
        waiter_thread_id=1,
        primitive_id="mutex:0x10",
        owner_thread_id=2,
        operation="mutex-lock",
        evidence=(evidence,),
    )
    snapshot = ConcurrencySnapshot(
        rust_version="1.98.0",
        adapter="gdb",
        platform="linux",
        threads=(
            ThreadAnalysis(
                thread_id=1,
                name="main",
                state=ThreadState.BLOCKED,
                wait=edge,
            ),
        ),
        primitives=(
            Primitive(
                primitive_id="mutex:0x10",
                kind=PrimitiveKind.MUTEX,
                address="0x10",
                label="Mutex at 0x10",
                evidence=(evidence,),
            ),
        ),
        edges=(edge,),
        confirmed_deadlocks=(
            Finding(
                kind=FindingKind.CONFIRMED_DEADLOCK,
                thread_ids=(1, 2),
                summary="closed mutex wait cycle",
            ),
        ),
        suspected_stalls=(
            Finding(
                kind=FindingKind.WHOLE_PROGRAM_STALL,
                thread_ids=(1,),
                summary="all application threads are blocked",
                evidence_gaps=("mutex owner unavailable",),
            ),
        ),
        warnings=("stack-only classification",),
    )

    assert snapshot.to_dict() == {
        "rust_version": "1.98.0",
        "adapter": "gdb",
        "platform": "linux",
        "threads": [
            {
                "thread_id": 1,
                "name": "main",
                "state": "blocked",
                "wait": {
                    "waiter_thread_id": 1,
                    "primitive_id": "mutex:0x10",
                    "owner_thread_id": 2,
                    "operation": "mutex-lock",
                    "evidence": [
                        {
                            "confidence": "confirmed",
                            "source": "frame-argument",
                            "detail": "self=0x10",
                        },
                    ],
                },
            },
        ],
        "primitives": [
            {
                "primitive_id": "mutex:0x10",
                "kind": "mutex",
                "address": "0x10",
                "label": "Mutex at 0x10",
                "evidence": [
                    {
                        "confidence": "confirmed",
                        "source": "frame-argument",
                        "detail": "self=0x10",
                    },
                ],
            },
        ],
        "edges": [
            {
                "waiter_thread_id": 1,
                "primitive_id": "mutex:0x10",
                "owner_thread_id": 2,
                "operation": "mutex-lock",
                "evidence": [
                    {
                        "confidence": "confirmed",
                        "source": "frame-argument",
                        "detail": "self=0x10",
                    },
                ],
            },
        ],
        "confirmed_deadlocks": [
            {
                "kind": "confirmed_deadlock",
                "thread_ids": [1, 2],
                "summary": "closed mutex wait cycle",
                "evidence_gaps": [],
            },
        ],
        "suspected_stalls": [
            {
                "kind": "whole_program_stall",
                "thread_ids": [1],
                "summary": "all application threads are blocked",
                "evidence_gaps": ["mutex owner unavailable"],
            },
        ],
        "warnings": ["stack-only classification"],
    }


def test_models_are_immutable():
    """Collected evidence cannot be changed after it becomes graph input."""
    evidence = Evidence(Confidence.CONFIRMED, "frame-argument", "self=0x10")

    with pytest.raises(FrozenInstanceError):
        evidence.detail = "changed"

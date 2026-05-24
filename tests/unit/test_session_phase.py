"""Contract tests for SessionPhase + DebugState lifecycle properties.

Pins the single-source-of-truth design from #5: phase is the only
mutable lifecycle field, every legacy boolean is a read-only property
derived from it, and `transition_to` is the sole mutator. These tests
exist so a future change that re-introduces a setter (or breaks the
RUNNING/STOPPED/TERMINATED/POST_MORTEM mapping) fails loudly.
"""

from __future__ import annotations

import pytest

from tdb.session.state import DebugState, SessionPhase


# --- Initial state -----------------------------------------------------


def test_initial_phase_is_not_started():
    s = DebugState()
    assert s.phase is SessionPhase.NOT_STARTED


def test_initial_derived_booleans():
    """A brand-new state should look exactly like 'no session at all'."""
    s = DebugState()
    assert s.is_ready is False
    assert s.is_running is False
    assert s.is_terminated is False
    assert s.is_post_mortem is False
    assert s.can_send_dap is False
    assert s.can_step is False
    assert s.can_evaluate is False


# --- Transition matrix --------------------------------------------------
# Every phase must be reachable from every other phase via transition_to.
# This is intentionally permissive — the controller knows which transitions
# are actually meaningful; the state machine itself does not gatekeep.


@pytest.mark.parametrize("src", list(SessionPhase))
@pytest.mark.parametrize("dst", list(SessionPhase))
def test_transition_to_any_phase(src, dst):
    s = DebugState()
    s.transition_to(src)
    assert s.phase is src
    s.transition_to(dst)
    assert s.phase is dst


# --- Derived-boolean mapping per phase ---------------------------------


@pytest.mark.parametrize(
    "phase, is_ready, is_running, is_terminated, is_post_mortem",
    [
        (SessionPhase.NOT_STARTED, False, False, False, False),
        (SessionPhase.RUNNING, True, True, False, False),
        (SessionPhase.STOPPED, True, False, False, False),
        (SessionPhase.TERMINATED, True, False, True, False),
        (SessionPhase.POST_MORTEM, True, False, True, True),
    ],
)
def test_derived_booleans_per_phase(
    phase,
    is_ready,
    is_running,
    is_terminated,
    is_post_mortem,
):
    s = DebugState()
    s.transition_to(phase)
    assert s.is_ready is is_ready
    assert s.is_running is is_running
    assert s.is_terminated is is_terminated
    assert s.is_post_mortem is is_post_mortem


def test_is_terminated_includes_post_mortem():
    """Both TERMINATED and POST_MORTEM share the 'no live DAP' invariant
    that the 17 guard sites on the controller rely on. If this ever
    splits, those guards must be audited."""
    s = DebugState()
    s.transition_to(SessionPhase.POST_MORTEM)
    assert s.is_terminated is True


# --- Capability properties ---------------------------------------------


@pytest.mark.parametrize(
    "phase, can_send_dap, can_step, can_evaluate",
    [
        (SessionPhase.NOT_STARTED, False, False, False),
        (SessionPhase.RUNNING, True, False, False),
        (SessionPhase.STOPPED, True, True, True),
        (SessionPhase.TERMINATED, False, False, False),
        (SessionPhase.POST_MORTEM, False, False, False),
    ],
)
def test_capabilities_per_phase(phase, can_send_dap, can_step, can_evaluate):
    s = DebugState()
    s.transition_to(phase)
    assert s.can_send_dap is can_send_dap
    assert s.can_step is can_step
    assert s.can_evaluate is can_evaluate


# --- Read-only enforcement ---------------------------------------------
# Direct assignment to the legacy booleans must raise AttributeError —
# this is the protection that forces every mutation through transition_to.


@pytest.mark.parametrize(
    "attr",
    [
        "is_ready",
        "is_running",
        "is_terminated",
        "is_post_mortem",
        "can_send_dap",
        "can_step",
        "can_evaluate",
    ],
)
def test_legacy_boolean_setters_are_blocked(attr):
    s = DebugState()
    with pytest.raises(AttributeError):
        setattr(s, attr, True)


# --- transition_to is idempotent --------------------------------------


def test_transition_to_same_phase_is_a_noop():
    s = DebugState()
    s.transition_to(SessionPhase.RUNNING)
    s.transition_to(SessionPhase.RUNNING)
    assert s.phase is SessionPhase.RUNNING

"""LanguageProfile datatypes: defaults, immutability, filter picking."""

import pytest

from tdb.dap.types import Capabilities
from tdb.languages.base import (
    AdapterNotFoundError,
    AdapterQuirks,
    AdapterSpec,
    LanguageProfile,
    Presentation,
    ProfileCapabilities,
)


class _StubAdapter(AdapterSpec):
    id = "stub"

    def command(self):
        return ["stub-adapter"]

    def launch_body(self, *, program, args, cwd, env, stop_on_entry, console, opts):
        return {"request": "launch", "program": program}

    def attach_body(self, *, host, port, opts):
        return {"request": "attach"}


def _profile() -> LanguageProfile:
    return LanguageProfile(
        id="stub",
        display_name="Stub",
        adapter=_StubAdapter(),
        presentation=Presentation(),
        capabilities=ProfileCapabilities(),
    )


def test_capability_defaults_are_all_off():
    caps = ProfileCapabilities()
    assert caps.compute_step_units is None
    assert caps.child_process_strategy is None
    assert caps.task_inspection is False


def test_quirks_default_off():
    assert AdapterQuirks().pre_arm_pause_on_attach is False
    assert AdapterSpec.quirks.pre_arm_pause_on_attach is False


def test_presentation_default_lexer_is_text():
    assert Presentation().lexer == "text"


def test_profile_is_frozen():
    profile = _profile()
    with pytest.raises(AttributeError):
        profile.id = "other"


def test_default_exception_filters_picks_adapter_defaults():
    caps = Capabilities()
    caps.exception_breakpoint_filters = [
        {"filter": "throw", "label": "On throw", "default": False},
        {"filter": "uncaught", "label": "Uncaught", "default": True},
    ]
    assert _StubAdapter().pick_exception_filters(caps) == ["uncaught"]


def test_default_exception_filters_empty_when_none_advertised():
    assert _StubAdapter().pick_exception_filters(Capabilities()) == []


def test_adapter_not_found_error_carries_hint():
    err = AdapterNotFoundError("install LLVM")
    assert err.hint == "install LLVM"
    assert "install LLVM" in str(err)


def test_adapter_quirks_attach_via_adapter_defaults_false():
    from tdb.languages.base import AdapterQuirks

    assert AdapterQuirks().attach_via_adapter is False
    assert AdapterQuirks(attach_via_adapter=True).attach_via_adapter is True

"""Contract every registered language profile must honor."""

import pytest

from tdb.languages import registry
from tdb.dap.types import Capabilities


@pytest.fixture(params=registry.known_languages())
def profile(request):
    return registry.resolve(request.param)


def test_expected_languages_are_registered():
    # Guards the parametrized fixture above: if registration ever moves
    # out of import time, this fails loudly instead of the contract
    # suite silently collecting zero profiles.
    assert {"python", "cpp"} <= set(registry.known_languages())


def test_identity_fields(profile):
    assert profile.id
    assert profile.display_name
    assert profile.adapter.id


def test_launch_body_is_well_formed(profile):
    body = profile.adapter.launch_body(
        program="/x/prog",
        args=[],
        cwd="/x",
        env=None,
        stop_on_entry=False,
        console="internalConsole",
        opts={},
    )
    assert body["request"] == "launch"
    assert body["program"] == "/x/prog"


def test_filters_tolerate_empty_capabilities(profile):
    filters = profile.adapter.pick_exception_filters(Capabilities())
    assert isinstance(filters, list)


def test_capability_types(profile):
    caps = profile.capabilities
    assert caps.compute_step_units is None or callable(caps.compute_step_units)
    assert caps.child_process_strategy in (None, "debugpy")
    assert isinstance(caps.task_inspection, bool)


def test_profile_modules_never_import_ui(profile):
    import sys

    mod = sys.modules[type(profile.adapter).__module__]
    source = open(mod.__file__).read()
    for forbidden in ("tdb.app", "tdb.widgets", "session.controller"):
        assert forbidden not in source, f"{mod.__name__} imports {forbidden}"

import sys

import pytest

from tdb.dap.types import Capabilities
from tdb.languages import registry
from tdb.languages.base import LanguageNotSupportedError
from tdb.languages.perl import PerlAdapter, build_perl_profile


def test_profile_shape():
    p = build_perl_profile()
    assert p.id == "perl"
    assert p.adapter.id == "perl-tdb"
    assert p.presentation.lexer == "perl"
    assert p.capabilities.compute_step_units is None
    assert p.capabilities.task_inspection is False
    assert p.adapter.quirks.attach_via_adapter is True
    assert p.adapter.quirks.pre_arm_pause_on_attach is False


def test_registered_in_registry():
    assert "perl" in registry.known_languages()
    assert registry.resolve("perl").id == "perl"


def test_command_is_bundled_module():
    assert PerlAdapter().command() == [sys.executable, "-m", "tdb.adapters.perl"]


def test_launch_body_carries_perl_override():
    body = PerlAdapter(perl_executable="/opt/bin/perl").launch_body(
        program="/x/p.pl",
        args=["a"],
        cwd="/x",
        env={"K": "V"},
        stop_on_entry=True,
        console="internalConsole",
        opts={},
    )
    assert body == {
        "type": "perl",
        "request": "launch",
        "program": "/x/p.pl",
        "args": ["a"],
        "cwd": "/x",
        "stopOnEntry": True,
        "env": {"K": "V"},
        "perl": "/opt/bin/perl",
    }


def test_launch_body_omits_optional_keys():
    body = PerlAdapter().launch_body(
        program="/x/p.pl",
        args=[],
        cwd="/x",
        env=None,
        stop_on_entry=False,
        console="internalConsole",
        opts={},
    )
    assert "env" not in body and "perl" not in body


def test_attach_body():
    body = PerlAdapter().attach_body(host="devbox", port=5678, opts={})
    assert body == {"type": "perl", "request": "attach", "host": "devbox", "port": 5678}


def test_attach_body_with_path_mappings():
    body = PerlAdapter().attach_body(
        host="devbox",
        port=5678,
        opts={"path_mappings": [("/local/src", "/srv/app")]},
    )
    assert body == {
        "type": "perl",
        "request": "attach",
        "host": "devbox",
        "port": 5678,
        "pathMappings": [{"localRoot": "/local/src", "remoteRoot": "/srv/app"}],
    }


def test_attach_body_omits_path_mappings_when_empty():
    body = PerlAdapter().attach_body(
        host="devbox", port=5678, opts={"path_mappings": []}
    )
    assert "pathMappings" not in body


def test_adapter_paths_names_the_interpreter():
    p = build_perl_profile(adapter_paths={"perl": "/opt/bin/perl"})
    body = p.adapter.launch_body(
        program="/x/p.pl",
        args=[],
        cwd="/x",
        env=None,
        stop_on_entry=False,
        console="internalConsole",
        opts={},
    )
    assert body["perl"] == "/opt/bin/perl"


def test_unknown_adapter_rejected():
    with pytest.raises(LanguageNotSupportedError):
        build_perl_profile(adapter="perl5db-xyz")


def test_no_exception_filters():
    assert build_perl_profile().adapter.pick_exception_filters(Capabilities()) == []

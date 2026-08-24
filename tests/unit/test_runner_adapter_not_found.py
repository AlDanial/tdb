"""Headless mode's single choke point for AdapterNotFoundError: instead of
a raw traceback when e.g. lldb-dap / gdb isn't installed, run_headless
should print the install hint to stderr and exit(2) — mirroring how the
remote-attach OSError case is already handled a few lines up."""

import pytest

from tdb.languages.base import (
    AdapterNotFoundError,
    AdapterQuirks,
    AdapterSpec,
    LanguageProfile,
    Presentation,
    ProfileCapabilities,
)
from tdb.server.runner import run_headless, setup_headless_session


class _MissingAdapterSpec(AdapterSpec):
    id = "missing"

    def command(self):
        raise AdapterNotFoundError(
            "missing-dap not found — install Missing Debugger >= 1.0"
        )

    def launch_body(self, **kw):
        return {}

    def attach_body(self, **kw):
        return {}

    def pick_exception_filters(self, caps):
        return []


def _missing_adapter_profile() -> LanguageProfile:
    return LanguageProfile(
        id="missing",
        display_name="Missing",
        adapter=_MissingAdapterSpec(),
        presentation=Presentation(),
        capabilities=ProfileCapabilities(),
    )


class _MissingMediatedAdapterSpec(_MissingAdapterSpec):
    """A native-style remote adapter: tdb must spawn it before attach."""

    quirks = AdapterQuirks(attach_via_adapter=True)


def _missing_mediated_adapter_profile() -> LanguageProfile:
    return LanguageProfile(
        id="rust",
        display_name="Rust",
        adapter=_MissingMediatedAdapterSpec(),
        presentation=Presentation(),
        capabilities=ProfileCapabilities(),
    )


async def test_run_headless_prints_hint_and_exits_on_adapter_not_found(capsys):
    with pytest.raises(SystemExit) as exc_info:
        await run_headless(
            program="does_not_matter.bin",
            profile=_missing_adapter_profile(),
        )
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "missing-dap not found" in captured.err


async def test_headless_native_remote_attach_prints_adapter_hint(capsys):
    with pytest.raises(SystemExit) as exc_info:
        await setup_headless_session(
            program="local-rust-binary",
            attach_host="remote.example",
            attach_port=2345,
            profile=_missing_mediated_adapter_profile(),
        )

    assert exc_info.value.code == 2
    assert "missing-dap not found" in capsys.readouterr().err

"""AdapterNotFoundError raised during session startup (e.g. lldb-dap /
gdb not installed) must surface its install hint to the user, not just
the log — mirrors how the remote-attach OSError case sets
`_startup_error` + exits instead of leaving a bare "Failed to start"
sub_title with the real reason buried in the log."""

from tdb.app import TdbApp
from tdb.languages.base import (
    AdapterNotFoundError,
    AdapterSpec,
    LanguageProfile,
    Presentation,
    ProfileCapabilities,
)
from tdb.persist import TdbConfig


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


async def test_adapter_not_found_surfaces_hint_and_exits():
    app = TdbApp(
        program="",
        config=TdbConfig(),
        profile=_missing_adapter_profile(),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        # _start_session is a background @work task; give it a beat to
        # run and hit controller.start() -> DAPClient.start() ->
        # adapter.command() -> AdapterNotFoundError.
        for _ in range(20):
            if app._startup_error is not None:
                break
            await pilot.pause()

    assert app._startup_error is not None
    assert "missing-dap not found" in app._startup_error
    assert app.return_code == 2

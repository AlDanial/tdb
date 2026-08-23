"""The Python language profile (debugpy adapter) — tdb's reference profile."""

from __future__ import annotations

import sys
from typing import Any

from tdb.dap.types import Capabilities
from tdb.languages.base import (
    AdapterQuirks,
    AdapterSpec,
    LanguageNotSupportedError,
    LanguageProfile,
    Presentation,
    ProfileCapabilities,
)
from tdb.languages.errors import parse_python_error


class DebugpyAdapter(AdapterSpec):
    id = "debugpy"
    quirks = AdapterQuirks(pre_arm_pause_on_attach=True)

    def command(self) -> list[str]:
        # Always tdb's own interpreter (which has debugpy installed).
        # The user's --python selects the *debuggee* interpreter and is
        # threaded through launch_body's "python" key instead. Running
        # the adapter on a Python without debugpy would die immediately
        # with ModuleNotFoundError.
        return [sys.executable, "-Xfrozen_modules=off", "-m", "debugpy.adapter"]

    def launch_body(
        self,
        *,
        program: str,
        args: list[str],
        cwd: str,
        env: dict[str, str] | None,
        stop_on_entry: bool,
        console: str,
        opts: dict[str, Any],
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "type": "debugpy",
            "request": "launch",
            "program": program,
            "args": args,
            "cwd": cwd,
            "console": console,
            "redirectOutput": console == "internalConsole",
            "justMyCode": opts.get("just_my_code", True),
            "stopOnEntry": stop_on_entry,
            "subProcess": opts.get("sub_process", True),
            # Frozen stdlib modules break debugpy's tracing.
            "pythonArgs": ["-Xfrozen_modules=off"],
        }
        if env:
            arguments["env"] = env
        if opts.get("python"):
            # "python" sets the debuggee interpreter; the adapter defaults
            # "debugLauncherPython" from it.
            arguments["python"] = opts["python"]
        return arguments

    def attach_body(
        self, *, host: str, port: int, opts: dict[str, Any]
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "type": "debugpy",
            "request": "attach",
            "connect": {"host": host, "port": port},
            "justMyCode": opts.get("just_my_code", True),
            "subProcess": True,
        }
        if opts.get("sub_process_id") is not None:
            # subProcessId (not processId) routes to a child session
            # without triggering ptrace injection.
            arguments["subProcessId"] = opts["sub_process_id"]
        if opts.get("path_mappings"):
            arguments["pathMappings"] = [
                {"localRoot": local, "remoteRoot": remote}
                for local, remote in opts["path_mappings"]
            ]
        return arguments

    def pick_exception_filters(self, caps: Capabilities) -> list[str]:
        # "userUnhandled" avoids spurious stops on internal exceptions
        # (e.g. GeneratorExit in traceback.walk_stack).
        return ["userUnhandled"]


def build_python_profile(
    adapter: str | None = None,
    adapter_paths: dict[str, str] | None = None,
    program: str | None = None,
) -> LanguageProfile:
    """Registry builder. `adapter`/`adapter_paths`/`program` exist for
    signature parity with other languages; Python has exactly one
    adapter and it always runs on tdb's own interpreter."""
    if adapter not in (None, "debugpy"):
        raise LanguageNotSupportedError(
            f"unknown adapter {adapter!r} for python (known: debugpy)"
        )
    from tdb.source_analysis import compute_step_units

    return LanguageProfile(
        id="python",
        display_name="Python",
        adapter=DebugpyAdapter(),
        presentation=Presentation(lexer="python", parse_error=parse_python_error),
        capabilities=ProfileCapabilities(
            compute_step_units=compute_step_units,
            child_process_strategy="debugpy",
            task_inspection=True,
            pause_while_running=True,
        ),
    )


PYTHON_PROFILE = build_python_profile()

"""The Python language profile (debugpy adapter) — tdb's reference profile."""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
from collections.abc import Callable
from typing import Any

from tdb import _timeouts
from tdb.dap.messages import Event
from tdb.dap.types import Capabilities
from tdb.languages.base import (
    AdapterQuirks,
    AdapterSpec,
    LanguageNotSupportedError,
    LanguageProfile,
    Presentation,
    assignment_matcher,
    ProfileCapabilities,
)
from tdb.languages import _python_adapter
from tdb.languages.errors import parse_python_error
from tdb.languages.python_attach import (
    PidAttachCancelled,
    PidAttachError,
    TargetInfo,
    inject,
    probe_target,
)


class DebugpyAdapter(AdapterSpec):
    id = "debugpy"
    quirks = AdapterQuirks(pre_arm_pause_on_attach=True, stdin_route="run_in_terminal")

    def command(self) -> list[str]:
        # Always tdb's own interpreter (which has debugpy installed).
        # The user's --python selects the *debuggee* interpreter and is
        # threaded through launch_body's "python" key instead. Running
        # the adapter on a Python without debugpy would die immediately
        # with ModuleNotFoundError.
        # -P: `-m` otherwise puts the cwd first on sys.path, and a debugpy/
        # or (with frozen modules off) runpy.py there would run as us. Every
        # `-m` spawn in tdb needs it; tests/integration/test_cwd_isolation.py.
        return [sys.executable, "-Xfrozen_modules=off", "-P", "-m", "debugpy.adapter"]

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
        if arguments["justMyCode"]:
            # A program installed under site-packages (e.g. tdb's packaged
            # examples) is library code to debugpy, so justMyCode would
            # skip its entry stop and every breakpoint. Mark the program's
            # own directory as user code; real libraries stay filtered.
            arguments["rules"] = [
                {"path": os.path.dirname(os.path.abspath(program)), "include": True}
            ]
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


class PidAttachDebugpyAdapter(DebugpyAdapter):
    """Attach to a running process by pid. tdb spawns the adapter, which
    picks a port and reports it; code injected into the target dials back,
    so nothing stays open in the target after tdb quits."""

    # No pre-armed pause: see DebugController._finish_attach_hook.
    quirks = AdapterQuirks(attach_via_adapter=True)

    def __init__(self, target: TargetInfo) -> None:
        self.target = target
        self.listen_port: int | None = None
        self._listening = asyncio.Event()
        self.token = secrets.token_hex(32)

    def command(self) -> list[str]:
        # Named as an argument because already_debugged() finds adapters by
        # looking for it in their command lines.
        return [
            sys.executable,
            "-Xfrozen_modules=off",
            "-P",
            _python_adapter.__file__,
            "debugpy.adapter",
        ]

    def environment(self) -> dict[str, str]:
        return {_python_adapter.TOKEN_VARIABLE: self.token}

    def attach_body(
        self, *, host: str, port: int, opts: dict[str, Any]
    ) -> dict[str, Any]:
        arguments = super().attach_body(host=host, port=port, opts=opts)
        del arguments["connect"]
        # The adapter chooses: a port tdb picked could be taken before the
        # adapter bound it, and the target would dial whoever took it.
        arguments["listen"] = {"host": "127.0.0.1", "port": 0}
        arguments["redirectOutput"] = True
        arguments["subProcess"] = False
        return arguments

    async def after_attach_request(
        self, initialized: asyncio.Event, quitting: asyncio.Event
    ) -> str | None:
        await self._wait_until_listening(quitting)
        assert self.listen_port is not None
        return await inject(
            self.target, self.listen_port, initialized, quitting, token=self.token
        )

    def event_handlers(self) -> dict[str, Callable[[Event], None]]:
        return {"debugpyWaitingForServer": self._on_waiting_for_server}

    def _on_waiting_for_server(self, event: Event) -> None:
        self.listen_port = event.body["port"]
        self._listening.set()

    async def _wait_until_listening(self, quitting: asyncio.Event) -> None:
        waits = {
            asyncio.ensure_future(self._listening.wait()),
            asyncio.ensure_future(quitting.wait()),
        }
        try:
            await asyncio.wait(
                waits,
                timeout=_timeouts.PID_ATTACH_LISTEN,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for wait in waits:
                wait.cancel()
        if quitting.is_set():
            raise PidAttachCancelled(f"attach to pid {self.target.pid} cancelled")
        if not self._listening.is_set():
            raise PidAttachError(
                f"cannot attach to pid {self.target.pid}: debugpy's adapter did "
                f"not start listening within {_timeouts.PID_ATTACH_LISTEN:.0f} "
                "seconds"
            )


def _is_breakpoint_hook_frame(frame) -> bool:
    """`tdb.breakpoint()` pauses inside tdb/breakpoint_hook.py."""
    src = frame.source.path if frame.source else None
    return bool(src) and os.path.basename(src) == "breakpoint_hook.py"


def _is_pytest_trace_trampoline_frame(frame) -> bool:
    """pytest `--trace` under tdb.pytest_debugger pauses in a trampoline
    on the line that calls the test function."""
    src = frame.source.path if frame.source else None
    return (
        bool(src)
        and os.path.basename(src) == "pytest_debugger.py"
        and frame.name.endswith("_trace_trampoline")
    )


def build_python_profile(
    adapter: str | None = None,
    adapter_paths: dict[str, str] | None = None,
    program: str | None = None,
    attach_pid: int | None = None,
    python: str | None = None,
) -> LanguageProfile:
    """Registry builder. `adapter`/`adapter_paths`/`program` exist for
    signature parity with other languages; Python has exactly one
    adapter and it always runs on tdb's own interpreter. `attach_pid`
    selects pid attach, and `python` names the target's interpreter."""
    if adapter not in (None, "debugpy"):
        raise LanguageNotSupportedError(
            f"unknown adapter {adapter!r} for python (known: debugpy)"
        )
    from tdb.source_analysis import compute_step_units

    spec: AdapterSpec = DebugpyAdapter()
    if attach_pid is not None:
        spec = PidAttachDebugpyAdapter(probe_target(attach_pid, python))
    return LanguageProfile(
        id="python",
        display_name="Python",
        adapter=spec,
        presentation=Presentation(lexer="python", parse_error=parse_python_error),
        capabilities=ProfileCapabilities(
            compute_step_units=compute_step_units,
            breakpoint_hook_frame=_is_breakpoint_hook_frame,
            breakpoint_hook_step_in_frame=_is_pytest_trace_trampoline_frame,
            # Children aren't debugged after an attach by pid, and a forged
            # debugpyAttach would have tdb connect wherever the target said.
            child_process_strategy="debugpy" if attach_pid is None else None,
            task_inspection=True,
            pause_while_running=True,
            interactive_variable=assignment_matcher(
                r"^\s*(?P<name>[A-Za-z_]\w*)\s*(?::[^=]+)?=(?!=)"
            ),
        ),
    )


PYTHON_PROFILE = build_python_profile()

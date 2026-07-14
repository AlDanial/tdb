"""Core datatypes for multi-language support.

A LanguageProfile bundles three sub-objects, each with exactly ONE consumer:

  - ``adapter``:      AdapterSpec        -> dap/client.py + session/controller.py
  - ``presentation``: Presentation       -> widgets/code_view.py
  - ``capabilities``: ProfileCapabilities -> per-feature gates (statement
                      stepping, task inspection, child processes)

Rules that keep this compartmentalized (see the design spec):
  - one-way dependency: modules read the profile; a profile never imports
    the controller, app, or widgets, and never holds runtime state.
  - capability values are data/callables, not subclass overrides, so
    consumers feature-gate with ``is not None`` / truthiness checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from tdb.dap.types import Capabilities


class AdapterNotFoundError(Exception):
    """A debug-adapter executable could not be located.

    ``hint`` is one user-facing line: what to install or which config
    key to set. Raised by AdapterSpec.command(); surfaced by the CLI.
    """

    def __init__(self, hint: str) -> None:
        super().__init__(hint)
        self.hint = hint


class LanguageNotSupportedError(Exception):
    """Language detection or --lang/--adapter resolution failed."""


@dataclass(frozen=True)
class AdapterQuirks:
    """Per-adapter workarounds, read only by session/controller.py."""

    # debugpy ignores `stopOnEntry` for attach requests; the controller
    # pre-arms a `pause` before configurationDone instead. True only
    # for debugpy. (The deferred launch/attach response needs no flag:
    # holding the response until configurationDone is DAP-spec behavior
    # and the controller's fire-and-forget launch future handles it for
    # every adapter.)
    pre_arm_pause_on_attach: bool = False


class AdapterSpec:
    """How to spawn and speak to one debug adapter. Subclass per adapter.

    Instances are stateless: pure data + pure functions.
    """

    id: str = ""
    quirks: AdapterQuirks = AdapterQuirks()

    def command(self) -> list[str]:
        """Argv for the adapter subprocess (DAP over stdio).

        Raises AdapterNotFoundError with an install hint when the
        executable cannot be found.
        """
        raise NotImplementedError

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
        """Arguments for the DAP `launch` request.

        ``opts`` carries adapter-specific extras the generic client
        signature doesn't know about (debugpy: just_my_code, python,
        sub_process).
        """
        raise NotImplementedError

    def attach_body(
        self, *, host: str, port: int, opts: dict[str, Any]
    ) -> dict[str, Any]:
        """Arguments for the DAP `attach` request."""
        raise NotImplementedError

    def pick_exception_filters(self, caps: Capabilities) -> list[str]:
        """Choose exception-breakpoint filters from what the adapter
        advertised in its initialize response. Default: the adapter's
        own defaults."""
        return [
            f["filter"] for f in caps.exception_breakpoint_filters if f.get("default")
        ]


@dataclass(frozen=True)
class Presentation:
    """Language-specific display knobs, consumed by widgets."""

    # Rich/pygments lexer name for the Code View.
    lexer: str = "text"


@dataclass(frozen=True)
class ProfileCapabilities:
    """Optional features. None/False means "hidden for this language"."""

    # Map a source path to statement step-units [(start_line, end_line)].
    # None -> no statement-granularity stepping; line mode only.
    compute_step_units: Callable[[str], list[tuple[int, int]]] | None = None

    # "debugpy" -> controller registers ChildProcessManager's
    # debugpyAttach listener. None -> no child-process debugging.
    # (A standard `startDebugging`-based strategy is future work.)
    child_process_strategy: str | None = None

    # True -> the asyncio-task / multiprocessing inspection snippets
    # (tdb.inspection) may be evaluated in the debuggee.
    task_inspection: bool = False


@dataclass(frozen=True)
class LanguageProfile:
    """One debuggable language: its default adapter + presentation + gates."""

    id: str
    display_name: str
    adapter: AdapterSpec
    presentation: Presentation = field(default_factory=Presentation)
    capabilities: ProfileCapabilities = field(default_factory=ProfileCapabilities)

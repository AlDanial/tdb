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

    # True -> tdb spawns the adapter subprocess for attach too and sends
    # the DAP attach request through it (the adapter dials the debuggee).
    # False (debugpy) -> tdb connects straight to the remote DAP server.
    attach_via_adapter: bool = False


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
class ErrorFrame:
    """One stack frame parsed out of a language's fatal-error output."""

    path: str
    line: int
    func: str  # "" when the language doesn't name one


@dataclass(frozen=True)
class ParsedError:
    """A language's fatal-error text, parsed into modal-ready pieces."""

    header: str  # modal's first line, e.g. "Traceback (most recent call last):"
    message: str  # e.g. "ZeroDivisionError: division by zero"
    frames: list[ErrorFrame]  # OUTERMOST-first (source order), same as Python prints
    # Raw display text for the modal body (below the header line): the
    # language's own error text, verbatim where possible, so source
    # snippets / chained-exception separator sentences / etc. aren't
    # lost. NOT reconstructed from `frames` -- built directly off the
    # original stderr text by the parser. `frames` stays the
    # structured data `_check_stderr_traceback` uses to build synthetic
    # DAP StackFrames.
    detail: str


@dataclass(frozen=True)
class Presentation:
    """Language-specific display knobs, consumed by widgets."""

    # Rich/pygments lexer name for the Code View.
    lexer: str = "text"

    # Parse a language's raw stderr into a ParsedError, or None if no
    # fatal error is present. `exit_code` is the debuggee's real DAP
    # `exited` code when it's available by the time parsing runs (None
    # if it hasn't arrived yet, or the language has no owned child to
    # report one for, e.g. attach mode) -- consulted only by parsers
    # that need it to disambiguate fatal-vs-non-fatal output (perl);
    # parsers with an unambiguous signal of their own (python's
    # traceback header) accept and ignore it. None -> language has no
    # parser (yet).
    parse_error: Callable[[str, int | None], ParsedError | None] | None = None

    # Synthetic-stack-frame display name for an ErrorFrame whose `func`
    # is "" (the language doesn't name a function for that frame, e.g.
    # top-level/compile-time code). Python's fatal-error frames use this
    # historical convention for module-level code; other languages have
    # their own (perl's live stackTrace already falls back to "main" --
    # see adapters/perl/server.py -- so its error-modal synthetic frames
    # match that instead of showing Python's "<module>").
    frame_placeholder: str = "<module>"


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

    # True -> the adapter honors a DAP `pause` request while the
    # debuggee is running (required for `tdb --run`). debugpy, the
    # perl adapter, and the bash adapter all do; tcsh gains it with
    # its pause handler; gdb/lldb-dap pending verification.
    pause_while_running: bool = False

    # Predicate over a stack frame's name marking frames that have no
    # inspectable locals (e.g. rdbg reports native frames like
    # "[C] Kernel#sleep" when a pause lands inside a C call, with a
    # locals scope holding only %self). After a stop, the initial frame
    # selection skips these so the Variables view opens on a frame that
    # actually has locals; the Stack view still shows the full stack.
    # None -> every frame is selectable (current behavior).
    opaque_frame: Callable[[str], bool] | None = None


@dataclass(frozen=True)
class LanguageProfile:
    """One debuggable language: its default adapter + presentation + gates."""

    id: str
    display_name: str
    adapter: AdapterSpec
    presentation: Presentation = field(default_factory=Presentation)
    capabilities: ProfileCapabilities = field(default_factory=ProfileCapabilities)

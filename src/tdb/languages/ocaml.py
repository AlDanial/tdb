"""The OCaml language profile (built up across Tasks 2-7).

This task: executable-flavor sniffing used by registry.detect(), plus
frame-name demangling for Presentation.frame_name.
"""

from __future__ import annotations

import re
import shutil
import sys
from importlib import resources
from pathlib import Path
from typing import Any

from tdb.dap.types import StackFrame, Thread
from tdb.languages.base import (
    AdapterNotFoundError,
    AdapterQuirks,
    AdapterSpec,
    LanguageNotSupportedError,
    LanguageProfile,
    Presentation,
    ProfileCapabilities,
    ThreadDecoration,
)
from tdb.languages.cpp import GdbDapAdapter, LldbDapAdapter
from tdb.languages.errors import parse_ocaml_error

_BYTECODE_TRAILER_MARK = b"Caml1999"  # e.g. b"Caml1999X033" at file end
_NATIVE_MAGIC = (
    b"\x7fELF",
    b"\xcf\xfa\xed\xfe",
    b"\xce\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
)
_CAML_MARKERS = (b"caml_program", b"caml_startup")
_SCAN_CHUNK = 2 * 1024 * 1024  # spec risk 5: bounded scan, head + tail


def ocaml_flavor(program: str) -> str | None:
    """ "native"/"bytecode" when `program` is an OCaml executable, else None.

    Best-effort byte sniffing: a stripped native binary may return None
    (lands in cpp; --lang ocaml overrides — documented in README).
    """
    path = Path(program)
    try:
        with open(path, "rb") as f:
            head = f.read(64)
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 16))
            tail = f.read()
    except OSError:
        return None
    if head.startswith(b"#!") and b"ocamlrun" in head.splitlines()[0]:
        return "bytecode"
    if _BYTECODE_TRAILER_MARK in tail:
        return "bytecode"
    if any(head.startswith(m) for m in _NATIVE_MAGIC):
        if _scan_for_caml_marker(path, size):
            return "native"
    return None


_MANGLED_SUFFIX_RE = re.compile(r"_\d+$")
# "caml" followed by an uppercase letter marks a mangled OCaml symbol
# (camlMain__worker_271, camlOcaml_domains.worker_297). Runtime C symbols
# (caml_apply2, caml_start_program) have a lowercase/underscore letter
# right after "caml" and must be left untouched.
_CAML_MANGLED_PREFIX_RE = re.compile(r"^caml[A-Z]")


def demangle_frame_name(name: str) -> str:
    """ "camlFoo__Bar__run_17" -> "Foo.Bar.run"; anything else unchanged.

    lldb's OCaml 5.x symbol display separates module-path segments with
    "__" but a module and its function with "." (e.g.
    "camlOcaml_domains.worker_297" -> "Ocaml_domains.worker",
    "camlStdlib__Domain.body_757" -> "Stdlib.Domain.body"), so both
    separators are normalized to ".". Runtime C symbols (caml_apply2,
    caml_start_program) don't match the mangled-prefix shape and pass
    through unchanged.
    """
    if not _CAML_MANGLED_PREFIX_RE.match(name) or (
        "__" not in name and "." not in name
    ):
        return name
    body = _MANGLED_SUFFIX_RE.sub("", name[len("caml") :])
    return body.replace("__", ".")


def _scan_for_caml_marker(path: Path, size: int) -> bool:
    try:
        with open(path, "rb") as f:
            if any(m in f.read(_SCAN_CHUNK) for m in _CAML_MARKERS):
                return True
            if size > _SCAN_CHUNK:
                # overlap by 16 bytes so a marker straddling the boundary
                # of head and tail chunks is still seen for mid-size files
                f.seek(max(_SCAN_CHUNK - 16, size - _SCAN_CHUNK))
                data = f.read()
                return any(m in data for m in _CAML_MARKERS)
    except OSError:
        pass
    return False


# Marker frames observed under lldb (probe Q3). Substring match on frame
# names; tolerant of symbol prefixes/suffixes across OCaml versions.
_BACKUP_FRAME_MARKERS = ("backup_thread_func", "caml_thread_condwait")
_DOMAIN_FRAME_MARKERS = (
    "domain_thread_func",
    "caml_start_program",
    "caml_domain_spawn",
)


def _stack_matches(frames: list[StackFrame], markers: tuple[str, ...]) -> bool:
    return any(m in f.name for f in frames for m in markers)


def classify_ocaml_threads(
    threads: list[Thread], stacks: dict[int, list[StackFrame]]
) -> list[ThreadDecoration]:
    """Label domain threads "Domain N" (creation order; the first thread
    is always Domain 0/main) and hide runtime backup threads. A thread
    with no stack info stays visible under the adapter's name."""
    decorations: list[ThreadDecoration] = []
    domain_no = 0
    for i, t in enumerate(threads):
        frames = stacks.get(t.id, [])
        if i == 0:
            decorations.append(ThreadDecoration(t, "Domain 0 (main)", False))
            domain_no = 1
            continue
        if (
            frames
            and _stack_matches(frames, _BACKUP_FRAME_MARKERS)
            and not _stack_matches(frames, _DOMAIN_FRAME_MARKERS)
        ):
            decorations.append(ThreadDecoration(t, None, True))
            continue
        if frames and _stack_matches(frames, _DOMAIN_FRAME_MARKERS):
            decorations.append(ThreadDecoration(t, f"Domain {domain_no}", False))
            domain_no += 1
            continue
        decorations.append(ThreadDecoration(t, None, False))
    return decorations


# --- Adapters + profile builder (Task 6) -----------------------------------
#
# Two native adapters (lldb-dap default, gdb fallback) reuse cpp.py's
# AdapterSpec subclasses with OCaml-specific launch_body twists; one
# bytecode adapter (ocamlearlybird) is spawned directly over stdio — see
# the spec's Probe-verified facts Q1: tdb's production DAP client already
# sends compact JSON (dap/protocol.py), which sidesteps the framing-parser
# misparse that only affects non-compact JSON bodies, so no proxy shim is
# needed.


def formatter_script_path() -> str:
    """Absolute path of the lldb formatter script, for initCommands."""
    return str(resources.files("tdb.adapters.ocaml") / "lldb_formatters.py")


def _with_runparam(env: dict[str, str] | None) -> dict[str, str]:
    """Merge OCAMLRUNPARAM=b (backtraces) into the debuggee env without
    clobbering user flags."""
    merged = dict(env or {})
    current = merged.get("OCAMLRUNPARAM", "")
    flags = [f for f in current.split(",") if f]
    if not any(f == "b" or f.startswith("b=") for f in flags):
        flags.append("b")
    merged["OCAMLRUNPARAM"] = ",".join(flags)
    return merged


class OCamlLldbAdapter(LldbDapAdapter):
    """lldb-dap with OCaml twists: formatter injection, backtrace env,
    and a stop-before-abort breakpoint on the uncaught-exception hook."""

    # The C/C++ base adapters support native remote attach; OCaml does
    # not offer it yet (attach_body below raises), so opt back out of
    # the attach-via-adapter quirk the base class declares.
    quirks = AdapterQuirks()

    def launch_body(
        self, *, program, args, cwd, env, stop_on_entry, console, opts: dict[str, Any]
    ) -> dict[str, Any]:
        body = super().launch_body(
            program=program,
            args=args,
            cwd=cwd,
            env=_with_runparam(env),
            stop_on_entry=stop_on_entry,
            console=console,
            opts=opts,
        )
        body["initCommands"] = [
            f"command script import {formatter_script_path()}",
        ]
        body["preRunCommands"] = [
            "breakpoint set --name caml_fatal_uncaught_exception",
        ]
        return body


class OCamlGdbAdapter(GdbDapAdapter):
    """gdb -i dap fallback (Linux). No formatter injection (lldb-only
    script) and no pre-run breakpoint (gdb DAP has no initCommands);
    the parse-on-exit error modal still works via OCAMLRUNPARAM=b."""

    quirks = AdapterQuirks()  # no native remote attach for OCaml yet

    def launch_body(
        self, *, program, args, cwd, env, stop_on_entry, console, opts: dict[str, Any]
    ) -> dict[str, Any]:
        return super().launch_body(
            program=program,
            args=args,
            cwd=cwd,
            env=_with_runparam(env),
            stop_on_entry=stop_on_entry,
            console=console,
            opts=opts,
        )


class EarlybirdAdapter(AdapterSpec):
    """ocamlearlybird: bytecode-only, stdio DAP, rich OCaml locals.
    Field names below are the probe-verified ones (spec Q1)."""

    id = "ocamlearlybird"
    quirks = AdapterQuirks()

    def __init__(self, executable: str | None = None) -> None:
        self._executable = executable

    def command(self) -> list[str]:
        exe = self._executable or shutil.which("ocamlearlybird")
        if exe is None:
            raise AdapterNotFoundError(
                "ocamlearlybird not found on PATH — `opam install earlybird`, "
                'or set {"adapters": {"ocamlearlybird": "/path/to/it"}} '
                "in tdb's config.json"
            )
        return [exe, "debug"]

    def launch_body(
        self, *, program, args, cwd, env, stop_on_entry, console, opts: dict[str, Any]
    ) -> dict[str, Any]:
        if console == "externalTerminal":
            raise LanguageNotSupportedError(
                "--terminal is not supported with the ocamlearlybird "
                "adapter (earlybird has no terminal integration)"
            )
        body: dict[str, Any] = {
            "type": "ocaml",
            "request": "launch",
            "program": program,
            "arguments": args,
            "cwd": cwd,
            "stopOnEntry": stop_on_entry,
            "console": "internalConsole",
        }
        # Unconditional merge (unlike a plain `if env:` gate): DebugController
        # never passes an `env` kwarg through client.launch(), so `env` is
        # always None on the production path. OCamlLldbAdapter/OCamlGdbAdapter
        # avoid this by pre-merging before their superclass's own `if env:`
        # check; this adapter builds its body directly, so it must merge
        # unconditionally itself or OCAMLRUNPARAM=b (needed for the
        # parse-on-exit error modal, per parse_ocaml_error) is silently
        # never injected.
        body["env"] = _with_runparam(env)
        return body

    def attach_body(self, *, host, port, opts) -> dict[str, Any]:
        raise LanguageNotSupportedError("remote attach is not supported for ocaml yet")


def build_ocaml_profile(
    adapter: str | None = None,
    adapter_paths: dict[str, str] | None = None,
    program: str | None = None,
) -> LanguageProfile:
    if sys.platform == "win32":
        raise LanguageNotSupportedError(
            "OCaml debugging is not supported on Windows yet"
        )
    adapters: dict[str, type[AdapterSpec]] = {
        "lldb-dap": OCamlLldbAdapter,
        "gdb": OCamlGdbAdapter,
        "ocamlearlybird": EarlybirdAdapter,
    }
    if adapter is None:
        flavor = ocaml_flavor(program) if program else None
        adapter = "ocamlearlybird" if flavor == "bytecode" else "lldb-dap"
    if adapter not in adapters:
        raise LanguageNotSupportedError(
            f"unknown adapter {adapter!r} for ocaml "
            f"(known: {', '.join(sorted(adapters))})"
        )
    executable = (adapter_paths or {}).get(adapter)
    native = adapter in ("lldb-dap", "gdb")
    return LanguageProfile(
        id="ocaml",
        display_name="OCaml",
        adapter=adapters[adapter](executable=executable),
        presentation=Presentation(
            lexer="ocaml",
            parse_error=parse_ocaml_error,
            frame_placeholder="<top>",
            frame_name=demangle_frame_name if native else None,
        ),
        capabilities=ProfileCapabilities(
            # lldb-dap/gdb pause verified for cpp (test_cpp_pause.py);
            # earlybird per probe Q4 (default False until verified True).
            pause_while_running=native,
            classify_threads=classify_ocaml_threads if native else None,
        ),
    )

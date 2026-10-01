"""The C/C++ language profile.

Default adapter: `gdb -i dap` (GDB >= 14) via GdbDapAdapter.
Alternate: lldb-dap (ships with LLVM >= 17; debugs GCC- and
clang-built binaries alike — DWARF is compiler-neutral), selected
via `--adapter lldb-dap`.

Core-DAP capabilities only: no statement stepping (no C++ source
model), no task inspection, no child-process tracking.
"""

from __future__ import annotations

import dataclasses
import logging
import shutil
from importlib import resources
from typing import Any

from tdb.languages import native_tools
from tdb.languages.base import (
    AdapterNotFoundError,
    AdapterQuirks,
    AdapterSpec,
    LanguageNotSupportedError,
    LanguageProfile,
    Presentation,
    assignment_matcher,
    ProfileCapabilities,
)

log = logging.getLogger(__name__)


def _required_program(opts: dict[str, Any]) -> str:
    """Native remote attach drives gdbserver/lldb-server through a local
    adapter, which needs the local symbol-bearing copy of the remote
    executable."""
    program = opts.get("program")
    if not isinstance(program, str) or not program:
        raise LanguageNotSupportedError(
            "native remote attach requires a local program with debug symbols"
        )
    return program


def quote_debugger_arg(value: str) -> str:
    """Quote one argument for a gdb or lldb CLI command. Both parse
    double-quoted arguments with backslash escapes (gdb via buildargv,
    e.g. `set substitute-path`; lldb e.g. `command script import`).
    Shared by the rust and ocaml profiles — keep semantics in sync with
    both debuggers when changing."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def gdb_source_filename(value: str) -> str:
    """Validate one filename for GDB's ``source`` command parser.

    ``source`` takes the rest of the line as a literal filename (tilde
    expansion only — no backslash unescaping, no quote stripping), so the
    path must be passed raw: escaping would corrupt paths containing
    spaces or Windows backslashes. Shared with the rust profile's probe.
    """
    if "\n" in value or "\r" in value:
        raise LanguageNotSupportedError("GDB script path contains a newline")
    return value


# gdb-side helper that guarantees libstdc++ pretty-printing (see its
# docstring): a gdb built into its own prefix never auto-loads the
# printers gcc installs, so std::vector shows as _M_impl pointer soup.
STL_PRINTERS_SCRIPT = "gdb_stl_printers.py"


def gdb_init_args() -> list[str]:
    """`-iex` arguments every tdb gdb session starts with: the STL
    pretty-printer helper, sourced before gdb enters DAP mode (it is
    silent, so it cannot disturb the protocol stream)."""
    script = resources.files("tdb.adapters.native").joinpath(STL_PRINTERS_SCRIPT)
    return ["-iex", f"source {gdb_source_filename(str(script))}"]


# The C symbol every native live breakpoint hook (tdb.h, Rust `tdb`,
# OCaml `Tdb`) calls once tdb is attached; tdb's hidden function
# breakpoint lands there. Keep in sync with the hook libraries.
HOOK_STOP_FUNCTION = "tdb_breakpoint_stop"

# Frames belonging to the C/C++ hook in tdb.h. The stop lands in
# tdb_breakpoint_stop; stepping out of each of these in turn reaches the
# user's caller (see app_handlers.dap_events._stopped_inside_breakpoint_hook).
NATIVE_HOOK_FRAMES = frozenset(
    {HOOK_STOP_FUNCTION, "tdb_breakpoint", "tdb_breakpoint_lang"}
)


def hook_frame_base(name: str) -> str:
    """Normalize a frame name the way adapters decorate native symbols:
    lldb-dap reports g++ frames as `::tdb_breakpoint_stop()` and
    `::tdb_breakpoint_lang(const char *)`; strip the leading `::` and the
    parameter list (everything from the first `(`)."""
    name = name.removeprefix("::")
    paren = name.find("(")
    return name if paren < 0 else name[:paren]


def is_native_hook_name(base: str) -> bool:
    """True when a normalized frame name is one of tdb.h's C symbols, bare
    or namespace-qualified (gdb names the Rust crate's `#[no_mangle]` stop
    function `tdb::tdb_breakpoint_stop`). The C symbol names are
    distinctive enough that the last-segment rule cannot match user code."""
    return base in NATIVE_HOOK_FRAMES or base.rsplit("::", 1)[-1] in NATIVE_HOOK_FRAMES


def is_breakpoint_hook_frame(frame) -> bool:
    """True when `frame` (a StackFrame) is inside the tdb.h hook."""
    return is_native_hook_name(hook_frame_base(frame.name or ""))


class LldbDapAdapter(AdapterSpec):
    id = "lldb-dap"
    quirks = AdapterQuirks(attach_via_adapter=True, attach_requires_local_program=True)

    def __init__(
        self, executable: str | None = None, attach_pid: int | None = None
    ) -> None:
        self._executable = executable
        self._attach_pid = attach_pid

    def command(self) -> list[str]:
        exe = self._executable or shutil.which("lldb-dap")
        if exe is None:
            raise AdapterNotFoundError(
                "lldb-dap not found on PATH — install LLVM >= 17 "
                '(package `lldb`), or set {"adapters": {"lldb-dap": '
                '"/path/to/lldb-dap"}} in tdb\'s config.json'
            )
        return [exe]

    def hook_function_breakpoints(self) -> tuple[str, ...]:
        return (HOOK_STOP_FUNCTION,) if self._attach_pid is not None else ()

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
        body: dict[str, Any] = {
            "type": "lldb-dap",
            "request": "launch",
            "program": program,
            "args": args,
            "cwd": cwd,
            "stopOnEntry": stop_on_entry,
        }
        if env:
            # lldb-dap wants ["KEY=VALUE", ...], not a mapping.
            body["env"] = [f"{k}={v}" for k, v in env.items()]
        if console == "externalTerminal":
            body["runInTerminal"] = True
        return body

    def attach_body(
        self, *, host: str, port: int, opts: dict[str, Any]
    ) -> dict[str, Any]:
        if self._attach_pid is not None:
            # Local pid attach. lldb-dap honors stopOnEntry for attach:
            # True stops the process for the user; the hooks pass
            # --no-pause-on-attach because the program stops itself.
            return {
                "program": _required_program(opts),
                "pid": self._attach_pid,
                "stopOnEntry": opts.get("pause_on_attach", True),
            }
        body: dict[str, Any] = {
            "program": _required_program(opts),
            "gdb-remote-host": host,
            "gdb-remote-port": port,
        }
        mappings = opts.get("path_mappings") or []
        if mappings:
            body["sourceMap"] = [[remote, local] for local, remote in mappings]
        return body


# One-line gdb `python` command printing every source breakpoint as the
# JSON listing tdb.session.breakpoint_sync parses. Must stay one line:
# it travels through DAP `evaluate` (context "repl"), which gdb hands
# to gdb.execute() a single command at a time. Filters out
# watchpoints/catchpoints (type), `tbreak` temporaries, pending
# breakpoints, and any without a source location. `dap` marks those
# gdb's own DAP layer created (its breakpoint_map), so the controller
# can tell CLI-created breakpoints apart and take them over.
#
# Side effect, on purpose: the command first prunes that map of
# breakpoints the user deleted at the CLI. gdb (17.1) leaves them in,
# and the next setBreakpoints for that file then dies with "Breakpoint
# N is invalid" when gdb tries to delete the dead entry — which would
# break the re-push this listing feeds, and any later breakpoint edit
# in tdb. Pruning here is the one place tdb can fix that up front.
GDB_BREAKPOINT_QUERY = (
    "python import json, gdb.dap.breakpoint as _tdb_dapbp; "
    "[m.pop(k) for m in _tdb_dapbp.breakpoint_map.values() "
    "for k, b in list(m.items()) if not b.is_valid()]; "
    "_tdb_owned = {b.number for m in _tdb_dapbp.breakpoint_map.values() "
    "for b in m.values()}; "
    "print(json.dumps(["
    '{"n": b.number, '
    '"path": b.locations[0].fullname or b.locations[0].source[0], '
    '"line": b.locations[0].source[1], '
    '"enabled": b.enabled, "cond": b.condition, '
    '"dap": b.number in _tdb_owned} '
    "for b in gdb.breakpoints() "
    "if b.type == gdb.BP_BREAKPOINT and not b.temporary and not b.pending "
    "and b.locations and b.locations[0].source]))"
)


# gdb's exact wording when `-i NAME` names no registered interpreter;
# defined with the `tdb --info` probe that looks for it.
GDB_NO_DAP_INTERPRETER = native_tools.GDB_NO_DAP_INTERPRETER


def _dotted(version: tuple[int, ...]) -> str:
    return ".".join(str(n) for n in version)


def gdb_too_old_hint(
    exe: str,
    version: tuple[int, ...],
    explicit: str | None,
    toolset: tuple[str, tuple[int, ...]] | None,
) -> str:
    """The AdapterNotFoundError hint for a gdb below GDB_DAP_MIN_VERSION."""
    source = (
        " (from --adapter or config.json's adapters.gdb)" if explicit else " on PATH"
    )
    hint = (
        f"gdb{source} at {exe} is version {_dotted(version)}, but tdb needs "
        f"GDB >= {_dotted(native_tools.GDB_DAP_MIN_VERSION)} for its DAP mode"
        " (`gdb -i dap`)"
    )
    if toolset is not None:
        hint += (
            f"; a usable gdb {_dotted(toolset[1])} is at {toolset[0]} — pass it "
            f"with `--adapter {toolset[0]}` or set "
            f'{{"adapters": {{"gdb": "{toolset[0]}"}}}} in tdb\'s config.json'
        )
    else:
        hint += (
            "; on RHEL 8 `dnf install gcc-toolset-14-gdb` provides "
            "/opt/rh/gcc-toolset-14/root/usr/bin/gdb, which tdb then finds "
            "on its own; elsewhere install a newer gdb and name it with "
            '`--adapter /path/to/gdb` or {"adapters": {"gdb": "/path/to/gdb"}} '
            "in tdb's config.json"
        )
    return hint


class GdbDapAdapter(AdapterSpec):
    """GDB's built-in DAP interpreter (`gdb -i dap`, GDB >= 14).

    Default C++ adapter: GDB's libstdc++ pretty-printers are more
    complete than LLDB's, which matters for heavily GCC codebases.
    """

    id = "gdb"
    quirks = AdapterQuirks(
        attach_via_adapter=True,
        attach_requires_local_program=True,
        resume_after_remote_attach=True,
    )

    def __init__(
        self, executable: str | None = None, attach_pid: int | None = None
    ) -> None:
        self._executable = executable
        self._attach_pid = attach_pid
        if attach_pid is not None:
            # gdb `attach PID` stops the inferior; unlike `target remote`
            # that stop is meaningful, so the controller (not this quirk)
            # decides whether to resume. dataclasses.replace() derives
            # from the subclass's own class-level quirks (e.g. OCaml's
            # bootstrap_stop_for_entry_breakpoints) instead of discarding
            # them with a fresh AdapterQuirks().
            self.quirks = dataclasses.replace(
                type(self).quirks,
                attach_via_adapter=True,
                attach_requires_local_program=True,
                attach_stop_is_pausable=True,
                resume_after_remote_attach=False,
            )

    def hook_function_breakpoints(self) -> tuple[str, ...]:
        return (HOOK_STOP_FUNCTION,) if self._attach_pid is not None else ()

    def resolve_executable(self) -> str:
        """The gdb this session runs, checked up front for DAP support.

        `gdb -i dap` needs GDB >= 14 (and a build with Python, which
        implements the DAP layer); an older gdb prints "Interpreter
        `dap' unrecognized" and exits, which used to surface only as a
        "Failed to start" subtitle. RHEL 8 ships gdb 8.2 as /usr/bin/gdb
        while `gcc-toolset-14-gdb` installs a DAP-capable 14.2 under
        /opt/rh, so:

        * an explicit path (`--adapter /path/to/gdb` or config.json's
          ``adapters.gdb``, which tdb seeds with the PATH gdb of its
          first run) is honored, and rejected with a hint naming the
          path, its version, and any usable toolset gdb, when it is
          provably too old;
        * the PATH gdb, when missing or provably too old, gives way to
          the newest toolset gdb (logged), else the same hint;
        * a gdb whose version can't be read is used as-is; the DAP
          client's death diagnosis still names the remedy if it fails.
        """
        explicit = self._executable
        exe = explicit or shutil.which("gdb")
        toolset = None
        if exe is None:
            toolset = native_tools.find_dap_capable_gdb()
            if toolset is not None:
                log.info(
                    "gdb not on PATH; using %s (gdb %s)",
                    toolset[0],
                    _dotted(toolset[1]),
                )
                return toolset[0]
            raise AdapterNotFoundError(
                "gdb not found on PATH — install GDB >= 14 (its DAP mode), "
                'or set {"adapters": {"gdb": "/path/to/gdb"}} in '
                "tdb's config.json"
            )
        version = native_tools.tool_version_tuple(exe)
        if version is None or version >= native_tools.GDB_DAP_MIN_VERSION:
            return exe
        toolset = native_tools.find_dap_capable_gdb()
        if toolset is not None and explicit is None:
            log.info(
                "gdb on PATH (%s) is %s, too old for DAP; using %s (gdb %s)",
                exe,
                _dotted(version),
                toolset[0],
                _dotted(toolset[1]),
            )
            return toolset[0]
        raise AdapterNotFoundError(gdb_too_old_hint(exe, version, explicit, toolset))

    def command(self) -> list[str]:
        return [self.resolve_executable(), *gdb_init_args(), "-i", "dap"]

    def diagnose_exit(self, stderr: str) -> str | None:
        if GDB_NO_DAP_INTERPRETER not in stderr:
            return None
        exe = self._executable or shutil.which("gdb") or "gdb"
        return (
            f"{exe} has no DAP interpreter: tdb needs GDB >= 14 built with "
            "Python support (`gdb --configuration` shows --with-python); "
            "on RHEL 8 install gcc-toolset-14-gdb and run `tdb --info` to "
            "see which gdb tdb picks, or point it at one with "
            '`--adapter /path/to/gdb` or {"adapters": {"gdb": '
            '"/path/to/gdb"}} in tdb\'s config.json'
        )

    def breakpoint_query_command(self) -> str | None:
        return GDB_BREAKPOINT_QUERY

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
        if console == "externalTerminal":
            raise LanguageNotSupportedError(
                "--terminal is not supported with the gdb adapter (gdb's "
                "DAP mode has no terminal integration) — use "
                "`--adapter lldb-dap`"
            )
        body: dict[str, Any] = {
            "type": "gdb",
            "request": "launch",
            "program": program,
            "args": args,
            "cwd": cwd,
            # GDB's DAP name for stop-on-entry.
            "stopAtBeginningOfMainSubprogram": stop_on_entry,
        }
        if env:
            body["env"] = env  # GDB takes a mapping, unlike lldb-dap
        return body

    def attach_body(
        self, *, host: str, port: int, opts: dict[str, Any]
    ) -> dict[str, Any]:
        program = _required_program(opts)
        if self._attach_pid is not None:
            return {"program": program, "pid": self._attach_pid}
        # gdb-dap passes "target" to `target remote`.
        return {"program": program, "target": f"{host}:{port}"}

    def pre_configuration_commands(
        self, path_mappings: list[tuple[str, str]]
    ) -> tuple[str, ...]:
        return tuple(
            f"set substitute-path {quote_debugger_arg(remote)} {quote_debugger_arg(local)}"
            for local, remote in path_mappings
        )

    def pre_stack_trace_commands(self) -> tuple[str, ...]:
        # RHEL 8 gdb: frame source locations (`info source`, stackTrace
        # `source`) stay empty until `list` selects the default source
        # symtab. `list` with no argument picks the CU containing main
        # and has no other effect on the session.
        return ("list",)


# gdb: `set $name = expr` creates a convenience variable; lldb: a `$name`
# declared in an expression (`int $name = 42`, or the same after a
# backtick CLI escape) is a persistent expression variable. Neither
# appears in any DAP scope; both read back as `$name` in "watch"
# context. Shared by every profile that runs on these adapters (cpp,
# rust, native OCaml).
GDB_INTERACTIVE_VARIABLE = assignment_matcher(
    r"^\s*set\s+(?:var\s+)?(?P<name>\$[A-Za-z_]\w*)\s*="
)
LLDB_INTERACTIVE_VARIABLE = assignment_matcher(
    r"^[^=]*?(?<![\w$])(?P<name>\$[A-Za-z_]\w*)\s*=(?!=)"
)
NATIVE_INTERACTIVE_VARIABLE = {
    "gdb": GDB_INTERACTIVE_VARIABLE,
    "lldb-dap": LLDB_INTERACTIVE_VARIABLE,
}


def build_cpp_profile(
    adapter: str | None = None,
    adapter_paths: dict[str, str] | None = None,
    program: str | None = None,
    *,
    attach_pid: int | None = None,
) -> LanguageProfile:
    adapters: dict[str, type[AdapterSpec]] = {
        "lldb-dap": LldbDapAdapter,
        "gdb": GdbDapAdapter,
    }
    adapter_id = adapter or "gdb"
    if adapter_id not in adapters:
        raise LanguageNotSupportedError(
            f"unknown adapter {adapter_id!r} for cpp "
            f"(known: {', '.join(sorted(adapters))}; note: codelldb is "
            f"not packaged standalone — use lldb-dap)"
        )
    executable = (adapter_paths or {}).get(adapter_id)
    return LanguageProfile(
        id="cpp",
        display_name="C/C++",
        adapter=adapters[adapter_id](executable=executable, attach_pid=attach_pid),
        presentation=Presentation(lexer="cpp"),
        # Verified (Task 9, tests/integration/test_cpp_pause.py): DAP
        # `pause` reliably stops a never-stopped, actively-looping
        # debuggee on both gdb -i dap and lldb-dap.
        capabilities=ProfileCapabilities(
            pause_while_running=True,
            interactive_variable=NATIVE_INTERACTIVE_VARIABLE[adapter_id],
            breakpoint_hook_frame=is_breakpoint_hook_frame,
        ),
    )

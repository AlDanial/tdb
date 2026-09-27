# Native Live Breakpoint Hooks (C/C++, Rust, OCaml) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let C, C++, Rust, and native OCaml programs call a one-line hook that opens tdb attached to the live process, paused on the next line, by generalizing `tdb -a PID` from Go to every gdb/lldb-dap debuggee.

**Architecture:** The gdb and lldb-dap adapters learn a local pid attach (`{"program", "pid"}` bodies) and install one hidden function breakpoint on the C symbol `tdb_breakpoint_stop` during attach configuration. Each language ships a hook library (header `tdb.h`, Rust crate `tdb`, OCaml library `Tdb`) that grants ptrace under Yama, spawns `tdb --lang <x> -a <pid> --no-pause-on-attach` on the program's tty, waits for TracerPid, and calls `tdb_breakpoint_stop()`; tdb's existing hook-frame step-out walks up to the caller.

**Tech Stack:** Python 3.11+ (tdb), gdb >= 14 `-i dap`, lldb-dap (LLVM >= 17), C99/C++ header, Rust (no dependencies), OCaml 5 with C stubs, pytest, pytest-asyncio (`asyncio_mode = auto`).

**Spec:** `docs/superpowers/specs/2026-09-27-native-breakpoint-hooks-design.md`

## Global Constraints

- Hooks and `tdb -a` language sniffing are Linux only (`/proc`, `PR_SET_PTRACER`); non-Linux hook builds warn once and return.
- The stop symbol is exactly `tdb_breakpoint_stop` in all three languages; the hidden function breakpoint is never entered into `state.breakpoints`.
- The hooks spawn exactly `tdb --lang <lang> -a <pid> --no-pause-on-attach` with `<lang>` in `cpp`, `rust`, `ocaml`, using `$TDB` if set, else `tdb` on PATH. No other environment variables are read.
- Timeouts (from the spec): 3 s wait for a previous tdb to exit, 60 s wait for attach, 50 ms poll.
- A hook must never stop the process when no debugger is attached, and must never abort the program: every failure is a stderr warning followed by normal execution.
- The Rust crate has zero dependencies (CI has rustc but no cargo). OCaml test builds use plain `ocamlopt` (CI has no dune or ocamlfind).
- `--remote-attach` behavior is unchanged for every adapter.
- Tests run with `uv run pytest` and use `uv pip install` for anything needed (per CLAUDE.md and memory).
- OCaml bytecode (ocamlearlybird) is rejected for `-a` with a clear error.

## Review Focus

1. **Stripped or `-O2` binary:** the hook still spawns tdb and waits; tdb attaches but the function breakpoint never resolves, so the program just runs on after tdb attaches. Pinned by Task 3's `test_hook_function_breakpoint_unverified_is_not_an_error` (controller ignores `verified: false`) and documented in Task 9.
2. **Yama `ptrace_scope` 2/3 or a container without `SYS_PTRACE`:** attach fails; the hook must time out with a warning and continue. Pinned by Task 5's `test_header_hook_times_out_when_no_attach` (short timeout override for the test via a compile-time macro).
3. **Hook called from a non-terminal (pipe, service):** must be a silent no-op. Pinned by Task 8's `test_hook_is_noop_without_tty` for the C header (the same code path serves OCaml; Rust has its own `test_rust_hook_noop_without_tty` in Task 6).
4. **`tdb -a PID` on a pid that is not a native ELF (a Python interpreter, a shell script):** must error out with a message naming `--lang`, not attach gdb to python. Pinned by Task 4's `test_detect_executable_rejects_non_elf`.
5. **gdb pid attach where the attach stop never arrives (attach refused by the kernel):** the controller must not hang forever waiting for the stop; it times out and surfaces the attach failure. Pinned by Task 3's `test_pid_attach_stop_wait_times_out_without_hanging`.

---

## File map

| Path | Responsibility |
|---|---|
| `src/tdb/languages/base.py` | `AdapterQuirks.attach_stop_is_pausable`, `AdapterSpec.hook_function_breakpoints()` |
| `src/tdb/languages/cpp.py` | pid attach for `GdbDapAdapter`/`LldbDapAdapter`, `HOOK_STOP_FUNCTION`, `NATIVE_HOOK_FRAMES`, `is_breakpoint_hook_frame`, `attach_pid` in `build_cpp_profile` |
| `src/tdb/languages/rust.py` | `attach_pid` in `build_rust_profile`, `is_rust_binary`, Rust `is_breakpoint_hook_frame` |
| `src/tdb/languages/ocaml.py` | `attach_pid` in `build_ocaml_profile`, OCaml lldb quirk flip, OCaml `is_breakpoint_hook_frame` |
| `src/tdb/languages/registry.py` | `resolve(..., attach_pid=)`, `detect_executable(path)` |
| `src/tdb/session/controller.py` | install hook function breakpoints on attach; wait for gdb's late attach stop; pause/resume decision |
| `src/tdb/cli.py` | `-a` for cpp/rust/ocaml, program default from `/proc/PID/exe`, pid sniffing, help text |
| `src/tdb/info.py` | "C/C++" section with `tdb.h dir` row |
| `src/tdb/adapters/native/__init__.py`, `src/tdb/adapters/native/tdb.h` | packaged C/C++ hook header |
| `pyproject.toml` | package-data entry for the header |
| `rust/tdb/Cargo.toml`, `rust/tdb/src/lib.rs` | Rust hook crate |
| `ocaml/tdb/tdb.ml`, `ocaml/tdb/tdb_stubs.c`, `ocaml/tdb/tdb.h`, `ocaml/tdb/dune`, `ocaml/tdb/dune-project`, `ocaml/tdb/tdb.opam` | OCaml hook library |
| `examples/C/breakpoint_hook_demo.c`, `examples/C++/breakpoint_hook_demo.cpp`, `examples/Rust/breakpoint_hook_demo/{Cargo.toml,src/main.rs}`, `examples/OCaml/breakpoint_hook_demo.ml` | demos |
| `tests/unit/test_cpp_profile.py`, `tests/unit/test_rust_profile.py`, `tests/unit/test_ocaml_profile.py` | adapter/predicate tests |
| `tests/unit/test_controller_pid_attach.py` | controller tests with a stub client |
| `tests/unit/test_cli_native_attach.py` | CLI tests |
| `tests/unit/test_native_hook_header.py`, `tests/unit/test_rust_hook_crate.py`, `tests/unit/test_ocaml_hook_library.py` | compile/link/no-op tests per library (skip without toolchain) |
| `tests/integration/test_native_breakpoint_hook.py` | end-to-end attach via real gdb/lldb-dap |
| `README.md`, `examples/README.md` | documentation |

Verified facts every task may rely on (probe of 2026-09-27, gdb 17.1, lldb-dap 21.1.8): both adapters accept `pid` in the attach body; a `setFunctionBreakpoints` sent before `configurationDone` is `pending` in gdb and resolves when gdb loads the program at attach; gdb sends the attach response first and then `stopped` with reason `attach`; lldb-dap answers `attach` before `initialized` and honors `stopOnEntry` at `configurationDone`; the hook stop is reason `breakpoint` with frames `tdb_breakpoint_stop` -> `tdb_breakpoint` -> caller; `disconnect` with `terminateDebuggee: false` detaches; a second attach works.

---

### Task 1: Adapter pid attach bodies, hook function breakpoints, pausable quirk

**Files:**
- Modify: `src/tdb/languages/base.py` (class `AdapterQuirks` ~line 43-78; class `AdapterSpec` methods ~line 118-175)
- Modify: `src/tdb/languages/cpp.py` (`LldbDapAdapter`, `GdbDapAdapter`, `build_cpp_profile`)
- Modify: `src/tdb/languages/rust.py` (`build_rust_profile`)
- Modify: `src/tdb/languages/ocaml.py` (`OCamlLldbAdapter`, `build_ocaml_profile`)
- Modify: `src/tdb/languages/registry.py` (`resolve`)
- Test: `tests/unit/test_cpp_profile.py`, `tests/unit/test_rust_profile.py`, `tests/unit/test_ocaml_profile.py`

**Interfaces:**
- Produces: `AdapterQuirks.attach_stop_is_pausable: bool = False`; `AdapterSpec.hook_function_breakpoints(self) -> tuple[str, ...]` (default `()`); `cpp.HOOK_STOP_FUNCTION = "tdb_breakpoint_stop"`; `LldbDapAdapter(executable=None, attach_pid=None)`, `GdbDapAdapter(executable=None, attach_pid=None)`; `build_cpp_profile(adapter=None, adapter_paths=None, program=None, *, attach_pid=None)`, same keyword on `build_rust_profile` and `build_ocaml_profile`; `registry.resolve(lang_id, adapter=None, adapter_paths=None, program=None, attach_pid=None)`.

- [ ] **Step 1: Write the failing adapter tests**

Append to `tests/unit/test_cpp_profile.py`:

```python
from tdb.languages.cpp import HOOK_STOP_FUNCTION


def test_gdb_pid_attach_body_and_quirks():
    gdb = GdbDapAdapter(attach_pid=4242)
    assert gdb.attach_body(host="127.0.0.1", port=0, opts={"program": "/bin/prog"}) == {
        "program": "/bin/prog",
        "pid": 4242,
    }
    assert gdb.quirks.attach_via_adapter is True
    assert gdb.quirks.attach_requires_local_program is True
    # The controller decides pause vs resume for pid attach; the
    # remote-stub "always resume" quirk must be off.
    assert gdb.quirks.attach_stop_is_pausable is True
    assert gdb.quirks.resume_after_remote_attach is False
    assert gdb.hook_function_breakpoints() == (HOOK_STOP_FUNCTION,)


def test_gdb_remote_attach_unchanged_without_pid():
    gdb = GdbDapAdapter()
    assert gdb.attach_body(host="h", port=9, opts={"program": "/bin/prog"}) == {
        "program": "/bin/prog",
        "target": "h:9",
    }
    assert gdb.quirks.resume_after_remote_attach is True
    assert gdb.quirks.attach_stop_is_pausable is False
    assert gdb.hook_function_breakpoints() == ()


def test_lldb_pid_attach_body_honors_pause_option():
    lldb = LldbDapAdapter(attach_pid=4242)
    assert lldb.attach_body(
        host="127.0.0.1", port=0, opts={"program": "/bin/prog"}
    ) == {
        "program": "/bin/prog",
        "pid": 4242,
        "stopOnEntry": True,
    }
    assert (
        lldb.attach_body(
            host="127.0.0.1",
            port=0,
            opts={"program": "/bin/prog", "pause_on_attach": False},
        )["stopOnEntry"]
        is False
    )
    assert lldb.quirks.attach_stop_is_pausable is False
    assert lldb.hook_function_breakpoints() == (HOOK_STOP_FUNCTION,)
    assert LldbDapAdapter().hook_function_breakpoints() == ()


def test_pid_attach_requires_program():
    with pytest.raises(LanguageNotSupportedError):
        GdbDapAdapter(attach_pid=1).attach_body(host="127.0.0.1", port=0, opts={})


def test_build_cpp_profile_forwards_attach_pid():
    p = build_cpp_profile(attach_pid=77)
    assert p.adapter.hook_function_breakpoints() == (HOOK_STOP_FUNCTION,)
    p = build_cpp_profile(adapter="lldb-dap", attach_pid=77)
    assert p.adapter.attach_body(host="", port=0, opts={"program": "x"})["pid"] == 77
    assert registry.resolve(
        "cpp", attach_pid=77
    ).adapter.hook_function_breakpoints() == (HOOK_STOP_FUNCTION,)
```

Append to `tests/unit/test_rust_profile.py`:

```python
from tdb.languages.cpp import HOOK_STOP_FUNCTION
from tdb.languages.rust import build_rust_profile


def test_rust_pid_attach_keeps_init_commands():
    p = build_rust_profile(adapter="lldb-dap", attach_pid=99)
    body = p.adapter.attach_body(
        host="127.0.0.1", port=0, opts={"program": "/bin/prog"}
    )
    assert body["pid"] == 99
    assert body["program"] == "/bin/prog"
    assert any("command script import" in c for c in body["initCommands"])
    assert p.adapter.hook_function_breakpoints() == (HOOK_STOP_FUNCTION,)
    g = build_rust_profile(adapter="gdb", attach_pid=99)
    assert g.adapter.attach_body(host="", port=0, opts={"program": "/bin/prog"}) == {
        "program": "/bin/prog",
        "pid": 99,
    }
    assert g.adapter.quirks.attach_stop_is_pausable is True
```

Append to `tests/unit/test_ocaml_profile.py`:

```python
from tdb.languages.base import LanguageNotSupportedError
from tdb.languages.cpp import HOOK_STOP_FUNCTION
from tdb.languages.ocaml import build_ocaml_profile


def test_ocaml_lldb_pid_attach_opts_into_attach_via_adapter():
    p = build_ocaml_profile(adapter="lldb-dap", attach_pid=5)
    assert p.adapter.quirks.attach_via_adapter is True
    body = p.adapter.attach_body(
        host="127.0.0.1", port=0, opts={"program": "/bin/prog"}
    )
    assert body["pid"] == 5
    assert any("lldb_formatters.py" in c for c in body["initCommands"])
    assert p.adapter.hook_function_breakpoints() == (HOOK_STOP_FUNCTION,)


def test_ocaml_lldb_without_pid_keeps_no_attach():
    p = build_ocaml_profile(adapter="lldb-dap")
    assert p.adapter.quirks.attach_via_adapter is False
    assert p.adapter.hook_function_breakpoints() == ()


def test_ocaml_gdb_pid_attach():
    p = build_ocaml_profile(adapter="gdb", attach_pid=5)
    assert p.adapter.attach_body(host="", port=0, opts={"program": "/bin/prog"}) == {
        "program": "/bin/prog",
        "pid": 5,
    }


def test_ocaml_pid_attach_rejects_earlybird():
    with pytest.raises(LanguageNotSupportedError, match="native OCaml adapter"):
        build_ocaml_profile(adapter="ocamlearlybird", attach_pid=5)


def test_ocaml_pid_attach_defaults_to_native_adapter_without_program():
    # No program to sniff a flavor from: pid attach must never pick earlybird.
    assert build_ocaml_profile(attach_pid=5).adapter.id == "lldb-dap"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_cpp_profile.py tests/unit/test_rust_profile.py tests/unit/test_ocaml_profile.py -q --no-cov 2>&1 | tail -15`
Expected: ImportError for `HOOK_STOP_FUNCTION` / TypeError for unexpected keyword `attach_pid`.

- [ ] **Step 3: Add the quirk and the hook method to base.py**

In `AdapterQuirks` (after `resume_after_remote_attach`):

```python
    # Local pid attach (gdb `attach PID`) leaves the inferior stopped, and
    # that stop is the user's to keep: the controller waits for it (gdb
    # reports it after the attach response) and resumes only when
    # pause-on-attach was not requested. Distinct from
    # resume_after_remote_attach, whose stub-entry stop is never useful.
    attach_stop_is_pausable: bool = False
```

In `AdapterSpec`, after `initial_function_breakpoints`:

```python
    def hook_function_breakpoints(self) -> tuple[str, ...]:
        """Function names the live breakpoint hooks stop in.

        The native hooks (tdb.h, the Rust `tdb` crate, OCaml `Tdb`) call
        the C symbol `tdb_breakpoint_stop` once a debugger is attached.
        The controller installs these as hidden DAP function breakpoints
        during attach configuration (before configurationDone; gdb keeps
        them pending until it loads the program). Never shown in the
        Breakpoints view. Empty for adapters/modes without a hook.
        """
        return ()
```

- [ ] **Step 4: Implement pid attach in cpp.py**

Add after `quote_debugger_arg`:

```python
# The C symbol every native live breakpoint hook (tdb.h, Rust `tdb`,
# OCaml `Tdb`) calls once tdb is attached; tdb's hidden function
# breakpoint lands there. Keep in sync with the hook libraries.
HOOK_STOP_FUNCTION = "tdb_breakpoint_stop"
```

`LldbDapAdapter`:

```python
class LldbDapAdapter(AdapterSpec):
    id = "lldb-dap"
    quirks = AdapterQuirks(attach_via_adapter=True, attach_requires_local_program=True)

    def __init__(
        self, executable: str | None = None, attach_pid: int | None = None
    ) -> None:
        self._executable = executable
        self._attach_pid = attach_pid

    def hook_function_breakpoints(self) -> tuple[str, ...]:
        return (HOOK_STOP_FUNCTION,) if self._attach_pid is not None else ()
```

and in its `attach_body`, before the existing body:

```python
        if self._attach_pid is not None:
            # Local pid attach. lldb-dap honors stopOnEntry for attach:
            # True stops the process for the user; the hooks pass
            # --no-pause-on-attach because the program stops itself.
            return {
                "program": _required_program(opts),
                "pid": self._attach_pid,
                "stopOnEntry": opts.get("pause_on_attach", True),
            }
```

`GdbDapAdapter`:

```python
def __init__(
    self, executable: str | None = None, attach_pid: int | None = None
) -> None:
    self._executable = executable
    self._attach_pid = attach_pid
    if attach_pid is not None:
        # gdb `attach PID` stops the inferior; unlike `target remote`
        # that stop is meaningful, so the controller (not this quirk)
        # decides whether to resume.
        self.quirks = AdapterQuirks(
            attach_via_adapter=True,
            attach_requires_local_program=True,
            attach_stop_is_pausable=True,
        )


def hook_function_breakpoints(self) -> tuple[str, ...]:
    return (HOOK_STOP_FUNCTION,) if self._attach_pid is not None else ()
```

and its `attach_body`:

```python
    def attach_body(self, *, host: str, port: int, opts: dict[str, Any]) -> dict[str, Any]:
        program = _required_program(opts)
        if self._attach_pid is not None:
            return {"program": program, "pid": self._attach_pid}
        # gdb-dap passes "target" to `target remote`.
        return {"program": program, "target": f"{host}:{port}"}
```

`build_cpp_profile` signature and construction:

```python
def build_cpp_profile(
    adapter: str | None = None,
    adapter_paths: dict[str, str] | None = None,
    program: str | None = None,
    *,
    attach_pid: int | None = None,
) -> LanguageProfile:
    ...
    return LanguageProfile(
        id="cpp",
        display_name="C/C++",
        adapter=adapters[adapter_id](executable=executable, attach_pid=attach_pid),
        ...
```

- [ ] **Step 5: Forward attach_pid in rust.py and ocaml.py; reject earlybird**

`rust.py`: `RustGdbAdapter`/`RustLldbAdapter` inherit the new constructors. Change `build_rust_profile` to take `*, attach_pid: int | None = None` and construct with `adapters[adapter_id](executable=executable, attach_pid=attach_pid)`. `RustLldbAdapter.attach_body` already calls `super().attach_body(...)` and appends `initCommands`, so the pid body gets them too.

`ocaml.py`, `OCamlLldbAdapter`:

```python
# The C/C++ base adapters support native remote attach; OCaml does
# not offer it yet, so opt back out of the attach-via-adapter quirk
# the base class declares -- except for local pid attach, which the
# live breakpoint hook (Tdb.breakpoint) relies on.
quirks = AdapterQuirks()


def __init__(
    self, executable: str | None = None, attach_pid: int | None = None
) -> None:
    super().__init__(executable=executable, attach_pid=attach_pid)
    if attach_pid is not None:
        self.quirks = AdapterQuirks(
            attach_via_adapter=True, attach_requires_local_program=True
        )


def attach_body(self, *, host, port, opts) -> dict[str, Any]:
    body = super().attach_body(host=host, port=port, opts=opts)
    body["initCommands"] = [
        f"command script import {quote_debugger_arg(formatter_script_path())}",
    ]
    return body
```

`build_ocaml_profile`:

```python
def build_ocaml_profile(
    adapter: str | None = None,
    adapter_paths: dict[str, str] | None = None,
    program: str | None = None,
    *,
    attach_pid: int | None = None,
) -> LanguageProfile:
    ...
    if adapter is None:
        flavor = ocaml_flavor(program) if program else None
        adapter = "ocamlearlybird" if flavor == "bytecode" else "lldb-dap"
    if attach_pid is not None and adapter == "ocamlearlybird":
        raise LanguageNotSupportedError(
            "pid attach needs a native OCaml adapter (--adapter lldb-dap or gdb); "
            "ocamlearlybird debugs bytecode and cannot attach to a process"
        )
    ...
    native = adapter in ("lldb-dap", "gdb")
    spec_cls = adapters[adapter]
    adapter_spec = (
        spec_cls(executable=executable, attach_pid=attach_pid)
        if native
        else spec_cls(executable=executable)
    )
```

and pass `adapter=adapter_spec` to `LanguageProfile`.

`registry.resolve`:

```python
def resolve(
    lang_id: str,
    adapter: str | None = None,
    adapter_paths: dict[str, str] | None = None,
    program: str | None = None,
    attach_pid: int | None = None,
) -> LanguageProfile:
    ...
    adapter, adapter_paths = normalize_adapter(adapter, adapter_paths)
    kwargs: dict = {}
    if attach_pid is not None:
        # Only the builders that support pid attach accept the keyword;
        # the CLI has already limited -a to those languages.
        kwargs["attach_pid"] = attach_pid
    return builder(
        adapter=adapter, adapter_paths=adapter_paths, program=program, **kwargs
    )
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_cpp_profile.py tests/unit/test_rust_profile.py tests/unit/test_ocaml_profile.py tests/unit/test_go_profile.py -q --no-cov 2>&1 | tail -5`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/tdb/languages/base.py src/tdb/languages/cpp.py src/tdb/languages/rust.py src/tdb/languages/ocaml.py src/tdb/languages/registry.py tests/unit/test_cpp_profile.py tests/unit/test_rust_profile.py tests/unit/test_ocaml_profile.py
git commit -m "Native pid attach bodies for gdb/lldb-dap plus hook function breakpoints"
```

---

### Task 2: Hook-frame predicates for cpp, rust, ocaml

**Files:**
- Modify: `src/tdb/languages/cpp.py`, `src/tdb/languages/rust.py`, `src/tdb/languages/ocaml.py`
- Test: `tests/unit/test_cpp_profile.py`, `tests/unit/test_rust_profile.py`, `tests/unit/test_ocaml_profile.py`

**Interfaces:**
- Consumes: `ProfileCapabilities.breakpoint_hook_frame: Callable[[StackFrame], bool] | None` (base.py ~line 326), `StackFrame` from `tdb.dap.types`.
- Produces: `cpp.NATIVE_HOOK_FRAMES: frozenset[str]`, `cpp.is_breakpoint_hook_frame(frame)`, `rust.is_breakpoint_hook_frame(frame)`, `ocaml.is_breakpoint_hook_frame(frame)`; each profile's `capabilities.breakpoint_hook_frame` set to its predicate (OCaml: only for native adapters).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_cpp_profile.py`:

```python
from tdb.dap.types import StackFrame
from tdb.languages.cpp import is_breakpoint_hook_frame


def _frame(name: str) -> StackFrame:
    return StackFrame(id=1, name=name, line=1, column=0)


@pytest.mark.parametrize(
    "name", ["tdb_breakpoint_stop", "tdb_breakpoint", "tdb_breakpoint_lang"]
)
def test_cpp_hook_frames(name):
    assert is_breakpoint_hook_frame(_frame(name)) is True


@pytest.mark.parametrize(
    "name", ["main", "compute", "nanosleep", "", "tdb::breakpoint"]
)
def test_cpp_non_hook_frames(name):
    assert is_breakpoint_hook_frame(_frame(name)) is False


def test_cpp_profile_declares_hook_predicate():
    assert (
        build_cpp_profile().capabilities.breakpoint_hook_frame
        is is_breakpoint_hook_frame
    )
```

Append to `tests/unit/test_rust_profile.py`:

```python
from tdb.dap.types import StackFrame
from tdb.languages.rust import is_breakpoint_hook_frame as rust_hook_frame


def _frame(name: str) -> StackFrame:
    return StackFrame(id=1, name=name, line=1, column=0)


@pytest.mark.parametrize(
    "name", ["tdb_breakpoint_stop", "tdb::breakpoint", "tdb::breakpoint::h1a2b3c"]
)
def test_rust_hook_frames(name):
    assert rust_hook_frame(_frame(name)) is True


@pytest.mark.parametrize("name", ["hooked::main", "compute", "std::thread::sleep"])
def test_rust_non_hook_frames(name):
    assert rust_hook_frame(_frame(name)) is False


def test_rust_profile_declares_hook_predicate():
    assert build_rust_profile().capabilities.breakpoint_hook_frame is rust_hook_frame
```

Append to `tests/unit/test_ocaml_profile.py`:

```python
from tdb.dap.types import StackFrame
from tdb.languages.ocaml import is_breakpoint_hook_frame as ocaml_hook_frame


def _frame(name: str) -> StackFrame:
    return StackFrame(id=1, name=name, line=1, column=0)


@pytest.mark.parametrize(
    "name",
    [
        "tdb_breakpoint_stop",
        "tdb_breakpoint_lang",
        "tdb_ocaml_breakpoint",
        "caml_c_call",
        "camlTdb.breakpoint_123",  # OCaml 5 mangling
        "camlTdb__breakpoint_123",  # OCaml 4 mangling
    ],
)
def test_ocaml_hook_frames(name):
    assert ocaml_hook_frame(_frame(name)) is True


@pytest.mark.parametrize(
    "name",
    [
        "camlMain.entry",
        "camlTdbx.breakpoint_1",
        "caml_main",
        "camlStdlib__List.iter_123",
    ],
)
def test_ocaml_non_hook_frames(name):
    assert ocaml_hook_frame(_frame(name)) is False


def test_ocaml_hook_predicate_only_for_native_adapters():
    assert (
        build_ocaml_profile(adapter="lldb-dap").capabilities.breakpoint_hook_frame
        is ocaml_hook_frame
    )
    assert (
        build_ocaml_profile(adapter="gdb").capabilities.breakpoint_hook_frame
        is ocaml_hook_frame
    )
    assert (
        build_ocaml_profile(adapter="ocamlearlybird").capabilities.breakpoint_hook_frame
        is None
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_cpp_profile.py tests/unit/test_rust_profile.py tests/unit/test_ocaml_profile.py -q --no-cov 2>&1 | tail -5`
Expected: ImportError `is_breakpoint_hook_frame`.

- [ ] **Step 3: Implement the predicates**

`cpp.py` (after `HOOK_STOP_FUNCTION`):

```python
# Frames belonging to the C/C++ hook in tdb.h. The stop lands in
# tdb_breakpoint_stop; stepping out of each of these in turn reaches the
# user's caller (see app_handlers.dap_events._stopped_inside_breakpoint_hook).
NATIVE_HOOK_FRAMES = frozenset(
    {HOOK_STOP_FUNCTION, "tdb_breakpoint", "tdb_breakpoint_lang"}
)


def is_breakpoint_hook_frame(frame) -> bool:
    """True when `frame` (a StackFrame) is inside the tdb.h hook."""
    return (frame.name or "") in NATIVE_HOOK_FRAMES
```

and in `build_cpp_profile`'s `ProfileCapabilities(...)` add `breakpoint_hook_frame=is_breakpoint_hook_frame,`.

`rust.py`:

```python
from tdb.languages.cpp import NATIVE_HOOK_FRAMES  # add to the existing cpp import


def is_breakpoint_hook_frame(frame) -> bool:
    """`tdb::breakpoint()` (rust/tdb) calls the shared C stop symbol; gdb
    and lldb name the Rust frame `tdb::breakpoint`, lldb sometimes with a
    hash suffix (`tdb::breakpoint::h...`)."""
    name = frame.name or ""
    return (
        name in NATIVE_HOOK_FRAMES
        or name == "tdb::breakpoint"
        or name.startswith("tdb::breakpoint::")
    )
```

and `breakpoint_hook_frame=is_breakpoint_hook_frame,` in `build_rust_profile`'s capabilities.

`ocaml.py`:

```python
from tdb.languages.cpp import NATIVE_HOOK_FRAMES  # add to the existing cpp import

# Tdb.breakpoint (ocaml/tdb): OCaml frame (mangled, both OCaml 4 and 5
# forms) -> caml_c_call trampoline -> C stub -> tdb.h recipe -> stop.
_OCAML_HOOK_FRAMES = NATIVE_HOOK_FRAMES | {
    "tdb_ocaml_breakpoint",
    "caml_c_call",
    "caml_c_call_stack_args",
}
_OCAML_HOOK_PREFIXES = ("camlTdb.breakpoint_", "camlTdb__breakpoint_")


def is_breakpoint_hook_frame(frame) -> bool:
    """True when `frame` (raw, un-demangled name) is inside Tdb.breakpoint."""
    name = frame.name or ""
    return name in _OCAML_HOOK_FRAMES or name.startswith(_OCAML_HOOK_PREFIXES)
```

and in `build_ocaml_profile`'s capabilities: `breakpoint_hook_frame=is_breakpoint_hook_frame if native else None,`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_cpp_profile.py tests/unit/test_rust_profile.py tests/unit/test_ocaml_profile.py tests/unit/test_dap_event_coordinator.py -q --no-cov 2>&1 | tail -5`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/languages/cpp.py src/tdb/languages/rust.py src/tdb/languages/ocaml.py tests/unit/test_cpp_profile.py tests/unit/test_rust_profile.py tests/unit/test_ocaml_profile.py
git commit -m "Hook-frame predicates for the native live breakpoint hooks"
```

---

### Task 3: Controller: install hook breakpoints, wait for gdb's late attach stop, pause or resume

**Files:**
- Modify: `src/tdb/session/controller.py` (`do_configure`, ~lines 373-536)
- Test: `tests/unit/test_controller_pid_attach.py` (new)

**Interfaces:**
- Consumes: `AdapterQuirks.attach_stop_is_pausable`, `AdapterSpec.hook_function_breakpoints()` (Task 1); `DAPClient.set_function_breakpoints(names) -> list[Breakpoint]`; controller fields `_is_remote_attach`, `_pre_arm_pause`, `_stopped_event`, `_suppress_next_stop`, `_launch_future`, `_resume_client`.
- Produces: no new public API. Behavior: on attach, `set_function_breakpoints(hook names)` is sent before `configuration_done()`; for `attach_stop_is_pausable` adapters the controller awaits the attach stop (10 s), keeps it when `_pre_arm_pause` is True, otherwise resumes and suppresses the stop from the UI.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_controller_pid_attach.py`:

```python
"""Native pid attach (`tdb -a PID` on gdb/lldb-dap): controller behavior.

gdb's `attach PID` reports its stop AFTER the attach response (the reverse
of `target remote`), and the live breakpoint hooks need a hidden function
breakpoint on `tdb_breakpoint_stop` installed before configurationDone.
Exercised here with a stub DAP client; the real adapters are covered by
tests/integration/test_native_breakpoint_hook.py.
"""

from __future__ import annotations

import asyncio

import pytest

from tdb.dap.messages import Response
from tdb.dap.types import Breakpoint, Capabilities
from tdb.languages.cpp import HOOK_STOP_FUNCTION, build_cpp_profile
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController
from tdb.session.state import SessionPhase


class _StubDAPClient:
    def __init__(self, *, stop_after_attach: bool = True, verified: bool = True):
        self.events: dict = {}
        self.reverse_handlers: dict = {}
        self.capabilities = Capabilities()
        self.calls: list[str] = []
        self.function_breakpoints: list[list[str]] = []
        self.resumed = 0
        self._stop_after_attach = stop_after_attach
        self._verified = verified

    def on_event(self, name, fn):
        self.events[name] = fn

    def on_reverse_request(self, name, fn):
        self.reverse_handlers[name] = fn

    async def set_exception_breakpoints(self, filters):
        self.calls.append("set_exception_breakpoints")

    async def set_function_breakpoints(self, names):
        self.calls.append("set_function_breakpoints")
        self.function_breakpoints.append(list(names))
        return [Breakpoint(id=1, verified=self._verified, line=None) for _ in names]

    async def configuration_done(self):
        self.calls.append("configuration_done")

    async def continue_(self, thread_id):
        self.calls.append("continue")
        self.resumed += 1
        return None

    async def threads(self):
        return []


def _make_controller(pause: bool, client: _StubDAPClient) -> DebugController:
    ctrl = DebugController(
        ServerEventHandler(), profile=build_cpp_profile(attach_pid=4242)
    )
    ctrl.client = client  # type: ignore[assignment]
    ctrl._setup_event_handlers()
    ctrl._is_remote_attach = True
    ctrl._pre_arm_pause = pause
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    fut.set_result(
        Response(seq=1, request_seq=1, command="attach", success=True, body={})
    )
    ctrl._launch_future = fut
    return ctrl


def _late_attach_stop(
    ctrl: DebugController, client: _StubDAPClient, delay: float = 0.05
):
    """gdb order: the stopped(attach) event lands after the attach response."""
    from tdb.dap.messages import Event

    async def fire():
        await asyncio.sleep(delay)
        client.events["stopped"](
            Event(seq=2, event="stopped", body={"threadId": 1, "reason": "attach"})
        )

    return asyncio.ensure_future(fire())


async def test_hook_function_breakpoint_installed_before_configuration_done():
    client = _StubDAPClient()
    ctrl = _make_controller(pause=True, client=client)
    task = _late_attach_stop(ctrl, client)
    await ctrl.do_configure()
    await task
    assert client.function_breakpoints == [[HOOK_STOP_FUNCTION]]
    assert client.calls.index("set_function_breakpoints") < client.calls.index(
        "configuration_done"
    )
    # Hidden: never enters the source-breakpoint table.
    assert ctrl.state.breakpoints == {}


async def test_pid_attach_with_pause_keeps_the_attach_stop():
    client = _StubDAPClient()
    ctrl = _make_controller(pause=True, client=client)
    handler = ctrl.event_handler
    task = _late_attach_stop(ctrl, client)
    await ctrl.do_configure()
    await task
    assert ctrl.state.phase == SessionPhase.STOPPED
    assert client.resumed == 0
    assert handler.last_stop_reason == "attach"  # reported to the UI


async def test_pid_attach_without_pause_resumes_and_hides_the_attach_stop():
    client = _StubDAPClient()
    ctrl = _make_controller(pause=False, client=client)
    handler = ctrl.event_handler
    task = _late_attach_stop(ctrl, client)
    await ctrl.do_configure()
    await task
    assert client.resumed == 1
    assert ctrl.state.phase == SessionPhase.RUNNING
    assert (
        handler.last_stop_reason is None
    )  # suppressed: the program is about to stop itself
    assert ctrl._suppress_next_stop is False  # reset for the hook's real stop


async def test_hook_function_breakpoint_unverified_is_not_an_error():
    """Stripped binary: gdb/lldb cannot resolve tdb_breakpoint_stop. Attach
    must still complete; the program simply runs on."""
    client = _StubDAPClient(verified=False)
    ctrl = _make_controller(pause=False, client=client)
    task = _late_attach_stop(ctrl, client)
    await ctrl.do_configure()
    await task
    assert client.resumed == 1


async def test_pid_attach_stop_wait_times_out_without_hanging(monkeypatch):
    """Attach refused (ptrace_scope, container): no stop ever arrives. The
    controller must give up on the wait and leave the session RUNNING, not
    hang forever."""
    import tdb.session.controller as controller_mod

    monkeypatch.setattr(controller_mod, "ATTACH_STOP_TIMEOUT", 0.05)
    client = _StubDAPClient(stop_after_attach=False)
    ctrl = _make_controller(pause=True, client=client)
    await asyncio.wait_for(ctrl.do_configure(), timeout=2)
    assert ctrl.state.phase == SessionPhase.RUNNING
    assert client.resumed == 0


async def test_remote_stub_attach_path_unchanged():
    """`--remote-attach` to gdbserver: stop lands before the response and
    is always resumed; no hook breakpoint (no pid)."""
    from tdb.dap.messages import Event

    client = _StubDAPClient()
    ctrl = DebugController(ServerEventHandler(), profile=build_cpp_profile())
    ctrl.client = client  # type: ignore[assignment]
    ctrl._setup_event_handlers()
    ctrl._is_remote_attach = True
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    fut.set_result(
        Response(seq=1, request_seq=1, command="attach", success=True, body={})
    )
    ctrl._launch_future = fut
    client.events["stopped"](
        Event(seq=2, event="stopped", body={"threadId": 1, "reason": "attach"})
    )
    await ctrl.do_configure()
    assert client.function_breakpoints == []
    assert client.resumed == 1
    assert ctrl.state.phase == SessionPhase.RUNNING
```

Check the stub against the real client surface before running: `_resume_client` is at `controller.py:601`; read it and make the stub provide whatever method it calls (`continue_(thread_id)` or similar) with the right name and signature. Also check `Breakpoint`'s constructor in `tdb/dap/types.py` and adjust the stub's `Breakpoint(...)` call to its real fields.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_controller_pid_attach.py -q --no-cov 2>&1 | tail -15`
Expected: failures: no function breakpoints sent; `ATTACH_STOP_TIMEOUT` attribute missing; resumed counts wrong.

- [ ] **Step 3: Implement in controller.py**

Module-level constant near the other timeouts/imports at the top of `controller.py`:

```python
# How long a pid attach may take to report its stop after the attach
# response (gdb `attach PID` order). Module-level so tests can shrink it.
ATTACH_STOP_TIMEOUT = 10.0
```

In `do_configure`, replace the `else:` of `if not self._is_remote_attach and self._launch_params:` (currently `bootstrap_entry = False`) with:

```python
        else:
            bootstrap_entry = False
            # Live breakpoint hooks (tdb.h, Rust `tdb`, OCaml `Tdb`) stop in
            # a known C function once tdb is attached. Install the hidden
            # function breakpoint now: lldb-dap has the target loaded
            # already; gdb keeps it pending until `attach` loads the
            # program. An unverified result (stripped binary) is not an
            # error -- the program then simply runs on after attach.
            hook_functions = self.profile.adapter.hook_function_breakpoints()
            if hook_functions:
                await self.client.set_function_breakpoints(list(hook_functions))
```

Before `await self.client.configuration_done()`:

```python
        quirks = self.profile.adapter.quirks
        pid_attach_resumes = (
            self._is_remote_attach
            and quirks.attach_stop_is_pausable
            and not self._pre_arm_pause
        )
        if pid_attach_resumes:
            # The attach stop is about to be resumed; keep it off the UI
            # (the program will stop itself at the hook).
            self._suppress_next_stop = True
```

Replace the existing "gdb's `target remote` attach leaves the inferior stopped ..." block with:

```python
        if self._is_remote_attach and quirks.attach_stop_is_pausable:
            # gdb `attach PID` reports its stop after the attach response
            # (the reverse of `target remote`); wait for it so the pause /
            # resume decision below sees it. A refused attach never stops:
            # give up and let the session proceed as running.
            if self.state.phase != SessionPhase.STOPPED:
                try:
                    await asyncio.wait_for(
                        self._stopped_event.wait(), timeout=ATTACH_STOP_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    log.warning("pid attach: no stop event after attach")
                    self._suppress_next_stop = False

        # gdb's `target remote` attach leaves the inferior stopped at the
        # stub's entry point (resume_after_remote_attach): resume so attach
        # means "join a running program" like every other adapter. A pid
        # attach stop is the user's to keep (attach_stop_is_pausable) unless
        # --no-pause-on-attach said the program stops itself.
        if (
            self._is_remote_attach
            and (quirks.resume_after_remote_attach or pid_attach_resumes)
            and self.state.phase == SessionPhase.STOPPED
        ):
            self.state.transition_to(SessionPhase.RUNNING)
            self.state.clear_frame_data()
            self._stopped_event.clear()
            await self._resume_client(self.client)
        self._suppress_next_stop = False if pid_attach_resumes else self._suppress_next_stop
```

Keep the trailing "Don't clobber STOPPED" block as is. Make sure `asyncio` is imported (it is) and that `_suppress_next_stop` is reset in all three exits of the pid path (stop resumed, timeout, pause kept). For the pause-kept case `pid_attach_resumes` is False so nothing was set.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_controller_pid_attach.py tests/unit/test_controller_configure_race.py tests/unit/test_controller_pre_arm_pause.py -q --no-cov 2>&1 | tail -5`
Expected: all PASS.

- [ ] **Step 5: Run the Rust remote-attach integration test (guards `--remote-attach`)**

Run: `uv run pytest tests/integration/test_rust_remote_attach.py -q --no-cov 2>&1 | tail -5`
Expected: PASS (or skip if no gdbserver/lldb-server is installed; note which in the commit message).

- [ ] **Step 6: Commit**

```bash
git add src/tdb/session/controller.py tests/unit/test_controller_pid_attach.py
git commit -m "Controller: hidden hook breakpoint on attach; wait for gdb's late pid-attach stop"
```

---

### Task 4: CLI: `-a PID` for native languages, program from /proc, pid sniffing

**Files:**
- Modify: `src/tdb/cli.py` (`-a` help ~line 61-68; `_detect_lang` ~line 502-520; `_resolve_language` ~line 522-600)
- Modify: `src/tdb/languages/registry.py` (add `detect_executable`)
- Modify: `src/tdb/languages/rust.py` (add `is_rust_binary`)
- Test: `tests/unit/test_cli_native_attach.py` (new), `tests/unit/test_registry_rust.py` (append)

**Interfaces:**
- Consumes: `registry.resolve(..., attach_pid=)` (Task 1), `go.is_go_binary(path)`, `ocaml.ocaml_flavor(path) -> "native" | "bytecode" | None`.
- Produces: `registry.detect_executable(path: str) -> str` (one of `go`, `ocaml`, `rust`, `cpp`; raises `LanguageNotSupportedError` otherwise); `rust.is_rust_binary(path: str) -> bool`; `cli.PID_ATTACH_LANGUAGES = ("go", "cpp", "rust", "ocaml")`; after `parse_args`, `args.program` is the pid's executable path for native pid attach when none was given.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_cli_native_attach.py`:

```python
"""`tdb -a PID` for native (gdb/lldb-dap) debuggees: C/C++, Rust, OCaml.

The live breakpoint hooks spawn `tdb --lang <x> -a <pid> --no-pause-on-attach`;
a user may also attach by hand, with or without --lang. Detection reads the
pid's executable through /proc (Linux only).
"""

from __future__ import annotations

import pytest

from tdb.cli import parse_args
from tdb.languages import registry
from tdb.languages.base import LanguageNotSupportedError
from tdb.languages.cpp import HOOK_STOP_FUNCTION

ELF = b"\x7fELF" + b"\0" * 60
GO_BUILDINFO_MAGIC = b"\xff Go buildinf:"


@pytest.fixture
def fake_proc(tmp_path, monkeypatch):
    """Make /proc/4242/exe resolve to a file we control."""
    exe = tmp_path / "prog"
    # Both the language sniff and the program default go through _pid_exe.
    monkeypatch.setattr("tdb.cli.sys.platform", "linux")
    monkeypatch.setattr("tdb.cli._pid_exe", lambda pid: str(exe))
    return exe


@pytest.mark.parametrize("lang", ["cpp", "rust", "ocaml"])
def test_attach_pid_accepted_for_native_languages(lang, fake_proc):
    fake_proc.write_bytes(ELF)
    args = parse_args(["--lang", lang, "-a", "4242"])
    assert args.attach_pid == 4242
    assert args.attach_host == "127.0.0.1" and args.attach_port == 0
    assert args.profile.id == lang
    assert args.profile.adapter.hook_function_breakpoints() == (HOOK_STOP_FUNCTION,)
    # gdb/lldb need a symbol-bearing program: defaulted from the pid's exe.
    assert args.program == str(fake_proc)


def test_attach_pid_explicit_program_wins(fake_proc, tmp_path):
    fake_proc.write_bytes(ELF)
    other = tmp_path / "prog-with-symbols"
    other.write_bytes(ELF)
    args = parse_args(["--lang", "cpp", "-a", "4242", str(other)])
    assert args.program == str(other)


def test_attach_pid_rejected_for_python(tmp_path):
    py = tmp_path / "x.py"
    py.write_text("print(1)\n")
    with pytest.raises(SystemExit):
        parse_args(["-a", "4242", str(py)])


def test_attach_pid_rejects_earlybird(fake_proc):
    fake_proc.write_bytes(ELF)
    with pytest.raises(SystemExit):
        parse_args(["--lang", "ocaml", "--adapter", "ocamlearlybird", "-a", "4242"])


def test_no_pause_on_attach_with_native_pid(fake_proc):
    fake_proc.write_bytes(ELF)
    args = parse_args(["--lang", "cpp", "-a", "4242", "--no-pause-on-attach"])
    assert args.no_pause_on_attach is True


def test_sniff_cpp_from_plain_elf(fake_proc):
    fake_proc.write_bytes(ELF)
    assert parse_args(["-a", "4242"]).profile.id == "cpp"


def test_sniff_rust_from_runtime_symbols(fake_proc):
    fake_proc.write_bytes(ELF + b"\0rust_eh_personality\0")
    assert parse_args(["-a", "4242"]).profile.id == "rust"


def test_sniff_ocaml_from_runtime_markers(fake_proc):
    fake_proc.write_bytes(ELF + b"\0caml_startup\0caml_program\0")
    assert parse_args(["-a", "4242"]).profile.id == "ocaml"
    # native, not earlybird, since a pid cannot be bytecode-debugged
    assert parse_args(["-a", "4242"]).profile.adapter.id in ("lldb-dap", "gdb")


def test_sniff_go_still_wins(fake_proc):
    fake_proc.write_bytes(ELF + GO_BUILDINFO_MAGIC + b"\0" * 32)
    assert parse_args(["-a", "4242"]).profile.id == "go"


def test_detect_executable_rejects_non_elf(tmp_path):
    script = tmp_path / "prog"
    script.write_bytes(b"#!/usr/bin/python3\nprint(1)\n")
    with pytest.raises(LanguageNotSupportedError, match="--lang"):
        registry.detect_executable(str(script))


def test_sniff_requires_proc_off_linux(monkeypatch):
    monkeypatch.setattr("tdb.cli.sys.platform", "darwin")
    with pytest.raises(SystemExit):
        parse_args(["-a", "4242"])


def test_native_pid_attach_off_linux_needs_program(monkeypatch):
    monkeypatch.setattr("tdb.cli.sys.platform", "darwin")
    with pytest.raises(SystemExit):
        parse_args(["--lang", "cpp", "-a", "4242"])
```

Append to `tests/unit/test_registry_rust.py`:

```python
from tdb.languages.rust import is_rust_binary


def test_is_rust_binary_scans_for_runtime_symbols(tmp_path):
    f = tmp_path / "bin"
    f.write_bytes(b"\x7fELF" + b"\0" * 100)
    assert is_rust_binary(str(f)) is False
    f.write_bytes(b"\x7fELF" + b"\0" * 100 + b"__rust_alloc\0")
    assert is_rust_binary(str(f)) is True
    assert is_rust_binary(str(tmp_path / "missing")) is False
```

Also check the existing `test_attach_pid_rejected_for_non_go` in `tests/unit/test_cli_go.py`: it must keep passing for a Python program; if it uses a C-like fixture, update its docstring/assertion to the new message.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_cli_native_attach.py tests/unit/test_registry_rust.py -q --no-cov 2>&1 | tail -15`
Expected: SystemExit "applies only to Go debuggees", ImportError `is_rust_binary`, AttributeError `_pid_exe`.

- [ ] **Step 3: Add `is_rust_binary` and `detect_executable`**

`rust.py`:

```python
_RUST_MARKERS = (b"rust_eh_personality", b"__rust_alloc")
_RUST_SCAN_CHUNK = 4 * 1024 * 1024


def is_rust_binary(path: str) -> bool:
    """Best-effort: a non-stripped Rust executable carries its runtime's
    symbol names. Bounded chunked scan (markers may straddle chunks, so
    keep a small overlap). Stripped binaries return False -> treated as
    cpp, which gdb/lldb debug the same way."""
    tail = b""
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(_RUST_SCAN_CHUNK)
                if not chunk:
                    return False
                data = tail + chunk
                if any(m in data for m in _RUST_MARKERS):
                    return True
                tail = data[-32:]
    except OSError:
        return False
```

`registry.py` (after `detect`):

```python
def detect_executable(path: str) -> str:
    """Language of a running process's executable (`tdb -a PID` without
    --lang; `path` is /proc/PID/exe on Linux). Order matters: Go and
    OCaml binaries are ELF too, and a Rust binary is only distinguishable
    by its runtime symbols; anything else ELF is debugged as C/C++."""
    from tdb.languages.go import is_go_binary  # lazy: import cycles
    from tdb.languages.ocaml import ocaml_flavor
    from tdb.languages.rust import is_rust_binary

    try:
        with open(path, "rb") as f:
            magic = f.read(4)
    except OSError as exc:
        raise LanguageNotSupportedError(f"cannot read {path}: {exc}") from exc
    if is_go_binary(path):
        return "go"
    if ocaml_flavor(path) == "native":
        return "ocaml"
    if is_rust_binary(path):
        return "rust"
    if magic == b"\x7fELF":
        return "cpp"
    raise LanguageNotSupportedError(
        f"{path} is not a native executable tdb can attach to -- "
        "pass --lang to name the language"
    )
```

- [ ] **Step 4: Update cli.py**

Help text for `-a`:

```python
help = "Attach to a running local process by pid: Go (Delve) or a "
"native C/C++, Rust, or OCaml program (gdb or lldb-dap). Linux "
"identifies the language from /proc/PID/exe; elsewhere pass "
("--lang and, for native programs, the executable path.",)
```

Add near `_detect_lang`:

```python
PID_ATTACH_LANGUAGES = ("go", "cpp", "rust", "ocaml")


def _pid_exe(pid: int) -> str:
    """Path of a live process's executable (Linux /proc)."""
    return os.path.realpath(f"/proc/{pid}/exe")
```

Replace `_detect_lang`:

```python
def _detect_lang(args: argparse.Namespace) -> str:
    """registry.detect, plus pid-attach sniffing: with -a and no
    program/--lang, read the pid's executable (Linux /proc) and classify
    it (Go buildinfo, OCaml runtime, Rust runtime, other ELF -> cpp)."""
    from tdb.languages import registry
    from tdb.languages.base import LanguageNotSupportedError

    if args.program is None and args.attach_pid is not None:
        if sys.platform != "linux":
            raise LanguageNotSupportedError(
                f"cannot determine the language of pid {args.attach_pid} "
                "without /proc -- pass --lang (and the program path for "
                "C/C++, Rust, or OCaml)"
            )
        return registry.detect_executable(_pid_exe(args.attach_pid))
    return registry.detect(args.program)
```

In `_resolve_language`, pass the pid to non-Go builders:

```python
        else:
            profile = registry.resolve(
                lang_id,
                adapter=adapter,
                adapter_paths=config.adapters,
                program=args.program,
                attach_pid=args.attach_pid if lang_id in PID_ATTACH_LANGUAGES else None,
            )
```

Replace the `-a` rejection inside `if profile.id != "go":` (keep the `--test` check there) with a separate block:

```python
    if args.attach_pid is not None and profile.id not in PID_ATTACH_LANGUAGES:
        parser.error(
            f"-a/--attach applies to Go, C/C++, Rust, and OCaml debuggees "
            f"(detected language: {profile.id})"
        )
    if args.attach_pid is not None and profile.adapter.quirks.attach_requires_local_program:
        # gdb/lldb-dap load symbols from a local copy of the executable.
        if args.program is None:
            if sys.platform != "linux":
                parser.error(
                    f"{profile.display_name} pid attach needs the program path "
                    "on this platform (no /proc): tdb --lang "
                    f"{profile.id} -a {args.attach_pid} ./program"
                )
            args.program = _pid_exe(args.attach_pid)
        else:
            program_path = Path(args.program).resolve()
            if not program_path.is_file():
                parser.error(f"File not found: {args.program}")
            args.program = str(program_path)
```

The earlybird rejection raised by `build_ocaml_profile` is a `LanguageNotSupportedError`, already turned into `parser.error` by the surrounding `try`. Import `os` in cli.py if not already imported.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_cli_native_attach.py tests/unit/test_registry_rust.py tests/unit/test_cli_go.py tests/unit/test_cli_pause_on_attach.py tests/unit/test_cli.py -q --no-cov 2>&1 | tail -5`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/tdb/cli.py src/tdb/languages/registry.py src/tdb/languages/rust.py tests/unit/test_cli_native_attach.py tests/unit/test_registry_rust.py tests/unit/test_cli_go.py
git commit -m "CLI: -a PID for C/C++, Rust, OCaml with /proc program default and ELF sniffing"
```

---

### Task 5: The C/C++ hook header `tdb.h`, packaged with `--info` row

**Files:**
- Create: `src/tdb/adapters/native/__init__.py` (empty, one docstring line), `src/tdb/adapters/native/tdb.h`
- Modify: `pyproject.toml` (package-data line 64), `src/tdb/info.py` (~line 111 and the sections list ~line 130-160)
- Test: `tests/unit/test_native_hook_header.py` (new), `tests/unit/test_info.py` (append)

**Interfaces:**
- Produces: header API `void tdb_breakpoint(void);`, `void tdb_breakpoint_lang(const char *lang);`, `void tdb_breakpoint_stop(void);`; compile-time override macros `TDB_ATTACH_TIMEOUT_MS` (default 60000), `TDB_LINGER_TIMEOUT_MS` (default 3000), `TDB_POLL_MS` (default 50); `info.native_header_dir()`-style row `tdb.h dir` in a "C/C++" section.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_native_hook_header.py`:

```python
"""tdb.h: the C/C++ live breakpoint hook. Compiles as C99 and C++, links
from two translation units (weak symbols), is a no-op without a tty, and
gives up with a warning when nothing attaches."""

from __future__ import annotations

import importlib.resources
import os
import shutil
import subprocess
import sys

import pytest

HEADER = importlib.resources.files("tdb.adapters.native") / "tdb.h"
CC = shutil.which("gcc") or shutil.which("cc")
CXX = shutil.which("g++") or shutil.which("c++")

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or CC is None, reason="needs Linux and a C compiler"
)

TWO_TU_A = """\
#include "tdb.h"
void other(void);
int main(void) { tdb_breakpoint(); other(); return 0; }
"""
TWO_TU_B = """\
#include "tdb.h"
void other(void) { tdb_breakpoint(); }
"""


def _build(tmp_path, sources: dict[str, str], compiler, *extra) -> str:
    for name, text in sources.items():
        (tmp_path / name).write_text(text)
    exe = tmp_path / "prog"
    cmd = [
        compiler,
        "-g",
        "-O0",
        "-std=c99" if compiler == CC else "-std=c++17",
        "-I",
        str(HEADER.parent),
        *extra,
        "-o",
        str(exe),
        *(str(tmp_path / n) for n in sources),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return str(exe)


def test_header_links_from_two_translation_units_c(tmp_path):
    _build(
        tmp_path, {"a.c": TWO_TU_A, "b.c": TWO_TU_B}, CC, "-Wall", "-Wextra", "-Werror"
    )


@pytest.mark.skipif(CXX is None, reason="no C++ compiler")
def test_header_compiles_as_cxx(tmp_path):
    _build(
        tmp_path,
        {"a.cpp": TWO_TU_A, "b.cpp": TWO_TU_B},
        CXX,
        "-Wall",
        "-Wextra",
        "-Werror",
    )


def test_hook_is_noop_without_tty(tmp_path):
    exe = _build(tmp_path, {"a.c": TWO_TU_A, "b.c": TWO_TU_B}, CC)
    # stdin/stdout are pipes here, so the hook must return at once.
    r = subprocess.run(
        [exe],
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "TDB": "/nonexistent/tdb"},
    )
    assert r.returncode == 0
    assert r.stderr == ""


def test_header_hook_times_out_when_no_attach(tmp_path):
    """A tty but nothing ever attaches (as under ptrace_scope=3): warn and
    continue. TDB points at a fake that just sleeps; the attach timeout is
    shrunk at compile time."""
    import pty

    fake = tmp_path / "fake_tdb"
    fake.write_text("#!/bin/sh\nexec sleep 30\n")
    fake.chmod(0o755)
    exe = _build(
        tmp_path,
        {"a.c": TWO_TU_A, "b.c": TWO_TU_B},
        CC,
        "-DTDB_ATTACH_TIMEOUT_MS=300",
        "-DTDB_LINGER_TIMEOUT_MS=100",
    )
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [exe],
        stdin=slave,
        stdout=slave,
        stderr=subprocess.PIPE,
        env={**os.environ, "TDB": str(fake)},
    )
    os.close(slave)
    _, err = proc.communicate(timeout=20)
    os.close(master)
    assert proc.returncode == 0
    assert "did not attach" in err.decode()
    subprocess.run(["pkill", "-f", str(fake)], check=False)


def test_header_warns_when_tdb_missing(tmp_path):
    import pty

    exe = _build(tmp_path, {"a.c": TWO_TU_A, "b.c": TWO_TU_B}, CC)
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [exe],
        stdin=slave,
        stdout=slave,
        stderr=subprocess.PIPE,
        env={**os.environ, "TDB": str(tmp_path / "no-such-tdb")},
    )
    os.close(slave)
    _, err = proc.communicate(timeout=20)
    os.close(master)
    assert proc.returncode == 0
    assert "cannot" in err.decode() and "tdb" in err.decode()


def test_header_is_in_package_data():
    text = (
        importlib.resources.files("tdb").parent.parent / "pyproject.toml"
    ).read_text()
    assert "adapters/native/tdb.h" in text
```

Note on the last test: `files("tdb").parent.parent` is the repo root only in an editable install; if the suite runs from a wheel, replace it with `Path(__file__).resolve().parents[2] / "pyproject.toml"`. Use the latter form directly.

Append to `tests/unit/test_info.py`:

```python
def test_cpp_section_points_at_the_hook_header(stub_tools):
    text = info.info_text()
    body = _section(text, "C/C++")
    assert "tdb.h dir" in body
    assert body.split(":", 1)[1].strip().endswith(os.path.join("adapters", "native"))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_native_hook_header.py tests/unit/test_info.py -q --no-cov 2>&1 | tail -8`
Expected: ModuleNotFoundError `tdb.adapters.native`; info test fails on missing section.

- [ ] **Step 3: Write the header**

`src/tdb/adapters/native/__init__.py`:

```python
"""Packaged native hook sources: tdb.h (C/C++ live breakpoint hook)."""
```

`src/tdb/adapters/native/tdb.h`:

```c
/* tdb.h -- live breakpoint hook for C and C++ programs debugged with tdb.
 *
 *   #include "tdb.h"
 *   ...
 *   tdb_breakpoint();   // tdb opens here, paused on the next line
 *
 * Run the program directly, not under tdb. When the call is reached it
 * starts `tdb --lang cpp -a <pid> --no-pause-on-attach` on the program's
 * terminal (the `tdb` on PATH, or the one named by $TDB), waits for tdb's
 * gdb or lldb to attach, then stops in tdb_breakpoint_stop(); tdb steps
 * out to the line after the call. Later calls reuse the running tdb;
 * quitting tdb (Ctrl+q) detaches and the program runs on. The call is a
 * no-op when stdin or stdout is not a terminal, and it warns on stderr
 * and continues when tdb cannot be found, exits early, or fails to attach
 * within TDB_ATTACH_TIMEOUT_MS. Compile with -g -O0 to keep locals
 * inspectable.
 *
 * Linux only: attaching needs ptrace, granted to tdb's debugger with
 * PR_SET_PTRACER (Yama ptrace_scope=1). Elsewhere the hook warns once and
 * returns. Header-only; the functions are weak so several translation
 * units may include it. Compile-time knobs: TDB_ATTACH_TIMEOUT_MS (60000),
 * TDB_LINGER_TIMEOUT_MS (3000), TDB_POLL_MS (50).
 *
 * Find this header with `tdb --info` ("tdb.h dir") and pass that
 * directory with -I.
 */
#ifndef TDB_H
#define TDB_H

#ifndef TDB_ATTACH_TIMEOUT_MS
#define TDB_ATTACH_TIMEOUT_MS 60000
#endif
#ifndef TDB_LINGER_TIMEOUT_MS
#define TDB_LINGER_TIMEOUT_MS 3000
#endif
#ifndef TDB_POLL_MS
#define TDB_POLL_MS 50
#endif

#if defined(__GNUC__) || defined(__clang__)
#define TDB_HOOK_FN __attribute__((weak, noinline, used))
#else
#define TDB_HOOK_FN static
#endif

#ifdef __cplusplus
extern "C" {
#endif

void tdb_breakpoint(void);
void tdb_breakpoint_lang(const char *lang);
void tdb_breakpoint_stop(void);

/* The function tdb's hidden breakpoint lands in. Must stay a real,
 * externally visible, never-inlined symbol named tdb_breakpoint_stop. */
TDB_HOOK_FN void tdb_breakpoint_stop(void) {
#if defined(__GNUC__) || defined(__clang__)
    __asm__ volatile("" ::: "memory");
#endif
}

#if defined(__linux__)

#include <errno.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

extern char **environ;

static pid_t tdb__child = 0;         /* last tdb we spawned, 0 if none/reaped */
static int tdb__tracer_allowed = 0;

static void tdb__warn(const char *msg) {
    fprintf(stderr, "tdb_breakpoint: %s; continuing without a debugger\n", msg);
}

static void tdb__sleep_ms(long ms) {
    struct timespec ts;
    ts.tv_sec = ms / 1000;
    ts.tv_nsec = (ms % 1000) * 1000000L;
    nanosleep(&ts, NULL);
}

/* Pid of the tracer attached to THIS thread (gdb/lldb attach thread by
 * thread; /proc/self/status describes only the main thread). */
static int tdb__tracer_pid(void) {
    FILE *f = fopen("/proc/thread-self/status", "r");
    char line[256];
    int pid = 0;
    if (f == NULL) return 0;
    while (fgets(line, sizeof line, f) != NULL) {
        if (strncmp(line, "TracerPid:", 10) == 0) {
            pid = (int)strtol(line + 10, NULL, 10);
            break;
        }
    }
    fclose(f);
    return pid;
}

/* 1 if the last spawned tdb has exited (and reap it), else 0. */
static int tdb__child_exited(void) {
    int status;
    pid_t r;
    if (tdb__child == 0) return 1;
    r = waitpid(tdb__child, &status, WNOHANG);
    if (r == tdb__child || (r == -1 && errno == ECHILD)) {
        tdb__child = 0;
        return 1;
    }
    return 0;
}

static void tdb__wait_child_exit(long timeout_ms) {
    long waited = 0;
    while (!tdb__child_exited() && waited < timeout_ms) {
        tdb__sleep_ms(TDB_POLL_MS);
        waited += TDB_POLL_MS;
    }
}

static int tdb__spawn(const char *lang) {
    const char *name = getenv("TDB");
    char pidstr[32];
    char *argv[7];
    int rc;
    if (name == NULL || *name == '\0') name = "tdb";
    snprintf(pidstr, sizeof pidstr, "%d", (int)getpid());
    argv[0] = (char *)name;
    argv[1] = (char *)"--lang";
    argv[2] = (char *)lang;
    argv[3] = (char *)"-a";
    argv[4] = pidstr;
    argv[5] = (char *)"--no-pause-on-attach";
    argv[6] = NULL;
    /* posix_spawnp is async-signal-safe and thread-safe, unlike fork() in
     * a program with many threads; stdio is inherited (tdb draws on the
     * program's terminal). */
    rc = posix_spawnp(&tdb__child, name, NULL, NULL, argv, environ);
    if (rc != 0) {
        char msg[256];
        snprintf(msg, sizeof msg, "cannot start %s (%s); install tdb or point $TDB at it",
                 name, strerror(rc));
        tdb__warn(msg);
        tdb__child = 0;
        return 0;
    }
    return 1;
}

/* 1 when a debugger is attached to this thread and stopping is safe. */
static int tdb__ensure_debugger(const char *lang) {
    long waited = 0;
    if (!isatty(STDIN_FILENO) || !isatty(STDOUT_FILENO)) return 0;
    if (!tdb__tracer_allowed) {
        /* Let a non-ancestor (tdb's gdb/lldb-dap) attach under Yama. */
        prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY, 0, 0, 0);
        tdb__tracer_allowed = 1;
    }
    if (tdb__tracer_pid() != 0) return 1;
    /* A tdb spawned earlier may still be leaving the terminal. */
    tdb__wait_child_exit(TDB_LINGER_TIMEOUT_MS);
    if (!tdb__spawn(lang)) return 0;
    while (tdb__tracer_pid() == 0) {
        if (tdb__child_exited()) {
            tdb__warn("tdb exited before attaching");
            return 0;
        }
        if (waited >= TDB_ATTACH_TIMEOUT_MS) {
            tdb__warn("tdb did not attach in time (is ptrace allowed? "
                      "see /proc/sys/kernel/yama/ptrace_scope)");
            return 0;
        }
        tdb__sleep_ms(TDB_POLL_MS);
        waited += TDB_POLL_MS;
    }
    return 1;
}

TDB_HOOK_FN void tdb_breakpoint_lang(const char *lang) {
    if (tdb__ensure_debugger(lang)) {
        tdb_breakpoint_stop();
    }
}

#else /* not Linux */

#include <stdio.h>

TDB_HOOK_FN void tdb_breakpoint_lang(const char *lang) {
    static int warned = 0;
    (void)lang;
    if (!warned) {
        fprintf(stderr, "tdb_breakpoint: live attach is supported on Linux only; continuing\n");
        warned = 1;
    }
}

#endif /* __linux__ */

TDB_HOOK_FN void tdb_breakpoint(void) {
    tdb_breakpoint_lang("cpp");
}

#ifdef __cplusplus
}
#endif

#endif /* TDB_H */
```

Adjust while compiling with `-Wall -Wextra -Werror` under both `-std=c99` (needs `_POSIX_C_SOURCE`/`_GNU_SOURCE` for `posix_spawnp`, `nanosleep`, `environ`: add `#ifndef _GNU_SOURCE` / `#define _GNU_SOURCE` at the very top, before any include, and note in the header comment that it must be included before system headers or the program must define `_GNU_SOURCE` itself) and `-std=c++17` (C++ static helpers are fine; `static pid_t` in a header is per-TU, which is acceptable because the weak function that wins the link references its own TU's statics).

- [ ] **Step 4: Package it and add the info row**

`pyproject.toml` package-data: add `"adapters/native/tdb.h"` to the `tdb = [...]` list.

`info.py`: next to the perl/ruby dirs:

```python
    native_header_dir = str(importlib.resources.files("tdb.adapters.native"))
```

and a new section right after the `"lldb-dap"` tuple:

```python
(
    (
        "C/C++",
        # For `-I` when a program uses tdb.h (tdb_breakpoint()).
        [row("tdb.h dir", native_header_dir)],
    ),
)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_native_hook_header.py tests/unit/test_info.py -q --no-cov 2>&1 | tail -5`
Expected: all PASS. Also run `uv run tdb --info | sed -n '/^C\/C++/,/^$/p'` and confirm the row prints.

- [ ] **Step 6: Commit**

```bash
git add src/tdb/adapters/native pyproject.toml src/tdb/info.py tests/unit/test_native_hook_header.py tests/unit/test_info.py
git commit -m "Add tdb.h: C/C++ live breakpoint hook, packaged with an --info row"
```

---

### Task 6: The Rust hook crate `rust/tdb`

**Files:**
- Create: `rust/tdb/Cargo.toml`, `rust/tdb/src/lib.rs`, `rust/tdb/.gitignore` (`/target`)
- Test: `tests/unit/test_rust_hook_crate.py` (new)

**Interfaces:**
- Produces: crate `tdb` with `pub fn breakpoint()` and `#[no_mangle] pub extern "C" fn tdb_breakpoint_stop()`; buildable as `rustc --crate-type lib --edition 2021 rust/tdb/src/lib.rs`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_rust_hook_crate.py`:

```python
"""rust/tdb: the Rust live breakpoint hook crate. Builds with plain rustc
(no cargo in CI), exports the shared C stop symbol, and is a no-op without
a tty."""

from __future__ import annotations

import os
import pty
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CRATE = REPO / "rust" / "tdb" / "src" / "lib.rs"
RUSTC = shutil.which("rustc")

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or RUSTC is None, reason="needs Linux and rustc"
)

MAIN = """\
fn main() {
    let counter = 10;
    tdb::breakpoint();
    println!("counter = {}", counter + 1);
}
"""


def build_with_crate(tmp_path: Path, main_src: str) -> Path:
    rlib = tmp_path / "libtdb.rlib"
    subprocess.run(
        [
            RUSTC,
            "--edition",
            "2021",
            "--crate-type",
            "lib",
            "--crate-name",
            "tdb",
            "-g",
            "-C",
            "opt-level=0",
            "-o",
            str(rlib),
            str(CRATE),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    (tmp_path / "main.rs").write_text(main_src)
    exe = tmp_path / "prog"
    subprocess.run(
        [
            RUSTC,
            "--edition",
            "2021",
            "-g",
            "-C",
            "opt-level=0",
            "-L",
            str(tmp_path),
            "--extern",
            f"tdb={rlib}",
            "-o",
            str(exe),
            str(tmp_path / "main.rs"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return exe


def test_crate_builds_without_dependencies_and_exports_stop_symbol(tmp_path):
    exe = build_with_crate(tmp_path, MAIN)
    nm = subprocess.run(
        ["nm", str(exe)], capture_output=True, text=True, check=True
    ).stdout
    assert " T tdb_breakpoint_stop" in nm or " t tdb_breakpoint_stop" in nm


def test_rust_hook_noop_without_tty(tmp_path):
    exe = build_with_crate(tmp_path, MAIN)
    r = subprocess.run(
        [exe],
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "TDB": "/nonexistent/tdb"},
    )
    assert r.returncode == 0
    assert r.stdout.strip() == "counter = 11"
    assert r.stderr == ""


def test_rust_hook_warns_when_tdb_missing(tmp_path):
    exe = build_with_crate(tmp_path, MAIN)
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [exe],
        stdin=slave,
        stdout=slave,
        stderr=subprocess.PIPE,
        env={**os.environ, "TDB": str(tmp_path / "no-such-tdb")},
    )
    os.close(slave)
    _, err = proc.communicate(timeout=20)
    os.close(master)
    assert proc.returncode == 0
    assert "tdb::breakpoint" in err.decode() and "cannot" in err.decode()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_rust_hook_crate.py -q --no-cov 2>&1 | tail -5`
Expected: rustc error, `lib.rs` not found.

- [ ] **Step 3: Write the crate**

`rust/tdb/Cargo.toml`:

```toml
[package]
name = "tdb"
version = "0.1.0"
edition = "2021"
description = "Live breakpoint hook for the tdb debugger: tdb::breakpoint() opens tdb attached to the running program"
license = "MIT"
repository = "https://github.com/AlDanial/tdb"

[dependencies]
```

`rust/tdb/.gitignore`:

```
/target
/Cargo.lock
```

`rust/tdb/src/lib.rs`:

```rust
//! Live breakpoint hook for the [tdb](https://github.com/AlDanial/tdb)
//! debugger, the Rust counterpart of Python's `tdb.breakpoint()`.
//!
//! ```no_run
//! fn compute(n: u64) -> u64 {
//!     let total: u64 = (0..n).sum();
//!     tdb::breakpoint(); // tdb opens here, paused on the next line
//!     total
//! }
//! ```
//!
//! Run the program directly, not under tdb. When `breakpoint` is reached
//! it starts `tdb --lang rust -a <pid> --no-pause-on-attach` on the
//! program's terminal (the `tdb` on `PATH`, or the one named by `$TDB`),
//! waits for tdb's gdb or lldb to attach, and stops in
//! `tdb_breakpoint_stop`; tdb steps out to the line after the call. Later
//! calls reuse the running tdb. Quitting tdb (Ctrl+q) detaches and the
//! program runs on; the next call opens a fresh tdb.
//!
//! `breakpoint` is a no-op when stdin or stdout is not a terminal, and it
//! warns on stderr and continues when tdb cannot be found, exits before
//! attaching, or does not attach within 60 seconds. Build a debug profile
//! (`opt-level = 0`, `debug = true`) so locals stay inspectable.
//!
//! Linux only: attaching needs ptrace, granted to tdb's debugger with
//! `PR_SET_PTRACER` (Yama `ptrace_scope=1`). Elsewhere `breakpoint`
//! warns once and returns. The crate has no dependencies.

use std::sync::Mutex;

/// The function tdb's hidden breakpoint lands in. Shared, by name, with
/// the C/C++ header and the OCaml library; never inlined, never mangled.
#[no_mangle]
#[inline(never)]
pub extern "C" fn tdb_breakpoint_stop() {
    std::hint::black_box(());
}

/// Stop the program in tdb on the line following the call. See the crate
/// documentation for what happens when no tdb is attached yet.
#[inline(never)]
pub fn breakpoint() {
    if platform::ensure_debugger() {
        tdb_breakpoint_stop();
    }
}

fn warn(msg: &str) {
    eprintln!("tdb::breakpoint: {msg}; continuing without a debugger");
}

#[cfg(target_os = "linux")]
mod platform {
    use super::warn;
    use std::ffi::{c_int, c_ulong};
    use std::io::IsTerminal;
    use std::process::{Child, Command};
    use std::sync::Mutex;
    use std::time::{Duration, Instant};

    const ATTACH_TIMEOUT: Duration = Duration::from_secs(60);
    const LINGER_TIMEOUT: Duration = Duration::from_secs(3);
    const POLL: Duration = Duration::from_millis(50);
    const PR_SET_PTRACER: c_int = 0x5961_6d61; // "Yama"
    const PR_SET_PTRACER_ANY: c_ulong = c_ulong::MAX;

    extern "C" {
        // libc is always linked on Linux; declaring the one prototype we
        // need keeps the crate dependency-free.
        fn prctl(option: c_int, ...) -> c_int;
    }

    #[derive(Default)]
    struct State {
        child: Option<Child>,
        tracer_allowed: bool,
    }

    static STATE: Mutex<State> = Mutex::new(State { child: None, tracer_allowed: false });

    /// Pid of the tracer attached to this thread (gdb/lldb attach thread by
    /// thread), 0 if none.
    fn tracer_pid() -> u32 {
        let Ok(status) = std::fs::read_to_string("/proc/thread-self/status") else {
            return 0;
        };
        status
            .lines()
            .find_map(|l| l.strip_prefix("TracerPid:"))
            .and_then(|v| v.trim().parse().ok())
            .unwrap_or(0)
    }

    fn child_exited(state: &mut State) -> bool {
        match state.child.as_mut() {
            None => true,
            Some(c) => match c.try_wait() {
                Ok(Some(_)) | Err(_) => {
                    state.child = None;
                    true
                }
                Ok(None) => false,
            },
        }
    }

    fn wait_child_exit(state: &mut State, timeout: Duration) {
        let deadline = Instant::now() + timeout;
        while !child_exited(state) && Instant::now() < deadline {
            std::thread::sleep(POLL);
        }
    }

    fn spawn(state: &mut State) -> bool {
        let name = std::env::var_os("TDB")
            .filter(|v| !v.is_empty())
            .unwrap_or_else(|| "tdb".into());
        match Command::new(&name)
            .args(["--lang", "rust", "-a"])
            .arg(std::process::id().to_string())
            .arg("--no-pause-on-attach")
            .spawn()
        {
            Ok(child) => {
                state.child = Some(child);
                true
            }
            Err(e) => {
                warn(&format!(
                    "cannot start {} ({e}); install tdb or point $TDB at it",
                    name.to_string_lossy()
                ));
                false
            }
        }
    }

    pub(super) fn ensure_debugger() -> bool {
        if !std::io::stdin().is_terminal() || !std::io::stdout().is_terminal() {
            return false;
        }
        let mut state = STATE.lock().unwrap_or_else(|p| p.into_inner());
        if !state.tracer_allowed {
            // Let a non-ancestor (tdb's gdb/lldb-dap) attach under Yama.
            // SAFETY: prctl with these constants only sets a per-process flag.
            unsafe {
                prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY, 0 as c_ulong, 0 as c_ulong, 0 as c_ulong);
            }
            state.tracer_allowed = true;
        }
        if tracer_pid() != 0 {
            return true;
        }
        wait_child_exit(&mut state, LINGER_TIMEOUT);
        if !spawn(&mut state) {
            return false;
        }
        let deadline = Instant::now() + ATTACH_TIMEOUT;
        while tracer_pid() == 0 {
            if child_exited(&mut state) {
                warn("tdb exited before attaching");
                return false;
            }
            if Instant::now() >= deadline {
                warn("tdb did not attach in time (is ptrace allowed? see /proc/sys/kernel/yama/ptrace_scope)");
                return false;
            }
            std::thread::sleep(POLL);
        }
        true
    }
}

#[cfg(not(target_os = "linux"))]
mod platform {
    use super::warn;
    use std::sync::atomic::{AtomicBool, Ordering};

    static WARNED: AtomicBool = AtomicBool::new(false);

    pub(super) fn ensure_debugger() -> bool {
        if !WARNED.swap(true, Ordering::Relaxed) {
            warn("live attach is supported on Linux only");
        }
        false
    }
}

#[allow(dead_code)]
static _KEEP_MUTEX_IMPORT: Option<Mutex<()>> = None;
```

Remove the trailing `_KEEP_MUTEX_IMPORT` and the top-level `use std::sync::Mutex;` if rustc warns about an unused import (the Linux module has its own `use`). Build with `-D warnings` once to be sure the crate is warning-free. If the installed rustc predates `Mutex::new` in const context (1.63) or `IsTerminal` (1.70), note the floor in Cargo.toml `rust-version`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_rust_hook_crate.py -q --no-cov 2>&1 | tail -5`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add rust/tdb tests/unit/test_rust_hook_crate.py
git commit -m "Add rust/tdb: dependency-free Rust live breakpoint hook crate"
```

---

### Task 7: The OCaml hook library `ocaml/tdb`

**Files:**
- Create: `ocaml/tdb/tdb.ml`, `ocaml/tdb/tdb.mli`, `ocaml/tdb/tdb_stubs.c`, `ocaml/tdb/tdb.h` (byte-identical copy of the packaged header), `ocaml/tdb/dune`, `ocaml/tdb/dune-project`, `ocaml/tdb/tdb.opam`
- Test: `tests/unit/test_ocaml_hook_library.py` (new)

**Interfaces:**
- Produces: OCaml module `Tdb` with `val breakpoint : unit -> unit`; C stub symbol `tdb_ocaml_breakpoint`; test build recipe `ocamlopt -g -o prog tdb_stubs.c tdb.ml prog.ml` run inside a directory holding the three library files plus `tdb.h`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_ocaml_hook_library.py`:

```python
"""ocaml/tdb: the OCaml live breakpoint hook. Its tdb.h copy must match the
packaged header; the library builds with plain ocamlopt (no dune/ocamlfind
in CI) and is a no-op without a tty."""

from __future__ import annotations

import importlib.resources
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "ocaml" / "tdb"
PACKAGED_HEADER = importlib.resources.files("tdb.adapters.native") / "tdb.h"
OCAMLOPT = shutil.which("ocamlopt")

DEMO = """\
let () =
  let counter = ref 10 in
  Tdb.breakpoint ();
  counter := !counter + 1;
  Printf.printf "counter = %d\\n" !counter
"""


def test_header_copy_matches_packaged_header():
    assert (LIB / "tdb.h").read_bytes() == PACKAGED_HEADER.read_bytes(), (
        "ocaml/tdb/tdb.h must be byte-identical to src/tdb/adapters/native/tdb.h; "
        "copy the packaged header over it"
    )


def build_demo(tmp_path: Path, demo_src: str) -> Path:
    for name in ("tdb.ml", "tdb.mli", "tdb_stubs.c", "tdb.h"):
        shutil.copy(LIB / name, tmp_path / name)
    (tmp_path / "demo.ml").write_text(demo_src)
    exe = tmp_path / "demo"
    subprocess.run(
        [OCAMLOPT, "-g", "-o", str(exe), "tdb_stubs.c", "tdb.mli", "tdb.ml", "demo.ml"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    return exe


@pytest.mark.skipif(
    sys.platform != "linux" or OCAMLOPT is None, reason="needs Linux + ocamlopt"
)
def test_library_builds_with_plain_ocamlopt_and_is_noop_without_tty(tmp_path):
    exe = build_demo(tmp_path, DEMO)
    r = subprocess.run(
        [exe],
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "TDB": "/nonexistent/tdb"},
    )
    assert r.returncode == 0
    assert r.stdout.strip() == "counter = 11"
    assert r.stderr == ""
    nm = subprocess.run(
        ["nm", str(exe)], capture_output=True, text=True, check=True
    ).stdout
    assert "tdb_breakpoint_stop" in nm and "tdb_ocaml_breakpoint" in nm


@pytest.mark.skipif(
    sys.platform != "linux" or OCAMLOPT is None, reason="needs Linux + ocamlopt"
)
def test_ocaml_hook_warns_when_tdb_missing(tmp_path):
    import pty

    exe = build_demo(tmp_path, DEMO)
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [exe],
        stdin=slave,
        stdout=slave,
        stderr=subprocess.PIPE,
        env={**os.environ, "TDB": str(tmp_path / "no-such-tdb")},
    )
    os.close(slave)
    _, err = proc.communicate(timeout=20)
    os.close(master)
    assert proc.returncode == 0
    assert "cannot" in err.decode() and "tdb" in err.decode()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_ocaml_hook_library.py -q --no-cov 2>&1 | tail -5`
Expected: FileNotFoundError for `ocaml/tdb/tdb.h`.

- [ ] **Step 3: Write the library**

`ocaml/tdb/tdb.mli`:

```ocaml
(** Live breakpoint hook for the tdb debugger, the OCaml counterpart of
    Python's [tdb.breakpoint()].

    Run the program directly, not under tdb. When [breakpoint ()] is
    reached it starts [tdb --lang ocaml -a <pid> --no-pause-on-attach] on
    the program's terminal (the [tdb] on PATH, or the one named by [$TDB]),
    waits for tdb's lldb or gdb to attach, and stops; tdb steps out to the
    line after the call. Later calls reuse the running tdb. Quitting tdb
    (Ctrl+q) detaches and the program runs on. The call is a no-op when
    stdin or stdout is not a terminal, and it warns on stderr and continues
    when tdb cannot be found, exits early, or does not attach within 60 s.
    Native code only (ocamlopt, dune's dev profile keeps debug info); Linux
    only (ptrace via PR_SET_PTRACER). *)

val breakpoint : unit -> unit
```

`ocaml/tdb/tdb.ml`:

```ocaml
external tdb_ocaml_breakpoint : unit -> unit = "tdb_ocaml_breakpoint"

(* Not inlined: tdb recognizes the hook by its frames (camlTdb.breakpoint_*,
   caml_c_call, tdb_ocaml_breakpoint, tdb_breakpoint_lang,
   tdb_breakpoint_stop) and steps out of them to the caller. *)
let[@inline never] breakpoint () = tdb_ocaml_breakpoint ()
```

`ocaml/tdb/tdb_stubs.c`:

```c
/* C stub for Tdb.breakpoint: runs the shared tdb.h recipe with the
 * language id "ocaml". tdb.h here is a copy of tdb's packaged header
 * (src/tdb/adapters/native/tdb.h); a test keeps them identical. */
#define CAML_NAME_SPACE
#include <caml/mlvalues.h>
#include <caml/signals.h>

#include "tdb.h"

__attribute__((noinline)) value tdb_ocaml_breakpoint(value unit)
{
    (void)unit;
    /* Waiting for tdb to attach can take a while; do not hold the
     * runtime lock meanwhile (OCaml 5 domains keep running). The stop
     * itself freezes the whole process, lock or no lock. */
    caml_enter_blocking_section();
    tdb_breakpoint_lang("ocaml");
    caml_leave_blocking_section();
    return Val_unit;
}
```

`ocaml/tdb/tdb.h`: `cp src/tdb/adapters/native/tdb.h ocaml/tdb/tdb.h`.

`ocaml/tdb/dune`:

```
(library
 (name tdb)
 (public_name tdb)
 (wrapped false)
 (foreign_stubs
  (language c)
  (names tdb_stubs)))
```

`ocaml/tdb/dune-project`:

```
(lang dune 3.0)
(name tdb)
```

`ocaml/tdb/tdb.opam`:

```
opam-version: "2.0"
synopsis: "Live breakpoint hook for the tdb debugger (Tdb.breakpoint)"
maintainer: "Al Danial"
authors: "Al Danial"
license: "MIT"
homepage: "https://github.com/AlDanial/tdb"
bug-reports: "https://github.com/AlDanial/tdb/issues"
depends: [ "ocaml" {>= "4.12"} "dune" {>= "3.0"} ]
build: [ [ "dune" "build" "-p" name "-j" jobs ] ]
dev-repo: "git+https://github.com/AlDanial/tdb.git"
```

If `ocamlopt` warns about `[@inline never]` on this compiler, keep it (it is a standard attribute); if the C stub's `__attribute__` is rejected, guard it with `#if defined(__GNUC__)` as the header does. If `ocamlopt` refuses to compile `.c` files alongside `.ml` (it should accept them), fall back in the test to `ocamlopt -c tdb_stubs.c` first and then link `tdb_stubs.o`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_ocaml_hook_library.py -q --no-cov 2>&1 | tail -5`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add ocaml/tdb tests/unit/test_ocaml_hook_library.py
git commit -m "Add ocaml/tdb: OCaml live breakpoint hook library with C stub"
```

---

### Task 8: End-to-end integration test with real gdb and lldb-dap

**Files:**
- Create: `tests/integration/test_native_breakpoint_hook.py`
- Consumes: `tests/integration/hook_harness.HookDebuggee` (pty + fake tdb; `wait_spawn(n)`, `spawns()`, `fake_pid(n)`, `stderr_text()`, `finish()`, `close()`), `DebugController`, `ServerEventHandler`, profiles from Tasks 1-2, libraries from Tasks 5-7.

- [ ] **Step 1: Write the test**

```python
"""Native live breakpoint hooks (tdb.h, rust/tdb, ocaml/tdb) end to end.

Each demo calls its hook twice. The demo runs under a pty with a fake tdb
(hook_harness) so we can check the spawn argv; then this test plays tdb's
part for real: build the language profile with attach_pid, attach through
DebugController with the real gdb / lldb-dap, expect the hook stop, step
out until the frame is the user's, check the line and a local, detach, and
repeat for the second call. Linux only (TracerPid, PR_SET_PTRACER).
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from tdb.languages.cpp import build_cpp_profile
from tdb.languages.ocaml import build_ocaml_profile
from tdb.languages.rust import build_rust_profile
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController
from tests.integration.hook_harness import HookDebuggee

REPO = Path(__file__).resolve().parents[2]
HEADER_DIR = REPO / "src" / "tdb" / "adapters" / "native"
RUST_LIB = REPO / "rust" / "tdb" / "src" / "lib.rs"
OCAML_LIB = REPO / "ocaml" / "tdb"
WAIT = 30.0


def _gdb_supports_dap() -> bool:
    gdb = shutil.which("gdb")
    if gdb is None:
        return False
    out = subprocess.run([gdb, "--version"], capture_output=True, text=True).stdout
    m = re.search(r"(\d+)\.\d+", out)
    return bool(m) and int(m.group(1)) >= 14


ADAPTERS = [
    a
    for a, ok in (
        ("gdb", _gdb_supports_dap()),
        ("lldb-dap", shutil.which("lldb-dap") is not None),
    )
    if ok
]

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not ADAPTERS,
    reason="native hooks need Linux and gdb>=14 or lldb-dap",
)

# Each program: counter starts 10, hook, += 1, += 20, hook, += 300, print.
# (lang_id, expected local name, values after 1st/2nd stop, line after each hook)
C_SRC = """\
#include <stdio.h>
#include "tdb.h"
int main(void) {
    int counter = 10;
    tdb_breakpoint();
    counter += 1;
    counter += 20;
    tdb_breakpoint();
    counter += 300;
    printf("counter = %d\\n", counter);
    return 0;
}
"""
CPP_SRC = C_SRC.replace("#include <stdio.h>", "#include <cstdio>")
RUST_SRC = """\
fn main() {
    let mut counter = 10;
    tdb::breakpoint();
    counter += 1;
    counter += 20;
    tdb::breakpoint();
    counter += 300;
    println!("counter = {}", counter);
}
"""
OCAML_SRC = """\
let () =
  let counter = ref 10 in
  Tdb.breakpoint ();
  counter := !counter + 1;
  counter := !counter + 20;
  Tdb.breakpoint ();
  counter := !counter + 300;
  Printf.printf "counter = %d\\n" !counter
"""

CASES = {
    # lang: (lang_id, source, lines after hooks, local name, values, toolchain)
    "c": (
        "cpp",
        C_SRC,
        (6, 9),
        "counter",
        ("10", "31"),
        shutil.which("gcc") or shutil.which("cc"),
    ),
    "cpp": (
        "cpp",
        CPP_SRC,
        (6, 9),
        "counter",
        ("10", "31"),
        shutil.which("g++") or shutil.which("c++"),
    ),
    "rust": ("rust", RUST_SRC, (4, 7), "counter", ("10", "31"), shutil.which("rustc")),
    "ocaml": ("ocaml", OCAML_SRC, (4, 7), "counter", None, shutil.which("ocamlopt")),
}


def _build(lang: str, tmp_path: Path) -> Path:
    lang_id, src, _, _, _, tool = CASES[lang]
    if tool is None:
        pytest.skip(f"no toolchain for {lang}")
    exe = tmp_path / f"hooked_{lang}"
    if lang in ("c", "cpp"):
        ext = "c" if lang == "c" else "cpp"
        s = tmp_path / f"hooked.{ext}"
        s.write_text(src)
        subprocess.run(
            [tool, "-g", "-O0", "-I", str(HEADER_DIR), "-o", str(exe), str(s)],
            check=True,
            capture_output=True,
            text=True,
        )
    elif lang == "rust":
        rlib = tmp_path / "libtdb.rlib"
        subprocess.run(
            [
                tool,
                "--edition",
                "2021",
                "--crate-type",
                "lib",
                "--crate-name",
                "tdb",
                "-g",
                "-C",
                "opt-level=0",
                "-o",
                str(rlib),
                str(RUST_LIB),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        s = tmp_path / "hooked.rs"
        s.write_text(src)
        subprocess.run(
            [
                tool,
                "--edition",
                "2021",
                "-g",
                "-C",
                "opt-level=0",
                "-L",
                str(tmp_path),
                "--extern",
                f"tdb={rlib}",
                "-o",
                str(exe),
                str(s),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        for name in ("tdb.ml", "tdb.mli", "tdb_stubs.c", "tdb.h"):
            shutil.copy(OCAML_LIB / name, tmp_path / name)
        (tmp_path / "hooked.ml").write_text(src)
        subprocess.run(
            [
                tool,
                "-g",
                "-o",
                str(exe),
                "tdb_stubs.c",
                "tdb.mli",
                "tdb.ml",
                "hooked.ml",
            ],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
    return exe


def _profile(lang_id: str, adapter: str, pid: int):
    build = {
        "cpp": build_cpp_profile,
        "rust": build_rust_profile,
        "ocaml": build_ocaml_profile,
    }[lang_id]
    return build(adapter=adapter, attach_pid=pid)


async def _attach_and_land(
    profile, exe: Path, pid: int, want_line: int, local: str, value: str | None
):
    """Attach for real, ride the hook stop out to the caller, check, detach."""
    handler = ServerEventHandler()
    ctrl = DebugController(handler, profile=profile)
    await ctrl.remote_attach(
        host="127.0.0.1", port=0, program=str(exe), pre_arm_pause=False
    )
    await asyncio.wait_for(handler.initialized_event.wait(), WAIT)
    await ctrl.do_configure()
    await asyncio.wait_for(handler.stopped_event.wait(), WAIT)
    assert handler.last_stop_reason not in ("attach", "pause", "entry"), (
        handler.last_stop_reason
    )
    is_hook = profile.capabilities.breakpoint_hook_frame
    for _ in range(8):
        await ctrl.fetch_stop_info()
        top = ctrl.state.stack_frames[0]
        if not is_hook(top):
            break
        handler.stopped_event.clear()
        await ctrl.step_out()
        await asyncio.wait_for(handler.stopped_event.wait(), WAIT)
    else:
        pytest.fail(
            f"never left the hook frames: {[f.name for f in ctrl.state.stack_frames]}"
        )
    assert top.line == want_line, (top.name, top.line, top.source)
    assert Path(top.source.path).name.startswith("hooked"), top.source
    names = {v.name: v.value for vs in ctrl.state.variables.values() for v in vs}
    assert local in names, names
    if value is not None:
        assert value in names[local], names[local]
    await asyncio.wait_for(ctrl.stop(), WAIT)
    # Detached: alive and untraced.
    for _ in range(100):
        status = Path(f"/proc/{pid}/status").read_text()
        if re.search(r"TracerPid:\s+0\b", status):
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("debuggee still traced after detach")


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("lang", list(CASES))
async def test_hook_opens_tdb_twice_and_program_finishes(lang, adapter, tmp_path):
    lang_id, _, lines, local, values, _ = CASES[lang]
    exe = _build(lang, tmp_path)
    dbg = HookDebuggee(tmp_path, [str(exe)])
    try:
        argv = dbg.wait_spawn(1)
        assert argv == [
            "--lang",
            lang_id,
            "-a",
            str(dbg.proc.pid),
            "--no-pause-on-attach",
        ], argv
        os.kill(dbg.fake_pid(1), signal.SIGKILL)  # make room: we are tdb now
        vals = values or (None, None)
        await _attach_and_land(
            _profile(lang_id, adapter, dbg.proc.pid),
            exe,
            dbg.proc.pid,
            lines[0],
            local,
            vals[0],
        )
        # After detach the second hook call spawns a fresh tdb.
        argv2 = dbg.wait_spawn(2, timeout=WAIT)
        assert argv2 == argv
        os.kill(dbg.fake_pid(2), signal.SIGKILL)
        await _attach_and_land(
            _profile(lang_id, adapter, dbg.proc.pid),
            exe,
            dbg.proc.pid,
            lines[1],
            local,
            vals[1],
        )
        out, err = dbg.finish(timeout=WAIT)
        assert dbg.proc.returncode == 0, (out, err)
        assert "counter = 331" in out, out
        assert "did not attach" not in err and "exited before" not in err, err
    finally:
        dbg.close()
```

Notes for the implementer:
- The fake tdb is killed so its exit is what the hook's "tdb exited before attaching" check sees; the hook must still be polling TracerPid (it is: the child-exited check only fires when no tracer is present, and our real attach lands within the 60 s window). If the kill races the attach, move the `os.kill` after `_attach_and_land` starts (spawn the kill as a task after `remote_attach`).
- OCaml locals under lldb/gdb: `counter` is a `ref`; the value renders through the OCaml formatter, so `values` is `None` there and only the name is checked. If the OCaml local is not named `counter` in the DAP variables (it may appear as `counter_NNN`), relax to `any(n.startswith("counter") for n in names)`.
- `top.source.path` for Rust under gdb may be relative; compare `Path(...).name` only, as written.
- If the first stop after `do_configure` is reported before `stopped_event` is awaited (already set), the code above handles it because `do_configure` returns after the attach handling and the event stays set.

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/integration/test_native_breakpoint_hook.py -q --no-cov -x 2>&1 | tail -30`
Expected: PASS for every installed (lang, adapter) pair. Debug failures with the probe technique: gdb writes DAP traffic to stderr with `gdb -i dap -iex "set debug dap-log-file /tmp/dap.log"`; for the controller, set `TDB_LOG_LEVEL=DEBUG` if the project honors it (check `tdb/__init__.py` / `cli.py` for the log-file setup and read the log named by `tdb --info`).

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_native_breakpoint_hook.py
git commit -m "Integration test: native live breakpoint hooks via real gdb and lldb-dap"
```

---

### Task 9: Examples and documentation

**Files:**
- Create: `examples/C/breakpoint_hook_demo.c`, `examples/C++/breakpoint_hook_demo.cpp`, `examples/Rust/breakpoint_hook_demo/Cargo.toml`, `examples/Rust/breakpoint_hook_demo/src/main.rs`, `examples/Rust/breakpoint_hook_demo/.gitignore`, `examples/OCaml/breakpoint_hook_demo.ml`
- Modify: `README.md` (intro list ~line 59-60; "C/C++ tips" section ~line 380; "Rust" ~line 404; "OCaml" ~line 748; Go's `-a` paragraph ~line 876; "Live Breakpoint Hook" cross-reference ~line 1460; CLI Reference `-a` line ~2054-2099), `examples/README.md`

- [ ] **Step 1: Write the demos**

`examples/C/breakpoint_hook_demo.c`:

```c
/* Demo: drop into tdb at a specific line via tdb_breakpoint().
 *
 * Run it directly (not under tdb). Build unoptimized so locals stay
 * inspectable, then run the binary from a terminal:
 *
 *   gcc -g -O0 -I "$(tdb --info | awk -F': ' '/tdb.h dir/ {print $2}')" \
 *       -o demo breakpoint_hook_demo.c && ./demo
 */
#include <stdio.h>
#include "tdb.h"

static int compute(int n) {
    int total = 0;
    int local_list[5] = {1, 2, 3, 4, 5};
    for (int i = 0; i < n; i++) {
        total += i;
    }
    tdb_breakpoint(); /* tdb opens here; inspect total and local_list */
    return total + local_list[4];
}

int main(void) {
    int result = compute(10);
    printf("result = %d\n", result);
    return 0;
}
```

`examples/C++/breakpoint_hook_demo.cpp`:

```cpp
// Demo: drop into tdb at a specific line via tdb_breakpoint().
//
// Run it directly (not under tdb). Build unoptimized so locals stay
// inspectable, then run the binary from a terminal:
//
//   g++ -g -O0 -I "$(tdb --info | awk -F': ' '/tdb.h dir/ {print $2}')" \
//       -o demo breakpoint_hook_demo.cpp && ./demo
#include <iostream>
#include <vector>
#include "tdb.h"

static int compute(int n) {
    int total = 0;
    std::vector<int> local_list{1, 2, 3, 4, 5};
    for (int i = 0; i < n; i++) {
        total += i;
    }
    tdb_breakpoint(); // tdb opens here; inspect total and local_list
    return total + static_cast<int>(local_list.size());
}

int main() {
    int result = compute(10);
    std::cout << "result = " << result << "\n";
    return 0;
}
```

`examples/Rust/breakpoint_hook_demo/Cargo.toml`:

```toml
[package]
name = "breakpoint_hook_demo"
version = "0.1.0"
edition = "2021"

# Your own program would use a git dependency:
#   tdb = { git = "https://github.com/AlDanial/tdb" }
[dependencies]
tdb = { path = "../../../rust/tdb" }
```

`examples/Rust/breakpoint_hook_demo/.gitignore`: `/target` and `/Cargo.lock`.

`examples/Rust/breakpoint_hook_demo/src/main.rs`:

```rust
// Demo: drop into tdb at a specific line via tdb::breakpoint().
//
// Run it directly (not under tdb). The dev profile keeps locals
// inspectable:
//
//     cd examples/Rust/breakpoint_hook_demo
//     cargo run

fn compute(n: i32) -> i32 {
    let mut total = 0;
    let local_list = vec![1, 2, 3, 4, 5];
    for i in 0..n {
        total += i;
    }
    tdb::breakpoint(); // tdb opens here; inspect total and local_list
    total + local_list.len() as i32
}

fn main() {
    let result = compute(10);
    println!("result = {result}");
}
```

`examples/OCaml/breakpoint_hook_demo.ml`:

```ocaml
(* Demo: drop into tdb at a specific line via Tdb.breakpoint ().

   Run it directly (not under tdb). Without dune, build against the
   library sources in this repository (ocamlopt keeps debug info with -g):

     cp ../../ocaml/tdb/{tdb.ml,tdb.mli,tdb_stubs.c,tdb.h} .
     ocamlopt -g -o demo tdb_stubs.c tdb.mli tdb.ml breakpoint_hook_demo.ml && ./demo

   With dune, add `(libraries tdb)` to your executable stanza and vendor
   or `opam pin` the ocaml/tdb directory. *)

let compute n =
  let total = ref 0 in
  let local_list = [ 1; 2; 3; 4; 5 ] in
  for i = 0 to n - 1 do
    total := !total + i
  done;
  Tdb.breakpoint ();
  (* tdb opens here; inspect total and local_list *)
  !total + List.length local_list

let () =
  let result = compute 10 in
  Printf.printf "result = %d\n" result
```

Build each demo once by hand with the commands in its header comment to make sure they are right (do not commit build outputs).

- [ ] **Step 2: README updates**

1. Intro (line ~59-60): extend the parenthetical list to
   ``(`tdb.breakpoint()` in Python, `Devel::TdbRemote::breakpoint()` in Perl, `Tdb.breakpoint` in Ruby, `tdb.Breakpoint()` in Go, `tdb_breakpoint()` in C/C++, `tdb::breakpoint()` in Rust, `Tdb.breakpoint ()` in OCaml)``.
2. In "### C/C++ tips", add at the end:

```markdown
**Attach to a running process:** `tdb -a PID` attaches gdb (or `--adapter
lldb-dap`) to a live native process and stops it. On Linux tdb reads
`/proc/PID/exe` to identify the language (Go, OCaml, Rust, otherwise
C/C++) and to load symbols; pass the executable path as well to use a
different symbol file, and pass `--lang` off Linux. `--no-pause-on-attach`
leaves the process running, for a program about to stop itself.

**Live breakpoint hook:** the C/C++ counterpart of Python's
[`tdb.breakpoint()`](#live-breakpoint-hook). Include `tdb.h` (its directory
is the `tdb.h dir` row of `tdb --info`) and call `tdb_breakpoint()`:

```c
#include "tdb.h"

int compute(int n) {
    int total = 0;
    for (int i = 0; i < n; i++) total += i;
    tdb_breakpoint();   /* tdb opens here, paused on the next line */
    return total;
}
```

```bash
gcc -g -O0 -I "$(tdb --info | awk -F': ' '/tdb.h dir/ {print $2}')" -o prog prog.c
./prog    # run it directly, not under tdb
```

When the call is reached, the program starts `tdb --lang cpp -a <its pid>
--no-pause-on-attach` on its own terminal (the `tdb` on `PATH`, or the one
named by `$TDB`), waits for tdb's gdb or lldb to attach, and stops in the
hook; tdb steps out so the stop lands on the line after the call, in your
own frame. Later calls reuse the running tdb. Quitting tdb (`Ctrl+q`)
detaches and the program runs on; the next call opens a fresh tdb. The
call is a no-op when stdin and stdout are not a terminal, and it warns and
continues when `tdb` cannot be found, exits before attaching, or cannot
attach within 60 s (Yama `ptrace_scope` 2 or 3, or a container without
`SYS_PTRACE`, blocks attaching). Build with `-g -O0` so locals stay
inspectable; a stripped binary attaches but never stops, because the hook's
stop symbol cannot be resolved. The header is C99 and C++ compatible and
header-only. Linux only: the hook grants ptrace access to tdb's debugger
with `PR_SET_PTRACER`; elsewhere it warns once and returns. See
`examples/C/breakpoint_hook_demo.c` and `examples/C++/breakpoint_hook_demo.cpp`.
```

3. In "### Rust", after the remote-attach paragraph, add:

```markdown
**Attach to a running process:** `tdb --lang rust -a PID` attaches gdb (or
`--adapter lldb-dap`) to a live Rust process and stops it; tdb loads
symbols from `/proc/PID/exe`, or from an executable path you pass as well.
Without `--lang`, tdb recognizes a non-stripped Rust binary by its runtime
symbols; a stripped one is debugged as C/C++.

**Live breakpoint hook:** the Rust counterpart of Python's
[`tdb.breakpoint()`](#live-breakpoint-hook). Depend on the `tdb` crate from
this repository (it has no dependencies) and call `tdb::breakpoint()`:

```toml
[dependencies]
tdb = { git = "https://github.com/AlDanial/tdb" }   # or path = ".../rust/tdb"
```

```rust
fn compute(n: u64) -> u64 {
    let total: u64 = (0..n).sum();
    tdb::breakpoint(); // tdb opens here, paused on the next line
    total
}
```

```bash
cargo build && ./target/debug/prog    # dev profile; run it directly, not under tdb
```

When the call is reached, the program starts `tdb --lang rust -a <its pid>
--no-pause-on-attach` on its own terminal (the `tdb` on `PATH`, or the one
named by `$TDB`), waits for tdb's gdb or lldb to attach, and stops in the
hook; tdb steps out so the stop lands on the line after the call, in your
own frame. Later calls reuse the running tdb. Quitting tdb (`Ctrl+q`)
detaches and the program runs on; the next call opens a fresh tdb. The
call is a no-op when stdin and stdout are not a terminal, and it warns and
continues when `tdb` cannot be found, exits before attaching, or cannot
attach within 60 s (Yama `ptrace_scope` 2 or 3, or a container without
`SYS_PTRACE`, blocks attaching). Use the dev profile so locals stay
inspectable. Linux only: the hook grants ptrace access to tdb's debugger
with `PR_SET_PTRACER`; elsewhere it warns once and returns. See
`examples/Rust/breakpoint_hook_demo/`.
```

4. In "### OCaml", at the end of the section, add:

```markdown
**Attach to a running process:** `tdb -a PID` attaches lldb-dap (or
`--adapter gdb`) to a live native OCaml process and stops it; tdb
recognizes the OCaml runtime in `/proc/PID/exe` and always picks a native
adapter (bytecode programs cannot be attached to). `--no-pause-on-attach`
leaves the process running, for a program about to stop itself.

**Live breakpoint hook:** the OCaml counterpart of Python's
[`tdb.breakpoint()`](#live-breakpoint-hook), for native code. Vendor the
`ocaml/tdb` directory from this repository (or `opam pin` it), add
`(libraries tdb)` to your executable's dune stanza, and call
`Tdb.breakpoint ()`:

```ocaml
let compute n =
  let total = ref 0 in
  for i = 0 to n - 1 do total := !total + i done;
  Tdb.breakpoint ();   (* tdb opens here, paused on the next line *)
  !total
```

```bash
dune build && ./_build/default/prog.exe   # dev profile; run it directly, not under tdb
```

Without dune: copy `tdb.ml`, `tdb.mli`, `tdb_stubs.c`, and `tdb.h` from
`ocaml/tdb` next to your source and build with
`ocamlopt -g -o prog tdb_stubs.c tdb.mli tdb.ml prog.ml`.

When the call is reached, the program starts `tdb --lang ocaml -a <its pid>
--no-pause-on-attach` on its own terminal (the `tdb` on `PATH`, or the one
named by `$TDB`), waits for tdb's lldb or gdb to attach, and stops in the
hook; tdb steps out so the stop lands on the line after the call, in your
own frame. Later calls reuse the running tdb. Quitting tdb (`Ctrl+q`)
detaches and the program runs on; the next call opens a fresh tdb. The
call is a no-op when stdin and stdout are not a terminal, and it warns and
continues when `tdb` cannot be found, exits before attaching, or cannot
attach within 60 s (Yama `ptrace_scope` 2 or 3, or a container without
`SYS_PTRACE`, blocks attaching). Keep debug info (`-g`, dune's dev
profile). Linux only: the hook grants ptrace access to tdb's debugger with
`PR_SET_PTRACER`; elsewhere it warns once and returns. See
`examples/OCaml/breakpoint_hook_demo.ml`.
```

5. Go section (~line 876): change "`-a`/`--attach` currently supports Go only; on Linux, and only on Linux, `tdb` can identify the target as Go from `/proc/PID/exe`'s buildinfo" to "`-a`/`--attach` works for Go and, through gdb/lldb-dap, for C/C++, Rust, and OCaml; on Linux `tdb` identifies the language from `/proc/PID/exe`".
6. "### Live Breakpoint Hook" cross-reference (~line 1460): extend the sentence listing Perl, Ruby, and Go with C/C++, Rust, and OCaml and their links.
7. CLI Reference: update the `-a/--attach` line to match the new help text.

- [ ] **Step 3: examples/README.md**

Add a section "## Live breakpoint hook demos (C, C++, Rust, OCaml)" with the four build-and-run command blocks from the demo headers, and one sentence that each demo stops in tdb on the line after the hook call with `total` and `local_list` visible.

- [ ] **Step 4: Verify the docs build nothing wrong**

Run: `uv run pytest tests/unit -q --no-cov -k "readme or docs or examples" 2>&1 | tail -3` (some suites check README references; ensure they pass), then `git diff --stat`.

- [ ] **Step 5: Commit**

```bash
git add examples/C/breakpoint_hook_demo.c examples/C++/breakpoint_hook_demo.cpp examples/Rust/breakpoint_hook_demo examples/OCaml/breakpoint_hook_demo.ml README.md examples/README.md
git commit -m "Docs and demos for the C/C++, Rust, and OCaml live breakpoint hooks"
```

---

### Task 10: Manual TUI verification and full test run

**Files:** none modified unless a fix is needed.

- [ ] **Step 1: Real-TUI run, C demo, gdb**

Build `examples/C/breakpoint_hook_demo.c` per its header and run `./demo` in a real terminal (or with `uv run tdb` on PATH via `TDB=$(pwd)/.venv/bin/tdb`). Expect: tdb opens paused on `return total + local_list[4];` with `total = 45` and `local_list` in Variables; press `q` (quit) and confirm the program prints `result = 50` and exits 0.

- [ ] **Step 2: Real-TUI run, quit-between and continue-between**

Compile the integration test's `C_SRC` (two hooks) and run it: (a) `c` at the first stop lands on the second stop in the same tdb (counter 31), `q` finishes the program printing 331; (b) `q` at the first stop, then a fresh tdb opens at the second hook, `q` again, program prints 331.

- [ ] **Step 3: Real-TUI run, Rust and OCaml demos, lldb-dap**

Run `cargo run` in `examples/Rust/breakpoint_hook_demo` with `~/.config/tdb/config.json` `default_adapters` unset (gdb default) and once with `"rust": "lldb-dap"`. Run the OCaml demo (lldb-dap default). Expect the same landing behavior; note any frame-name that the predicates missed (the TUI would show a stop inside the hook) and fix the predicate in Task 2's files with a matching unit test.

- [ ] **Step 4: Full suite**

Run: `/home/al/bin/memcap-pytest` if present (the memory-capped wrapper from the OOM incident note), else `uv run pytest -q 2>&1 | tail -15`.
Expected: all pass or pre-existing unrelated flakes only (name them in the final report).

- [ ] **Step 5: Commit any fixes; leave the branch for the user to push**

```bash
git status --short
git log --oneline main..HEAD
```

Report: branch `native-breakpoint-hooks`, commits listed, tests run and their results, and the three manual runs' outcomes.

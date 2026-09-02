# Go Language Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Go debugging to tdb via Delve's DAP server (`dlv dap`), including a goroutine inspection workspace (states, stacks, wait graph, findings) mirroring the Rust concurrency pattern.

**Architecture:** A new `LanguageProfile` (`src/tdb/languages/go.py`) fronts `dlv dap` through a new `spawn_tcp` adapter connect mode (Delve serves DAP over TCP, not stdio). A `src/tdb/go_concurrency/` package collects goroutines (each is a DAP thread under Delve), classifies their park state from stack shape, and builds a bipartite wait graph. A `GoroutinesModal` (three tabs, like `RustConcurrencyModal`) presents the snapshot; the plain `ThreadsModal` remains the fallback.

**Tech Stack:** Python 3.11+, textual, asyncio, Delve (`dlv`) >= 1.21, Go toolchain (integration tests only).

**Spec:** `docs/superpowers/specs/2026-09-01-go-support-design.md`

## Global Constraints

- Work on branch `add-go-support` (already created off `main`; the spec is committed there).
- Use `uv pip install`, never bare `pip install`. Run tests with `pytest` from the repo root (`work/`).
- Profiles never import controller/app/widgets and hold no runtime state (`languages/base.py` rule). Capability values are data/callables, consumers gate with `is not None`/truthiness.
- Goroutine collection is bounded: default cap 150 goroutines, and the modal must report "N more not collected" — no silent truncation.
- The analyzer must never claim a mutex holder (Go mutexes don't record owners).
- `--terminal` is rejected for Go in v1. `task_inspection` stays `False` for Go. `child_process_strategy` stays `None`.
- Cross-platform: no POSIX-only assumptions outside clearly-marked fallbacks (`/proc` reads must be guarded, as `InspectService._processes_from_pids` does).
- Every new user-visible limitation lands in README's Go section (Task 12).
- Commit after every task (at minimum); commit messages in the repo's existing style (`git log --oneline` shows it).

---

### Task 1: `spawn_tcp` adapter connect mode

`dlv dap` speaks DAP only over TCP: it prints `DAP server listening at: 127.0.0.1:<port>` on stdout and accepts exactly one connection. Teach `DAPClient.start()` to spawn-then-connect, driven by two new `AdapterSpec` class attributes.

**Files:**
- Modify: `src/tdb/languages/base.py` (class `AdapterSpec`, ~line 73)
- Modify: `src/tdb/dap/client.py` (`DAPClient.start()`, ~line 73)
- Modify: `src/tdb/_timeouts.py` (add one constant)
- Test: `tests/unit/test_dap_spawn_tcp.py`

**Interfaces:**
- Consumes: existing `DAPClient.start()/connect()/stop()`, `AdapterSpec`.
- Produces: `AdapterSpec.connect_mode: str = "stdio"`; `AdapterSpec.listen_regex: re.Pattern[str] | None = None` (groups 1=host, 2=port); `tdb._timeouts.ADAPTER_LISTEN: float = 15.0`. Task 3's `DelveAdapter` sets `connect_mode = "spawn_tcp"` and a `listen_regex`.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/unit/test_dap_spawn_tcp.py"""
import re
import sys
import textwrap

import pytest

from tdb.dap.client import DAPClient
from tdb.languages.base import AdapterSpec

# Serves one TCP connection and speaks just enough DAP to answer an
# `initialize` request, so the client's read loop has a real stream.
_SERVER_SCRIPT = textwrap.dedent(
    """
    import socket, sys
    srv = socket.create_server(("127.0.0.1", 0))
    print(f"DAP server listening at: 127.0.0.1:{srv.getsockname()[1]}", flush=True)
    conn, _ = srv.accept()
    conn.recv(65536)  # swallow the initialize request
    body = b'{"seq":1,"type":"response","request_seq":1,"command":"initialize","success":true,"body":{}}'
    conn.sendall(b"Content-Length: %d\\r\\n\\r\\n%s" % (len(body), body))
    conn.recv(65536)  # hold the connection until the client closes
    """
)


class _FakeTcpAdapter(AdapterSpec):
    id = "fake-tcp"
    connect_mode = "spawn_tcp"
    listen_regex = re.compile(r"DAP server listening at: (\S+):(\d+)")

    def __init__(self, script: str) -> None:
        self._script = script

    def command(self) -> list[str]:
        return [sys.executable, "-c", self._script]


@pytest.mark.asyncio
async def test_spawn_tcp_connects_and_talks():
    client = DAPClient(_FakeTcpAdapter(_SERVER_SCRIPT))
    await client.start()
    try:
        caps = await client.initialize()
        assert caps is not None
        assert client._writer is not None  # TCP stream, not stdin
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_spawn_tcp_times_out_without_listen_line(monkeypatch):
    monkeypatch.setattr("tdb._timeouts.ADAPTER_LISTEN", 0.5)
    silent = "import time; time.sleep(30)"
    client = DAPClient(_FakeTcpAdapter(silent))
    with pytest.raises(ConnectionError):
        await client.start()
    await client.stop()


@pytest.mark.asyncio
async def test_spawn_tcp_surfaces_stderr_on_early_death():
    dying = "import sys; sys.stderr.write('bad flag'); sys.exit(3)"
    client = DAPClient(_FakeTcpAdapter(dying))
    with pytest.raises(ConnectionError) as exc:
        await client.start()
    assert "bad flag" in str(exc.value)
    await client.stop()


def test_default_connect_mode_is_stdio():
    assert AdapterSpec.connect_mode == "stdio"
    assert AdapterSpec.listen_regex is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_dap_spawn_tcp.py -v`
Expected: FAIL — `AdapterSpec` has no attribute `connect_mode`.

- [ ] **Step 3: Implement**

In `src/tdb/languages/base.py`, inside `class AdapterSpec` right after `quirks`:

```python
    # How the DAP byte stream is established after spawning `command()`:
    #   "stdio"      — (default) DAP over the subprocess's stdin/stdout.
    #   "spawn_tcp"  — the subprocess serves DAP on a TCP socket and
    #                  announces it on stdout with a line matching
    #                  `listen_regex` (group 1 host, group 2 port);
    #                  the client connects and speaks DAP over TCP.
    #                  Used by adapters with no stdio mode (dlv dap).
    connect_mode: str = "stdio"
    listen_regex: "re.Pattern[str] | None" = None
```

(add `import re` at the top of `base.py`).

In `src/tdb/_timeouts.py`, next to `DAP_REQUEST`:

```python
# Ceiling for a spawn_tcp adapter to print its "listening at" line.
ADAPTER_LISTEN = 15.0
```

In `src/tdb/dap/client.py`, replace the tail of `start()` (the two lines
`self._reader = self._process.stdout` / `self._reader_task = ...` — keep the
`_watch_task` creation last):

```python
        if self._adapter.connect_mode == "spawn_tcp":
            host, port = await self._await_listen_line()
            self._reader, self._writer = await asyncio.open_connection(host, port)
            # Keep draining the adapter's stdout so its pipe never fills
            # and blocks it (dlv logs there). Strong ref like _watch_task.
            self._stdout_drain_task = asyncio.create_task(self._drain_stdout())
        else:
            self._reader = self._process.stdout
        self._reader_task = asyncio.create_task(self._read_loop())
        self._watch_task = asyncio.create_task(self._watch_adapter_death())
```

Add the field `self._stdout_drain_task: asyncio.Task[None] | None = None` in `__init__`, cancel it in `stop()` exactly the way `_watch_task` is cancelled, and add:

```python
    async def _drain_stdout(self) -> None:
        """Discard a spawn_tcp adapter's post-handshake stdout (log
        noise) so the pipe can't fill and stall the adapter."""
        assert self._process is not None and self._process.stdout is not None
        try:
            while await self._process.stdout.read(65536):
                pass
        except Exception:
            pass
```

And add the helper (module needs no new imports — `asyncio` is imported):

```python
    async def _await_listen_line(self) -> tuple[str, int]:
        """Read the spawn_tcp adapter's stdout until its listen
        announcement, with a hard deadline. Raises ConnectionError with
        the adapter's stderr when it dies or stays silent."""
        from tdb import _timeouts

        assert self._process is not None and self._process.stdout is not None
        pattern = self._adapter.listen_regex
        assert pattern is not None, "spawn_tcp adapter must define listen_regex"

        async def _read_until_match() -> tuple[str, int]:
            assert self._process is not None and self._process.stdout is not None
            while True:
                raw = await self._process.stdout.readline()
                if not raw:  # EOF: adapter exited before announcing
                    stderr = b""
                    if self._process.stderr is not None:
                        stderr = await self._process.stderr.read()
                    raise ConnectionError(
                        f"{self._adapter.id} adapter exited before serving DAP: "
                        f"{stderr.decode('utf-8', errors='replace').strip() or 'no output'}"
                    )
                m = pattern.search(raw.decode("utf-8", errors="replace"))
                if m:
                    return m.group(1), int(m.group(2))

        try:
            return await asyncio.wait_for(
                _read_until_match(), timeout=_timeouts.ADAPTER_LISTEN
            )
        except asyncio.TimeoutError:
            raise ConnectionError(
                f"{self._adapter.id} adapter did not announce its DAP port "
                f"within {_timeouts.ADAPTER_LISTEN}s"
            ) from None
```

Note `from tdb import _timeouts` + attribute access (not `from ... import ADAPTER_LISTEN`) so the test's `monkeypatch.setattr` works.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_dap_spawn_tcp.py tests/unit/test_dap_client.py -v`
Expected: all PASS (existing stdio tests must not regress).

- [ ] **Step 5: Commit**

```bash
git add src/tdb/languages/base.py src/tdb/dap/client.py src/tdb/_timeouts.py tests/unit/test_dap_spawn_tcp.py
git commit -m "Add spawn_tcp adapter connect mode for socket-only DAP servers"
```

---

### Task 2: Go target detection (buildinfo sniff + package directories)

**Files:**
- Create: `src/tdb/languages/go.py` (detection half only; adapter comes in Task 3)
- Modify: `src/tdb/languages/registry.py` (`detect()`, `_MAGIC` loop, ~lines 108-150)
- Test: `tests/unit/test_go_detection.py`

**Interfaces:**
- Produces: `tdb.languages.go.is_go_binary(program: str) -> bool` (also used by Task 3's mode inference and Task 5's pid sniff).
- Registry behavior: executables containing Go buildinfo detect as `"go"` (not `"cpp"`); directories containing `*.go` files detect as `"go"`.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/unit/test_go_detection.py"""
import pytest

from tdb.languages import registry
from tdb.languages.base import LanguageNotSupportedError
from tdb.languages.go import is_go_binary

# 16-byte magic Go embeds in every built binary (read by `go version`).
GO_BUILDINFO_MAGIC = b"\xff Go buildinf:"


def _fake_binary(tmp_path, name, payload):
    p = tmp_path / name
    p.write_bytes(b"\x7fELF" + b"\x00" * 60 + payload)
    return str(p)


def test_go_binary_sniff_positive(tmp_path):
    prog = _fake_binary(tmp_path, "gohello", b"junk" + GO_BUILDINFO_MAGIC + b"more")
    assert is_go_binary(prog)
    assert registry.detect(prog) == "go"


def test_non_go_elf_still_detects_cpp(tmp_path):
    prog = _fake_binary(tmp_path, "chello", b"no go marker here")
    assert not is_go_binary(prog)
    assert registry.detect(prog) == "cpp"


def test_marker_straddling_chunk_boundary(tmp_path):
    from tdb.languages import go
    payload = b"A" * (go._CHUNK - 70) + GO_BUILDINFO_MAGIC
    prog = _fake_binary(tmp_path, "straddle", payload)
    assert is_go_binary(prog)


def test_marker_beyond_scan_limit_is_missed(tmp_path):
    from tdb.languages import go
    payload = b"A" * (go._SCAN_LIMIT + 10) + GO_BUILDINFO_MAGIC
    prog = _fake_binary(tmp_path, "huge", payload)
    assert not is_go_binary(prog)  # bounded scan, documented limitation


def test_is_go_binary_missing_file_is_false(tmp_path):
    assert not is_go_binary(str(tmp_path / "nope"))


def test_directory_with_go_files_detects_go(tmp_path):
    (tmp_path / "main.go").write_text("package main\n")
    assert registry.detect(str(tmp_path)) == "go"


def test_directory_without_go_files_errors(tmp_path):
    (tmp_path / "readme.txt").write_text("hi")
    with pytest.raises(LanguageNotSupportedError):
        registry.detect(str(tmp_path))


def test_go_source_extension_still_maps(tmp_path):
    src = tmp_path / "main.go"
    src.write_text("package main\n")
    assert registry.detect(str(src)) == "go"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_go_detection.py -v`
Expected: FAIL — `tdb.languages.go` does not exist. (`test_go_source_extension_still_maps` fails too: `.go` maps to `"go"` but `resolve` isn't involved in `detect`, so it PASSES — that's fine, it's a regression guard.)

- [ ] **Step 3: Implement**

Create `src/tdb/languages/go.py`:

```python
"""The Go language profile (Delve DAP).

This module is built up across Tasks 2-4: detection helpers here,
DelveAdapter + build_go_profile in Task 3, error parsing and thread
classification in Task 4.
"""

from __future__ import annotations

# Magic prefix of the build-info blob Go links into every binary
# (what `go version <binary>` locates). 16 bytes, version-stable.
_BUILDINFO_MAGIC = b"\xff Go buildinf:"
_CHUNK = 1024 * 1024  # scan in 1MB chunks
_SCAN_LIMIT = 16 * _CHUNK  # bounded: huge non-Go binaries stay cheap
_OVERLAP = len(_BUILDINFO_MAGIC) - 1


def is_go_binary(program: str) -> bool:
    """True when `program` is an executable with embedded Go buildinfo.

    Bounded scan of the first 16MB (the blob sits in an early data
    section in practice). Best-effort: unreadable files and misses
    beyond the limit return False — `--lang go` overrides (README).
    """
    scanned = 0
    tail = b""
    try:
        with open(program, "rb") as f:
            while scanned < _SCAN_LIMIT:
                chunk = f.read(_CHUNK)
                if not chunk:
                    break
                if _BUILDINFO_MAGIC in tail + chunk[:_OVERLAP] or (
                    _BUILDINFO_MAGIC in chunk
                ):
                    return True
                tail = chunk[-_OVERLAP:]
                scanned += len(chunk)
    except OSError:
        pass
    return False
```

In `src/tdb/languages/registry.py` `detect()`: after `path = Path(program)` and **before** `reject_compiled_source(program)`, add the directory case:

```python
    if path.is_dir():
        if any(path.glob("*.go")):
            return "go"
        raise LanguageNotSupportedError(
            f"{program!r} is a directory with no .go files — tdb debugs "
            f"a program file, or a Go package directory"
        )
```

And change the magic loop so Go binaries beat the `cpp` fallback:

```python
    for magic, lang_id in _MAGIC:
        if head.startswith(magic):
            from tdb.languages.go import is_go_binary  # lazy: import cycle

            if is_go_binary(str(path)):
                return "go"
            return lang_id
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_go_detection.py tests/unit/test_registry*.py tests/unit/test_cpp_profile.py -v`
(if no `test_registry*.py` exists, run `pytest tests/unit -k "registry or detect" -v`)
Expected: PASS, no detection regressions.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/languages/go.py src/tdb/languages/registry.py tests/unit/test_go_detection.py
git commit -m "Detect Go targets: buildinfo sniff for binaries, package directories"
```

---

### Task 3: DelveAdapter and the Go profile

**Files:**
- Modify: `src/tdb/languages/go.py` (append adapter + builder)
- Modify: `src/tdb/languages/registry.py` (register at end, after rust)
- Test: `tests/unit/test_go_profile.py`

**Interfaces:**
- Consumes: `is_go_binary` (Task 2), `connect_mode`/`listen_regex` (Task 1).
- Produces: `DelveAdapter(executable=None, mode=None, attach_pid=None)` with `id="dlv"`; `build_go_profile(adapter=None, adapter_paths=None, program=None, *, test=False, attach_pid=None) -> LanguageProfile`. Launch body keys: `{"type": "go", "request": "launch", "mode": "debug"|"exec"|"test", "program", "args", "cwd", "stopOnEntry", ["env"]}`. Attach bodies: `{"mode": "local", "processId": pid, "stopOnEntry": True}` or `{"mode": "remote"}`.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/unit/test_go_profile.py"""
import shutil

import pytest

from tdb.languages import registry
from tdb.languages.base import AdapterNotFoundError, LanguageNotSupportedError
from tdb.languages.go import DelveAdapter, build_go_profile


def test_profile_shape():
    p = build_go_profile()
    assert p.id == "go"
    assert p.display_name == "Go"
    assert p.adapter.id == "dlv"
    assert p.adapter.connect_mode == "spawn_tcp"
    assert p.adapter.listen_regex is not None
    assert p.presentation.lexer == "go"
    assert p.capabilities.task_inspection is False
    assert p.capabilities.child_process_strategy is None
    assert p.capabilities.pause_while_running is True
    assert p.capabilities.concurrency_inspection == "go"
    assert p.capabilities.compute_step_units is None


def test_registered_in_registry():
    assert "go" in registry.known_languages()
    assert registry.resolve("go").id == "go"


def test_command_and_adapter_paths_override():
    assert DelveAdapter(executable="/opt/dlv").command() == [
        "/opt/dlv", "dap", "--listen=127.0.0.1:0",
    ]
    p = build_go_profile(adapter_paths={"dlv": "/opt/dlv"})
    assert p.adapter.command()[0] == "/opt/dlv"


def test_command_missing_dlv_hints_install(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(AdapterNotFoundError) as exc:
        DelveAdapter().command()
    assert "go install github.com/go-delve/delve/cmd/dlv@latest" in exc.value.hint


def test_listen_regex_matches_dlv_output():
    m = DelveAdapter.listen_regex.search("DAP server listening at: 127.0.0.1:38697\n")
    assert m is not None
    assert (m.group(1), m.group(2)) == ("127.0.0.1", "38697")


def _body(adapter, program, console="internalConsole"):
    return adapter.launch_body(
        program=program, args=["-n"], cwd="/w", env={"A": "1"},
        stop_on_entry=True, console=console, opts={},
    )


def test_launch_mode_debug_for_source(tmp_path):
    src = tmp_path / "main.go"
    src.write_text("package main\n")
    body = _body(DelveAdapter(), str(src))
    assert body["mode"] == "debug"
    assert body == {
        "type": "go", "request": "launch", "mode": "debug",
        "program": str(src), "args": ["-n"], "cwd": "/w",
        "stopOnEntry": True, "env": {"A": "1"},
    }


def test_launch_mode_exec_for_go_binary(tmp_path):
    binary = tmp_path / "prog"
    binary.write_bytes(b"\x7fELF" + b"\x00" * 60 + b"\xff Go buildinf:xxxx")
    assert _body(DelveAdapter(), str(binary))["mode"] == "exec"


def test_launch_mode_test_via_builder(tmp_path):
    p = build_go_profile(program=str(tmp_path), test=True)
    assert _body(p.adapter, str(tmp_path))["mode"] == "test"


def test_terminal_rejected():
    with pytest.raises(LanguageNotSupportedError):
        _body(DelveAdapter(), "x.go", console="externalTerminal")


def test_attach_bodies():
    local = build_go_profile(attach_pid=1234).adapter
    assert local.attach_body(host="127.0.0.1", port=0, opts={}) == {
        "mode": "local", "processId": 1234, "stopOnEntry": True,
    }
    assert local.quirks.attach_via_adapter is True
    remote = build_go_profile().adapter
    assert remote.attach_body(host="h", port=9, opts={}) == {"mode": "remote"}
    assert remote.quirks.attach_via_adapter is False


def test_unknown_adapter_rejected():
    with pytest.raises(LanguageNotSupportedError):
        build_go_profile(adapter="gdb")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_go_profile.py -v`
Expected: FAIL — no `DelveAdapter`.

- [ ] **Step 3: Implement**

Append to `src/tdb/languages/go.py`:

```python
import re
import shutil
from typing import Any

from tdb.languages.base import (
    AdapterNotFoundError,
    AdapterQuirks,
    AdapterSpec,
    LanguageNotSupportedError,
    LanguageProfile,
    Presentation,
    ProfileCapabilities,
)


class DelveAdapter(AdapterSpec):
    """Delve's native DAP server (`dlv dap`).

    Socket-only: dlv prints its listen line and serves one TCP
    connection (spawn_tcp mode, Task 1). One instance covers all four
    Delve modes — launch mode is constructor data or inferred from the
    program; a local-attach pid flips the attach quirk so the
    controller spawns dlv for attach too.
    """

    id = "dlv"
    connect_mode = "spawn_tcp"
    listen_regex = re.compile(r"DAP server listening at: (\S+):(\d+)")

    def __init__(
        self,
        executable: str | None = None,
        mode: str | None = None,
        attach_pid: int | None = None,
    ) -> None:
        self._executable = executable
        self._mode = mode  # "debug" | "exec" | "test" | None -> infer
        self._attach_pid = attach_pid
        if attach_pid is not None:
            # Local pid attach spawns dlv locally (like launch) and
            # sends the attach request through it.
            self.quirks = AdapterQuirks(attach_via_adapter=True)

    def command(self) -> list[str]:
        exe = self._executable or shutil.which("dlv")
        if exe is None:
            raise AdapterNotFoundError(
                "dlv (Delve) not found on PATH — "
                "`go install github.com/go-delve/delve/cmd/dlv@latest`, "
                'or set {"adapters": {"dlv": "/path/to/dlv"}} in '
                "tdb's config.json"
            )
        return [exe, "dap", "--listen=127.0.0.1:0"]

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
                "--terminal is not supported for Go yet (dlv dap does "
                "not route the debuggee to a caller-provided terminal)"
            )
        mode = self._mode or ("exec" if is_go_binary(program) else "debug")
        body: dict[str, Any] = {
            "type": "go",
            "request": "launch",
            "mode": mode,
            "program": program,
            "args": args,
            "cwd": cwd,
            "stopOnEntry": stop_on_entry,
        }
        if env:
            body["env"] = env
        return body

    def attach_body(
        self, *, host: str, port: int, opts: dict[str, Any]
    ) -> dict[str, Any]:
        if self._attach_pid is not None:
            # Stop on attach so the user gets control immediately —
            # the same UX debugpy's pre-armed pause gives Python attach.
            return {
                "mode": "local",
                "processId": self._attach_pid,
                "stopOnEntry": True,
            }
        # -r host:port — tdb connected straight to a user-run
        # `dlv dap --listen`; the attach request selects remote mode.
        return {"mode": "remote"}


def build_go_profile(
    adapter: str | None = None,
    adapter_paths: dict[str, str] | None = None,
    program: str | None = None,
    *,
    test: bool = False,
    attach_pid: int | None = None,
) -> LanguageProfile:
    adapter_id = adapter or "dlv"
    if adapter_id != "dlv":
        raise LanguageNotSupportedError(
            f"unknown adapter {adapter_id!r} for go (known: dlv)"
        )
    executable = (adapter_paths or {}).get("dlv")
    return LanguageProfile(
        id="go",
        display_name="Go",
        adapter=DelveAdapter(
            executable=executable,
            mode="test" if test else None,
            attach_pid=attach_pid,
        ),
        presentation=Presentation(lexer="go"),
        capabilities=ProfileCapabilities(
            pause_while_running=True,  # dlv dap honors DAP `pause` -> --run works
            concurrency_inspection="go",
        ),
    )
```

(Keep all imports at the top of the file: `re`, `shutil`, `typing.Any`, and the `tdb.languages.base` block go directly below the existing module docstring and `from __future__ import annotations`, above the detection constants from Task 2.)

At the end of `src/tdb/languages/registry.py`, after the rust registration:

```python
from tdb.languages.go import build_go_profile  # noqa: E402

register("go", build_go_profile)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_go_profile.py tests/unit/test_go_detection.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/languages/go.py src/tdb/languages/registry.py tests/unit/test_go_profile.py
git commit -m "Add Go language profile backed by Delve DAP (dlv dap, spawn_tcp)"
```

---

### Task 4: Go panic parsing and goroutine thread classification

**Files:**
- Modify: `src/tdb/languages/errors.py` (append `parse_go_error`)
- Modify: `src/tdb/languages/go.py` (add `classify_go_threads`; wire both into `build_go_profile`)
- Test: `tests/unit/test_go_errors.py`, extend `tests/unit/test_go_profile.py`

**Interfaces:**
- Produces: `parse_go_error(stderr: str, exit_code: int | None = None) -> ParsedError | None`; `classify_go_threads(threads: list[Thread], stacks: dict[int, list[StackFrame]]) -> list[ThreadDecoration]`.
- Profile wiring: `Presentation(parse_error=parse_go_error, frame_placeholder="<main>")`, `ProfileCapabilities(classify_threads=classify_go_threads, ...)`.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/unit/test_go_errors.py"""
from tdb.languages.errors import parse_go_error

PANIC = """\
some program output
panic: runtime error: integer divide by zero

goroutine 1 [running]:
main.divide(...)
\t/w/main.go:7 +0x11
main.main()
\t/w/main.go:12 +0x1d
exit status 2
"""


def test_parse_go_panic():
    err = parse_go_error(PANIC, 2)
    assert err is not None
    assert err.header == "panic: runtime error: integer divide by zero"
    assert err.message == "runtime error: integer divide by zero"
    # ParsedError.frames are OUTERMOST-first; Go prints innermost-first.
    assert [(f.func, f.path, f.line) for f in err.frames] == [
        ("main.main", "/w/main.go", 12),
        ("main.divide", "/w/main.go", 7),
    ]
    assert "goroutine 1 [running]:" in err.detail


def test_parse_only_first_goroutine_block():
    text = PANIC.replace(
        "exit status 2",
        "goroutine 18 [chan receive]:\nmain.worker()\n\t/w/main.go:3 +0x1\nexit status 2",
    )
    err = parse_go_error(text, 2)
    assert all(f.func != "main.worker" for f in err.frames)


def test_no_panic_returns_none():
    assert parse_go_error("all fine\n", 0) is None
    assert parse_go_error("", None) is None


def test_goexit_frames_skipped():
    text = PANIC.replace(
        "exit status 2",
        "runtime.goexit()\n\t/usr/local/go/src/runtime/asm_amd64.s:1650 +0x1\nexit status 2",
    )
    err = parse_go_error(text, 2)
    assert all("runtime." not in f.func for f in err.frames)
```

And append to `tests/unit/test_go_profile.py`:

```python
from tdb.dap.types import Source, StackFrame, Thread
from tdb.languages.go import classify_go_threads


def _frame(name):
    return StackFrame(id=1, name=name, source=Source(path="/w/main.go"), line=1)


def test_classify_hides_pure_runtime_goroutines():
    threads = [
        Thread(id=1, name="* [Go 1] main.main"),
        Thread(id=2, name="[Go 17] runtime.gcBgMarkWorker"),
        Thread(id=3, name="[Go 5] main.worker"),
    ]
    stacks = {
        1: [_frame("main.main")],
        2: [_frame("runtime.gopark"), _frame("runtime.gcBgMarkWorker")],
        3: [_frame("runtime.gopark"), _frame("runtime.chanrecv"), _frame("main.worker")],
    }
    d = classify_go_threads(threads, stacks)
    assert [x.hidden for x in d] == [False, True, False]
    assert all(x.label is None for x in d)  # dlv's names are already good


def test_classify_without_stack_stays_visible():
    threads = [Thread(id=9, name="[Go 9] main.helper")]
    d = classify_go_threads(threads, {})
    assert d[0].hidden is False


def test_profile_wires_error_parser_and_classifier():
    p = build_go_profile()
    assert p.presentation.parse_error is not None
    assert p.capabilities.classify_threads is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_go_errors.py tests/unit/test_go_profile.py -v`
Expected: FAIL — no `parse_go_error` / `classify_go_threads`.

- [ ] **Step 3: Implement**

Append to `src/tdb/languages/errors.py`:

```python
_GO_PANIC_HEADER_RE = re.compile(r"^panic: (.+)$", re.MULTILINE)
_GO_GOROUTINE_HEADER_RE = re.compile(r"^goroutine \d+ \[[^\]]+\]:$", re.MULTILINE)
# A frame is a `pkg.func(args...)` line followed by a tab-indented
# `\t/path/file.go:NN +0xOFF` location line.
_GO_FRAME_RE = re.compile(r"^(\S.*?)\(.*\)?$")
_GO_LOC_RE = re.compile(r"^\t(\S+?):(\d+)(?: \+0x[0-9a-f]+)?\s*$")


def parse_go_error(stderr: str, exit_code: int | None = None) -> ParsedError | None:
    """Parse a Go panic out of raw stderr text.

    `exit_code` is accepted for Presentation.parse_error signature
    parity and ignored: the `panic:` header is an unambiguous signal
    (Go always exits 2 on unrecovered panic, but a program printing
    "panic:" itself and exiting 0 would be pathological either way).

    Frames come from the FIRST goroutine block only — Go prints the
    panicking goroutine first; other goroutines' dumps (GOTRACEBACK=all)
    are preserved in `detail` but not turned into synthetic frames.
    Runtime-internal frames (runtime.goexit etc.) are skipped.
    """
    header_match = _GO_PANIC_HEADER_RE.search(stderr)
    if header_match is None:
        return None
    detail_text = stderr[header_match.start() :].rstrip()

    goroutine_match = _GO_GOROUTINE_HEADER_RE.search(detail_text)
    frames: list[ErrorFrame] = []
    if goroutine_match is not None:
        lines = detail_text[goroutine_match.end() :].lstrip("\n").split("\n")
        i = 0
        while i + 1 < len(lines):
            func_line, loc_line = lines[i], lines[i + 1]
            if not func_line.strip() or _GO_GOROUTINE_HEADER_RE.match(func_line):
                break  # end of the first goroutine's block
            func_m = _GO_FRAME_RE.match(func_line)
            loc_m = _GO_LOC_RE.match(loc_line)
            if func_m and loc_m:
                func = func_m.group(1).split("(")[0]
                if not func.startswith("runtime."):
                    frames.append(
                        ErrorFrame(
                            path=loc_m.group(1),
                            line=int(loc_m.group(2)),
                            func=func,
                        )
                    )
                i += 2
            else:
                i += 1
    # Go prints innermost-first; ParsedError wants outermost-first.
    frames.reverse()

    message = header_match.group(1).strip()
    return ParsedError(
        header=f"panic: {message}",
        message=message,
        frames=frames,
        detail=detail_text,
    )
```

Append to `src/tdb/languages/go.py` (imports: add `from tdb.dap.types import StackFrame, Thread` and `from tdb.languages.base import ThreadDecoration`, `from tdb.languages.errors import parse_go_error`):

```python
def classify_go_threads(
    threads: list[Thread], stacks: dict[int, list[StackFrame]]
) -> list[ThreadDecoration]:
    """Hide goroutines whose entire stack is Go-runtime internals (GC
    workers, finalizers, netpoll) so the plain ThreadsModal shows user
    goroutines by default; `a` reveals everything. Labels stay None —
    dlv's own thread names ("[Go N] pkg.func") are already right.
    A goroutine with no stack info stays visible."""
    decorations: list[ThreadDecoration] = []
    for t in threads:
        frames = stacks.get(t.id, [])
        hidden = bool(frames) and all(
            f.name.startswith(("runtime.", "runtime/")) for f in frames
        )
        decorations.append(ThreadDecoration(t, None, hidden))
    return decorations
```

And update `build_go_profile`'s return value:

```python
        presentation=Presentation(
            lexer="go",
            parse_error=parse_go_error,
            frame_placeholder="<main>",
        ),
        capabilities=ProfileCapabilities(
            pause_while_running=True,
            concurrency_inspection="go",
            classify_threads=classify_go_threads,
        ),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_go_errors.py tests/unit/test_go_profile.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/languages/errors.py src/tdb/languages/go.py tests/unit/test_go_errors.py tests/unit/test_go_profile.py
git commit -m "Parse Go panics into the error modal; classify runtime goroutines"
```

---

### Task 5: CLI — `--test`, `-a/--attach`, mode plumbing, allowlists

**Files:**
- Modify: `src/tdb/cli.py` (`build_parser` flag block ~lines 30-245; `_resolve_language` ~line 389; `_parse_attach_spec`; program-required check ~line 380; TdbApp construction ~line 776)
- Test: `tests/unit/test_cli_go.py`

**Interfaces:**
- Consumes: `build_go_profile(..., test=, attach_pid=)` (Task 3), `is_go_binary` (Task 2).
- Produces: `args.test: bool`, `args.attach_pid: int | None`; Go accepted by `--remote-attach`; `--terminal` rejected for adapter id `dlv`; pid attach reaches the app as `attach_host="127.0.0.1", attach_port=0` with the pid inside the profile's adapter (zero `app.py` changes — `remote_attach` + `attach_via_adapter` + `spawn_tcp` compose).

- [ ] **Step 1: Write the failing tests**

```python
"""tests/unit/test_cli_go.py"""
import pytest

from tdb.cli import parse_args

GO_BUILDINFO_MAGIC = b"\xff Go buildinf:"


@pytest.fixture
def go_src(tmp_path):
    src = tmp_path / "main.go"
    src.write_text("package main\nfunc main() {}\n")
    return str(src)


@pytest.fixture
def go_pkg(tmp_path):
    (tmp_path / "main.go").write_text("package main\nfunc main() {}\n")
    return str(tmp_path)


def test_go_source_resolves_go_profile(go_src):
    args = parse_args([go_src])
    assert args.profile.id == "go"
    assert args.profile.adapter.id == "dlv"


def test_test_flag_selects_test_mode(go_pkg):
    args = parse_args(["--test", go_pkg])
    body = args.profile.adapter.launch_body(
        program=go_pkg, args=[], cwd=".", env=None,
        stop_on_entry=True, console="internalConsole", opts={},
    )
    assert body["mode"] == "test"


def test_test_flag_rejected_for_non_go(tmp_path):
    py = tmp_path / "x.py"
    py.write_text("print(1)\n")
    with pytest.raises(SystemExit):
        parse_args(["--test", str(py)])


def test_attach_pid_builds_local_attach_profile(tmp_path, monkeypatch):
    monkeypatch.setattr("tdb.languages.go.is_go_binary", lambda p: True)
    args = parse_args(["--lang", "go", "-a", "4242"])
    assert args.attach_pid == 4242
    assert args.attach_host == "127.0.0.1"
    assert args.attach_port == 0
    body = args.profile.adapter.attach_body(host="127.0.0.1", port=0, opts={})
    assert body == {"mode": "local", "processId": 4242, "stopOnEntry": True}


def test_attach_pid_rejected_for_non_go(tmp_path):
    py = tmp_path / "x.py"
    py.write_text("print(1)\n")
    with pytest.raises(SystemExit):
        parse_args(["-a", "4242", str(py)])


def test_attach_pid_conflicts_with_remote_attach():
    with pytest.raises(SystemExit):
        parse_args(["--lang", "go", "-a", "1", "-r", "5678"])


def test_remote_attach_allows_go():
    args = parse_args(["--lang", "go", "-r", "localhost:5678"])
    assert args.profile.id == "go"


def test_terminal_rejected_for_go(go_src):
    with pytest.raises(SystemExit):
        parse_args(["--terminal", "xterm", go_src])


def test_run_allowed_for_go(go_src):
    assert parse_args(["--run", go_src]).profile.id == "go"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_cli_go.py -v`
Expected: FAIL — unknown flags `--test` / `-a`.

- [ ] **Step 3: Implement**

In `build_parser()` (put `--test` near `--run`, `-a` next to `-r/--remote-attach`):

```python
    parser.add_argument(
        "--test",
        action="store_true",
        help="Debug a Go test package (Delve 'test' mode; Go only). "
        "Arguments after -- go to the test binary, e.g. -- -run TestFoo",
    )
    parser.add_argument(
        "-a",
        "--attach",
        dest="attach_pid",
        type=int,
        metavar="PID",
        help="Attach to a running local process by pid (Go only)",
    )
```

In `parse_args()`, next to the `--run` conflict table, add pid-attach conflicts:

```python
    if args.attach_pid is not None:
        for flag, value in (
            ("-r/--remote-attach", args.remote_attach),
            ("--test", args.test),
            ("--terminal", args.terminal),
            ("--run", args.run),
        ):
            if value:
                parser.error(f"-a/--attach cannot be combined with {flag}")
```

Relax the program-required check (~line 380):

```python
    if not args.program and not args.remote_attach and args.attach_pid is None:
        parser.error("either a program, --remote-attach, or -a/--attach is required")
```

In `_parse_attach_spec` (after the `-r` parsing), route pid attach through the existing remote-attach app path — host/port are dummies the Delve adapter ignores because it carries the pid:

```python
    if args.attach_pid is not None:
        args.attach_host, args.attach_port = "127.0.0.1", 0
```

(If `attach_host`/`attach_port` are only set when `args.remote_attach` is truthy, make sure they default to `None` first, as today.)

In `_resolve_language`, replace the single `registry.resolve(...)` call region:

```python
    try:
        registry.reject_compiled_source(args.program)
        lang_id = args.lang or _detect_lang(args)
        adapter = args.adapter or config.default_adapters.get(lang_id)
        if lang_id == "go" and (args.test or args.attach_pid is not None):
            from tdb.languages.go import build_go_profile

            profile = build_go_profile(
                adapter=adapter,
                adapter_paths=config.adapters,
                program=args.program,
                test=args.test,
                attach_pid=args.attach_pid,
            )
        else:
            profile = registry.resolve(
                lang_id,
                adapter=adapter,
                adapter_paths=config.adapters,
                program=args.program,
            )
    except LanguageNotSupportedError as e:
        parser.error(str(e))
    args.profile = profile
```

with the new detection helper just above `_resolve_language`:

```python
def _detect_lang(args: argparse.Namespace) -> str:
    """registry.detect, plus pid-attach language sniffing: with -a and
    no program/--lang, read the pid's executable (Linux /proc) and
    check for Go buildinfo; elsewhere --lang is required."""
    from tdb.languages import registry
    from tdb.languages.base import LanguageNotSupportedError

    if args.program is None and args.attach_pid is not None:
        from tdb.languages.go import is_go_binary

        exe = f"/proc/{args.attach_pid}/exe"
        if is_go_binary(exe):
            return "go"
        raise LanguageNotSupportedError(
            f"cannot determine the language of pid {args.attach_pid} — "
            "pass --lang (pid attach currently supports Go only)"
        )
    return registry.detect(args.program)
```

Still in `_resolve_language`, extend the Go-only-flag rejections next to the `--python` block:

```python
    if profile.id != "go":
        if args.test:
            parser.error(
                f"--test applies only to Go debuggees (detected language: {profile.id})"
            )
        if args.attach_pid is not None:
            parser.error(
                f"-a/--attach applies only to Go debuggees "
                f"(detected language: {profile.id})"
            )
```

Update the remote-attach allowlist (~line 429):

```python
        if profile.id not in ("python", "perl", "ruby", "rust", "cpp", "go"):
            parser.error(
                f"--remote-attach supports Python, Perl, Ruby, Rust, C/C++, "
                f"and Go debuggees only (detected language: {profile.id})"
            )
```

Add the `--terminal` guard next to the gdb/ocamlearlybird ones:

```python
    if args.terminal and profile.adapter.id == "dlv":
        parser.error(
            "--terminal is not supported for Go yet (dlv dap does not "
            "route the debuggee to a caller-provided terminal)"
        )
```

Finally check the two `TdbApp(...)` construction sites (`cli.py:776`, `cli.py:850`): they already pass `attach_host=`/`attach_port=` from args; confirm those read the attributes set above (adjust names to whatever `_parse_attach_spec` produces — keep them identical to the `-r` path so no `app.py` change is needed).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_cli_go.py tests/unit/test_cli.py tests/unit/test_cli_run_flag.py -v`
Expected: PASS, no CLI regressions.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/cli.py tests/unit/test_cli_go.py
git commit -m "CLI: Go mode inference, --test, -a/--attach pid, allowlist updates"
```

---

### Task 6: `go_concurrency` models and stack classifier

**Files:**
- Create: `src/tdb/go_concurrency/__init__.py` (empty)
- Create: `src/tdb/go_concurrency/models.py`
- Create: `src/tdb/go_concurrency/classifier.py`
- Test: `tests/unit/test_go_classifier.py`

**Interfaces:**
- Produces (models): `GoroutineState`, `Confidence`, `GoFindingKind` enums; frozen dataclasses `GoroutineInfo(thread_id, goid, function, state, operation, resource_id, frames, is_runtime)`, `GoResource(resource_id, kind, label)`, `GoWaitEdge(thread_id, resource_id, operation)`, `GoFinding(kind, thread_ids, summary, confidence)`, `GoroutineSnapshot(goroutines, resources, edges, findings, uncollected, warnings)` — all with `to_dict()`.
- Produces (classifier): `Classification(state, park_frame_index, operation, target_expr)`; `classify_stack(frame_names: list[str]) -> Classification`.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/unit/test_go_classifier.py"""
from tdb.go_concurrency.classifier import classify_stack
from tdb.go_concurrency.models import GoroutineState

CHAN_RECV = [
    "runtime.gopark",
    "runtime.chanrecv",
    "runtime.chanrecv1",
    "main.worker",
    "runtime.goexit",
]
CHAN_SEND = ["runtime.gopark", "runtime.chansend", "runtime.chansend1", "main.feed"]
SELECT = ["runtime.gopark", "runtime.selectgo", "main.mux"]
MUTEX = [
    "runtime.gopark",
    "runtime.goparkunlock",
    "runtime.semacquire1",
    "sync.runtime_SemacquireMutex",
    "sync.(*Mutex).lockSlow",
    "sync.(*Mutex).Lock",
    "main.critical",
]
WAITGROUP = [
    "runtime.gopark",
    "runtime.semacquire1",
    "sync.runtime_Semacquire",
    "sync.(*WaitGroup).Wait",
    "main.main",
]
SLEEP = ["runtime.gopark", "time.Sleep", "main.napper"]
SYSCALL = ["syscall.Syscall6", "internal/poll.(*FD).Read", "os.(*File).Read", "main.reader"]
RUNNING = ["main.crunch", "main.main"]
GC = ["runtime.gopark", "runtime.gcBgMarkWorker"]


def _c(frames):
    return classify_stack(frames)


def test_chan_recv():
    c = _c(CHAN_RECV)
    assert c.state is GoroutineState.CHAN_RECV
    assert c.operation == "recv"
    assert CHAN_RECV[c.park_frame_index] == "runtime.chanrecv"
    assert c.target_expr == "c"


def test_chan_send():
    c = _c(CHAN_SEND)
    assert c.state is GoroutineState.CHAN_SEND
    assert c.operation == "send"
    assert c.target_expr == "c"


def test_select_has_state_but_no_target():
    c = _c(SELECT)
    assert c.state is GoroutineState.SELECT
    assert c.target_expr is None  # scases enumeration deferred (spec)


def test_mutex():
    c = _c(MUTEX)
    assert c.state is GoroutineState.MUTEX_WAIT
    assert MUTEX[c.park_frame_index] == "sync.runtime_SemacquireMutex"
    assert c.target_expr == "addr"


def test_waitgroup():
    c = _c(WAITGROUP)
    assert c.state is GoroutineState.WAITGROUP_WAIT
    assert c.target_expr == "addr"


def test_sleep_syscall_running_runtime():
    assert _c(SLEEP).state is GoroutineState.SLEEP
    assert _c(SYSCALL).state is GoroutineState.SYSCALL
    assert _c(RUNNING).state is GoroutineState.RUNNING
    assert _c(GC).state is GoroutineState.RUNTIME


def test_empty_stack_is_unknown():
    assert _c([]).state is GoroutineState.UNKNOWN


def test_snapshot_to_dict_roundtrips():
    from tdb.go_concurrency.models import (
        Confidence, GoFinding, GoFindingKind, GoResource, GoWaitEdge,
        GoroutineInfo, GoroutineSnapshot,
    )
    snap = GoroutineSnapshot(
        goroutines=(
            GoroutineInfo(
                thread_id=3, goid=5, function="main.worker",
                state=GoroutineState.CHAN_RECV, operation="recv",
                resource_id="chan:0xc000024180",
                frames=("runtime.gopark", "main.worker"), is_runtime=False,
            ),
        ),
        resources=(GoResource("chan:0xc000024180", "channel", "chan 0xc000024180"),),
        edges=(GoWaitEdge(3, "chan:0xc000024180", "recv"),),
        findings=(
            GoFinding(
                GoFindingKind.STUCK_CHANNEL, (3,),
                "1 goroutine receiving on chan 0xc000024180 with no sender",
                Confidence.PROBABLE,
            ),
        ),
        uncollected=0,
        warnings=(),
    )
    d = snap.to_dict()
    assert d["goroutines"][0]["state"] == "chan_recv"
    assert d["findings"][0]["kind"] == "stuck_channel"
    assert d["findings"][0]["confidence"] == "probable"
    assert d["uncollected"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_go_classifier.py -v`
Expected: FAIL — no `tdb.go_concurrency`.

- [ ] **Step 3: Implement**

`src/tdb/go_concurrency/__init__.py`: empty file.

`src/tdb/go_concurrency/models.py`:

```python
"""Immutable goroutine observations and wait-graph results.

Mirrors rust_concurrency/models.py's discipline: frozen dataclasses,
str-enums, to_dict() for the RPC/MCP JSON surface. Deliberately no
owner/holder fields anywhere — Go mutexes don't record owners and the
analyzer must never pretend otherwise (spec).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Confidence(str, Enum):
    CONFIRMED = "confirmed"
    PROBABLE = "probable"


class GoroutineState(str, Enum):
    # RUNNING covers running-or-runnable: with the whole process
    # stopped, DAP can't distinguish the two.
    RUNNING = "running"
    CHAN_SEND = "chan_send"
    CHAN_RECV = "chan_recv"
    SELECT = "select"
    MUTEX_WAIT = "mutex_wait"
    WAITGROUP_WAIT = "waitgroup_wait"
    SLEEP = "sleep"
    SYSCALL = "syscall"
    RUNTIME = "runtime"
    UNKNOWN = "unknown"


class GoFindingKind(str, Enum):
    STUCK_CHANNEL = "stuck_channel"
    MUTEX_CONVOY = "mutex_convoy"
    LIKELY_LEAK = "likely_leak"


@dataclass(frozen=True)
class GoroutineInfo:
    thread_id: int  # DAP thread id (Delve: one thread per goroutine)
    goid: int | None  # parsed from "[Go N] ..." (None if unparseable)
    function: str  # display function from dlv's thread name
    state: GoroutineState
    operation: str | None  # "recv" | "send" | "mutex" | "waitgroup" | None
    resource_id: str | None  # wait-graph key, e.g. "chan:0xc000024180"
    frames: tuple[str, ...]  # frame names, innermost first (display only)
    is_runtime: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "goid": self.goid,
            "function": self.function,
            "state": self.state.value,
            "operation": self.operation,
            "resource_id": self.resource_id,
            "frames": list(self.frames),
            "is_runtime": self.is_runtime,
        }


@dataclass(frozen=True)
class GoResource:
    resource_id: str
    kind: str  # "channel" | "semaphore"
    label: str

    def to_dict(self) -> dict[str, str]:
        return {"resource_id": self.resource_id, "kind": self.kind, "label": self.label}


@dataclass(frozen=True)
class GoWaitEdge:
    thread_id: int
    resource_id: str
    operation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "resource_id": self.resource_id,
            "operation": self.operation,
        }


@dataclass(frozen=True)
class GoFinding:
    kind: GoFindingKind
    thread_ids: tuple[int, ...]
    summary: str
    confidence: Confidence

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "thread_ids": list(self.thread_ids),
            "summary": self.summary,
            "confidence": self.confidence.value,
        }


@dataclass(frozen=True)
class GoroutineSnapshot:
    goroutines: tuple[GoroutineInfo, ...]
    resources: tuple[GoResource, ...]
    edges: tuple[GoWaitEdge, ...]
    findings: tuple[GoFinding, ...]
    uncollected: int  # goroutines beyond the collection cap
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "goroutines": [g.to_dict() for g in self.goroutines],
            "resources": [r.to_dict() for r in self.resources],
            "edges": [e.to_dict() for e in self.edges],
            "findings": [f.to_dict() for f in self.findings],
            "uncollected": self.uncollected,
            "warnings": list(self.warnings),
        }
```

`src/tdb/go_concurrency/classifier.py`:

```python
"""Goroutine state from stack shape — pure functions, no DAP.

A parked goroutine's top frames are runtime.gopark(+unlock); the
nearest recognizable caller names the park reason. `target_expr` is
the expression the collector evaluates IN the park frame to identify
the wait target: `c` (the *hchan argument of chanrecv/chansend) or
`addr` (the *uint32 semaphore of the sync semacquire family).
"""

from __future__ import annotations

from dataclasses import dataclass

from tdb.go_concurrency.models import GoroutineState

# Ordered: first match on the parked stack wins. (frame-name prefix,
# state, operation, target_expr)
_PARK_RULES: tuple[tuple[str, GoroutineState, str | None, str | None], ...] = (
    ("runtime.chanrecv", GoroutineState.CHAN_RECV, "recv", "c"),
    ("runtime.chansend", GoroutineState.CHAN_SEND, "send", "c"),
    ("runtime.selectgo", GoroutineState.SELECT, None, None),
    ("sync.runtime_SemacquireMutex", GoroutineState.MUTEX_WAIT, "mutex", "addr"),
    ("sync.runtime_SemacquireRWMutex", GoroutineState.MUTEX_WAIT, "mutex", "addr"),
    ("sync.runtime_SemacquireWaitGroup", GoroutineState.WAITGROUP_WAIT, "waitgroup", "addr"),
    ("sync.runtime_Semacquire", GoroutineState.WAITGROUP_WAIT, "waitgroup", "addr"),
    ("time.Sleep", GoroutineState.SLEEP, None, None),
)
_SYSCALL_MARKERS = ("syscall.Syscall", "syscall.syscall", "runtime.netpoll")


@dataclass(frozen=True)
class Classification:
    state: GoroutineState
    park_frame_index: int | None  # index into the frame list, or None
    operation: str | None
    target_expr: str | None


def classify_stack(frame_names: list[str]) -> Classification:
    if not frame_names:
        return Classification(GoroutineState.UNKNOWN, None, None, None)
    parked = any(name.startswith("runtime.gopark") for name in frame_names)
    if parked:
        for prefix, state, operation, target in _PARK_RULES:
            for i, name in enumerate(frame_names):
                if name.startswith(prefix):
                    return Classification(state, i, operation, target)
        # Parked for a reason we don't model (netpoll, finalizer, GC…).
        return Classification(GoroutineState.RUNTIME, None, None, None)
    if any(name.startswith(_SYSCALL_MARKERS) for name in frame_names):
        return Classification(GoroutineState.SYSCALL, None, None, None)
    if all(name.startswith(("runtime.", "runtime/")) for name in frame_names):
        return Classification(GoroutineState.RUNTIME, None, None, None)
    return Classification(GoroutineState.RUNNING, None, None, None)
```

Note on rule order: `sync.runtime_Semacquire` is a string prefix of `sync.runtime_SemacquireMutex`, so the bare `Semacquire` (WaitGroup) entry MUST stay after the Mutex/RWMutex/WaitGroup-specific entries in `_PARK_RULES` — first match wins. The tuple above is already in that order; preserve it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_go_classifier.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/go_concurrency tests/unit/test_go_classifier.py
git commit -m "go_concurrency: snapshot models and stack-shape classifier"
```

---

### Task 7: goroutine collector and wait-graph analyzer

**Files:**
- Modify: `src/tdb/dap/client.py` (add `evaluate_raw`)
- Create: `src/tdb/go_concurrency/collector.py`
- Create: `src/tdb/go_concurrency/analyzer.py`
- Test: `tests/unit/test_go_analyzer.py`, `tests/unit/test_go_collector.py`

**Interfaces:**
- Consumes: `classify_stack` (Task 6), `DAPClient.threads()/stack_trace()`, `SessionGateError`, `SessionPhase`.
- Produces: `DAPClient.evaluate_raw(expression, frame_id=None, context="watch") -> dict` (full response body — the collector needs `memoryReference`); `GoConcurrencyCollector(max_goroutines=150).collect(controller) -> GoroutineSnapshot`; `analyze(goroutines, uncollected, warnings) -> GoroutineSnapshot`.

- [ ] **Step 1: Write the failing analyzer tests**

```python
"""tests/unit/test_go_analyzer.py"""
from tdb.go_concurrency.analyzer import analyze
from tdb.go_concurrency.models import (
    Confidence, GoFindingKind, GoroutineInfo, GoroutineState,
)


def _g(tid, state, op=None, res=None, func="main.f", runtime=False):
    return GoroutineInfo(
        thread_id=tid, goid=tid, function=func, state=state,
        operation=op, resource_id=res, frames=(), is_runtime=runtime,
    )


CH = "chan:0xc000024180"


def test_edges_and_resources_built():
    snap = analyze(
        [_g(1, GoroutineState.CHAN_RECV, "recv", CH),
         _g(2, GoroutineState.RUNNING)],
        uncollected=0, warnings=(),
    )
    assert [e.to_dict() for e in snap.edges] == [
        {"thread_id": 1, "resource_id": CH, "operation": "recv"}
    ]
    assert snap.resources[0].kind == "channel"


def test_matched_send_recv_is_not_a_finding():
    snap = analyze(
        [_g(1, GoroutineState.CHAN_RECV, "recv", CH),
         _g(2, GoroutineState.CHAN_SEND, "send", CH)],
        uncollected=0, warnings=(),
    )
    assert snap.findings == ()


def test_stuck_channel_confirmed_when_everyone_blocked():
    snap = analyze(
        [_g(1, GoroutineState.CHAN_RECV, "recv", CH),
         _g(2, GoroutineState.CHAN_RECV, "recv", CH),
         _g(3, GoroutineState.RUNTIME, runtime=True)],
        uncollected=0, warnings=(),
    )
    (f,) = snap.findings
    assert f.kind is GoFindingKind.STUCK_CHANNEL
    assert f.confidence is Confidence.CONFIRMED
    assert set(f.thread_ids) == {1, 2}


def test_stuck_channel_probable_when_something_still_runs():
    snap = analyze(
        [_g(1, GoroutineState.CHAN_RECV, "recv", CH),
         _g(2, GoroutineState.RUNNING)],
        uncollected=0, warnings=(),
    )
    (f,) = snap.findings
    assert f.confidence is Confidence.PROBABLE


def test_uncollected_downgrades_confidence():
    snap = analyze(
        [_g(1, GoroutineState.CHAN_RECV, "recv", CH)],
        uncollected=5, warnings=(),
    )
    (f,) = snap.findings
    assert f.confidence is Confidence.PROBABLE  # unseen goroutines may hold the sender


def test_mutex_convoy():
    sem = "sem:0xc00001c0a8"
    gs = [_g(i, GoroutineState.MUTEX_WAIT, "mutex", sem) for i in (1, 2, 3)]
    snap = analyze(gs, uncollected=0, warnings=())
    assert any(f.kind is GoFindingKind.MUTEX_CONVOY for f in snap.findings)


def test_likely_leak_needs_a_cluster():
    gs = [_g(i, GoroutineState.CHAN_RECV, "recv", CH) for i in range(1, 6)] + [
        _g(99, GoroutineState.RUNNING)
    ]
    snap = analyze(gs, uncollected=0, warnings=())
    kinds = {f.kind for f in snap.findings}
    assert GoFindingKind.LIKELY_LEAK in kinds
```

- [ ] **Step 2: Write the failing collector test**

```python
"""tests/unit/test_go_collector.py"""
import pytest

from tdb.dap.types import Source, StackFrame, Thread
from tdb.go_concurrency.collector import GoConcurrencyCollector
from tdb.go_concurrency.models import GoroutineState
from tdb.session.errors import SessionGateError
from tdb.session.state import SessionPhase


class FakeClient:
    def __init__(self, threads, stacks, evals=None, fail_stacks=()):
        self._threads = threads
        self._stacks = stacks
        self._evals = evals or {}
        self._fail = set(fail_stacks)

    async def threads(self):
        return self._threads

    async def stack_trace(self, thread_id, levels=64):
        if thread_id in self._fail:
            raise RuntimeError("boom")
        return self._stacks[thread_id]

    async def evaluate_raw(self, expr, frame_id=None, context="watch"):
        return self._evals.get((frame_id, expr), {"result": "nil"})


class FakeState:
    is_terminated = False
    phase = SessionPhase.STOPPED


class FakeController:
    def __init__(self, client):
        self.client = client
        self.state = FakeState()


def _frames(*names):
    return [
        StackFrame(id=100 + i, name=n, source=Source(path="/w/m.go"), line=1)
        for i, n in enumerate(names)
    ]


@pytest.mark.asyncio
async def test_collect_classifies_and_extracts_channel():
    threads = [Thread(id=1, name="* [Go 1] main.main"),
               Thread(id=2, name="[Go 5] main.worker")]
    stacks = {
        1: _frames("main.main"),
        2: _frames("runtime.gopark", "runtime.chanrecv", "main.worker"),
    }
    # park frame for thread 2 is index 1 -> frame id 101; `c` evaluates
    # with a memoryReference carrying the channel address.
    evals = {(101, "c"): {"result": "*runtime.hchan {...}", "memoryReference": "0xc000024180"}}
    snap = await GoConcurrencyCollector().collect(
        FakeController(FakeClient(threads, stacks, evals))
    )
    by_id = {g.thread_id: g for g in snap.goroutines}
    assert by_id[1].state is GoroutineState.RUNNING
    assert by_id[1].goid == 1
    assert by_id[2].state is GoroutineState.CHAN_RECV
    assert by_id[2].resource_id == "chan:0xc000024180"
    assert snap.uncollected == 0


@pytest.mark.asyncio
async def test_collect_caps_and_reports_uncollected():
    threads = [Thread(id=i, name=f"[Go {i}] main.w") for i in range(1, 12)]
    stacks = {i: _frames("main.w") for i in range(1, 12)}
    snap = await GoConcurrencyCollector(max_goroutines=10).collect(
        FakeController(FakeClient(threads, stacks))
    )
    assert len(snap.goroutines) == 10
    assert snap.uncollected == 1


@pytest.mark.asyncio
async def test_stack_failure_degrades_to_unknown():
    threads = [Thread(id=1, name="[Go 1] main.main")]
    snap = await GoConcurrencyCollector().collect(
        FakeController(FakeClient(threads, {}, fail_stacks={1}))
    )
    assert snap.goroutines[0].state is GoroutineState.UNKNOWN
    assert snap.warnings  # degradation is surfaced


@pytest.mark.asyncio
async def test_gate_raises_when_running():
    class RunningState(FakeState):
        phase = SessionPhase.RUNNING

    ctrl = FakeController(FakeClient([], {}))
    ctrl.state = RunningState()
    with pytest.raises(SessionGateError):
        await GoConcurrencyCollector().collect(ctrl)
```

(Check `tdb.session.state.SessionPhase` member names — the rust collector at `src/tdb/rust_concurrency/collector.py:124` uses `SessionPhase.STOPPED`; use whatever non-stopped member exists, e.g. `RUNNING`, adjusting the test to the real enum.)

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/unit/test_go_analyzer.py tests/unit/test_go_collector.py -v`
Expected: FAIL — modules don't exist.

- [ ] **Step 4: Implement**

`DAPClient.evaluate_raw` in `src/tdb/dap/client.py`, next to the existing `evaluate` method (find it via `grep -n "async def evaluate" src/tdb/dap/client.py`), mirroring its argument shape:

```python
    async def evaluate_raw(
        self,
        expression: str,
        frame_id: int | None = None,
        context: str = "watch",
    ) -> dict[str, Any]:
        """Like evaluate(), but returns the full response body — the Go
        concurrency collector needs `memoryReference` to identify wait
        targets, which the (result, variablesReference) tuple drops."""
        args: dict[str, Any] = {"expression": expression, "context": context}
        if frame_id is not None:
            args["frameId"] = frame_id
        resp = await self._send("evaluate", args)
        return resp.body
```

`src/tdb/go_concurrency/analyzer.py`:

```python
"""Bipartite wait graph (goroutine -> resource) and conservative findings.

Never goroutine->goroutine edges, never mutex owners. Confidence rules:
CONFIRMED only when the whole picture was seen (uncollected == 0) and
no non-runtime goroutine is still running; otherwise PROBABLE.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from tdb.go_concurrency.models import (
    Confidence,
    GoFinding,
    GoFindingKind,
    GoResource,
    GoroutineInfo,
    GoroutineSnapshot,
    GoroutineState,
    GoWaitEdge,
)

_LEAK_CLUSTER = 4  # same-channel waiter count that suggests a leak
_CONVOY_SIZE = 3  # same-semaphore waiter count worth flagging


def analyze(
    goroutines: Iterable[GoroutineInfo],
    uncollected: int,
    warnings: tuple[str, ...],
) -> GoroutineSnapshot:
    gs = tuple(goroutines)
    edges: list[GoWaitEdge] = []
    waiters: dict[str, list[GoroutineInfo]] = defaultdict(list)
    for g in gs:
        if g.resource_id is not None and g.operation is not None:
            edges.append(GoWaitEdge(g.thread_id, g.resource_id, g.operation))
            waiters[g.resource_id].append(g)

    resources = tuple(
        GoResource(
            rid,
            "channel" if rid.startswith("chan:") else "semaphore",
            f"{'chan' if rid.startswith('chan:') else 'sem'} {rid.split(':', 1)[1]}",
        )
        for rid in sorted(waiters)
    )

    someone_running = any(
        g.state is GoroutineState.RUNNING and not g.is_runtime for g in gs
    )
    full_picture = uncollected == 0 and not someone_running
    confidence = Confidence.CONFIRMED if full_picture else Confidence.PROBABLE

    findings: list[GoFinding] = []
    for resource in resources:
        group = waiters[resource.resource_id]
        ops = {g.operation for g in group}
        tids = tuple(sorted(g.thread_id for g in group))
        if resource.kind == "channel":
            if ops == {"recv"} or ops == {"send"}:
                side = "receiving" if ops == {"recv"} else "sending"
                other = "sender" if ops == {"recv"} else "receiver"
                findings.append(
                    GoFinding(
                        GoFindingKind.STUCK_CHANNEL,
                        tids,
                        f"{len(group)} goroutine(s) {side} on {resource.label} "
                        f"with no {other} observed",
                        confidence,
                    )
                )
                if len(group) >= _LEAK_CLUSTER:
                    findings.append(
                        GoFinding(
                            GoFindingKind.LIKELY_LEAK,
                            tids,
                            f"{len(group)} goroutines parked on {resource.label} "
                            f"— possible goroutine leak",
                            Confidence.PROBABLE,
                        )
                    )
        elif len(group) >= _CONVOY_SIZE:
            findings.append(
                GoFinding(
                    GoFindingKind.MUTEX_CONVOY,
                    tids,
                    f"{len(group)} goroutines queued on {resource.label}",
                    Confidence.PROBABLE,
                )
            )

    return GoroutineSnapshot(
        goroutines=gs,
        resources=resources,
        edges=tuple(edges),
        findings=tuple(findings),
        uncollected=uncollected,
        warnings=warnings,
    )
```

`src/tdb/go_concurrency/collector.py`:

```python
"""Bounded goroutine collection over plain DAP — no injected probes.

Delve surfaces every goroutine as a DAP thread named
"[Go <goid>] <function>" ("* " prefix on the current one), so the
collector is: threads -> per-goroutine stackTrace -> classify ->
(for channel/semaphore parks) one frame-scoped evaluate to identify
the wait target. Every per-goroutine failure degrades that entry, not
the snapshot (spec's fail-soft rule).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from tdb.go_concurrency.analyzer import analyze
from tdb.go_concurrency.classifier import classify_stack
from tdb.go_concurrency.models import GoroutineInfo, GoroutineSnapshot, GoroutineState
from tdb.session.errors import SessionGateError
from tdb.session.state import SessionPhase

log = logging.getLogger(__name__)

_DLV_THREAD_RE = re.compile(r"^\*?\s*\[Go (\d+)\]\s*(.*)$")
_ADDR_RE = re.compile(r"0x[0-9a-fA-F]+")


class GoConcurrencyCollector:
    def __init__(self, *, max_goroutines: int = 150, max_frames: int = 64) -> None:
        self.max_goroutines = max_goroutines
        self.max_frames = max_frames

    @staticmethod
    def _gate(controller: Any) -> None:
        state = controller.state
        if state.is_terminated:
            raise SessionGateError("terminated")
        if state.phase is not SessionPhase.STOPPED:
            raise SessionGateError("running")

    async def collect(self, controller: Any) -> GoroutineSnapshot:
        self._gate(controller)
        client = controller.client
        threads = await client.threads()

        # Cap with user goroutines first: runtime-named entries sort last.
        def runtime_last(t: Any) -> tuple[int, int]:
            m = _DLV_THREAD_RE.match(t.name or "")
            func = m.group(2) if m else (t.name or "")
            return (1 if func.startswith(("runtime.", "runtime/")) else 0, t.id)

        ordered = sorted(threads, key=runtime_last)
        selected = ordered[: self.max_goroutines]
        uncollected = len(ordered) - len(selected)

        goroutines: list[GoroutineInfo] = []
        warnings: list[str] = []
        for t in selected:
            self._gate(controller)  # a resume mid-collection aborts cleanly
            m = _DLV_THREAD_RE.match(t.name or "")
            goid = int(m.group(1)) if m else None
            function = (m.group(2) if m else t.name or "").strip()
            try:
                frames = await client.stack_trace(t.id, levels=self.max_frames)
            except Exception:
                log.debug("goroutine %s: stack fetch failed", t.id)
                warnings.append(f"goroutine {goid or t.id}: stack unavailable")
                goroutines.append(
                    GoroutineInfo(t.id, goid, function, GoroutineState.UNKNOWN,
                                  None, None, (), False)
                )
                continue
            names = [f.name for f in frames]
            c = classify_stack(names)
            resource_id: str | None = None
            if c.target_expr is not None and c.park_frame_index is not None:
                resource_id = await self._resolve_target(
                    client, frames[c.park_frame_index].id, c.target_expr, c.operation
                )
            goroutines.append(
                GoroutineInfo(
                    thread_id=t.id,
                    goid=goid,
                    function=function,
                    state=c.state,
                    operation=c.operation,
                    resource_id=resource_id,
                    frames=tuple(names),
                    is_runtime=c.state is GoroutineState.RUNTIME,
                )
            )
        return analyze(goroutines, uncollected, tuple(warnings))

    @staticmethod
    async def _resolve_target(
        client: Any, frame_id: int, expr: str, operation: str | None
    ) -> str | None:
        """Evaluate the park frame's channel/semaphore argument and turn
        it into a stable resource key. Prefers the DAP memoryReference;
        falls back to any address literal in the printed value."""
        try:
            body = await client.evaluate_raw(expr, frame_id=frame_id, context="watch")
        except Exception:
            log.debug("target eval %r in frame %s failed", expr, frame_id)
            return None
        addr = body.get("memoryReference")
        if not addr:
            m = _ADDR_RE.search(body.get("result", ""))
            addr = m.group(0) if m else None
        if not addr:
            return None
        prefix = "chan" if operation in ("recv", "send") else "sem"
        return f"{prefix}:{addr}"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_go_analyzer.py tests/unit/test_go_collector.py tests/unit/test_go_classifier.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/tdb/go_concurrency src/tdb/dap/client.py tests/unit/test_go_analyzer.py tests/unit/test_go_collector.py
git commit -m "go_concurrency: bounded DAP collector and conservative wait-graph analyzer"
```

---

### Task 8: InspectService, RPC action, MCP tool

**Files:**
- Modify: `src/tdb/session/inspect_service.py` (generalize the gate; add `collect_go_concurrency`)
- Modify: `src/tdb/server/handlers.py` (action registry ~lines 117/147/225; new `action_goroutines` next to `action_rust_concurrency` ~line 697)
- Modify: `src/tdb/mcp/server.py` (new tool next to `rust_concurrency` ~line 332)
- Test: `tests/unit/test_go_inspect_service.py`

**Interfaces:**
- Produces: `InspectService.collect_go_concurrency() -> GoroutineSnapshot`; `_require_concurrency_inspection(kind: str)` (rust call site updated to pass `"rust"`); RPC action `goroutines` (no params, `ok_data(snapshot.to_dict(), summary)`); MCP tool `goroutines`.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/unit/test_go_inspect_service.py"""
import pytest

from tdb.session.inspect_service import InspectService, SessionGateError


class _Caps:
    concurrency_inspection = "go"
    task_inspection = False


class _Profile:
    capabilities = _Caps()


class _State:
    is_terminated = False
    is_running = False


class _Ctrl:
    profile = _Profile()
    state = _State()


@pytest.mark.asyncio
async def test_go_gate_rejects_rust_and_python_profiles():
    ctrl = _Ctrl()
    svc = InspectService(lambda: ctrl)
    ctrl.profile.capabilities.concurrency_inspection = None
    with pytest.raises(SessionGateError) as e:
        await svc.collect_go_concurrency()
    assert e.value.reason == "unsupported"
    ctrl.profile.capabilities.concurrency_inspection = "rust"
    with pytest.raises(SessionGateError):
        await svc.collect_go_concurrency()


@pytest.mark.asyncio
async def test_rust_gate_still_works():
    ctrl = _Ctrl()
    ctrl.profile.capabilities.concurrency_inspection = "go"
    svc = InspectService(lambda: ctrl)
    with pytest.raises(SessionGateError):
        await svc.collect_rust_concurrency()
```

(Adapt attribute spelling to `SessionGateError`'s actual constructor — see `src/tdb/session/errors.py`; the TUI reads `e.reason`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_go_inspect_service.py -v`
Expected: FAIL — no `collect_go_concurrency`.

- [ ] **Step 3: Implement**

`src/tdb/session/inspect_service.py`:

- Imports: `from tdb.go_concurrency.collector import GoConcurrencyCollector` and `from tdb.go_concurrency.models import GoroutineSnapshot`.
- `__init__`: add `self._go_collector = GoConcurrencyCollector()`.
- Generalize the gate and add the service method:

```python
    def _require_concurrency_inspection(self, kind: str) -> None:
        if self._ctrl.profile.capabilities.concurrency_inspection != kind:
            raise SessionGateError("unsupported")
```

Update the rust call site (`collect_rust_concurrency`) to `self._require_concurrency_inspection("rust")`, and add:

```python
    async def collect_go_concurrency(self) -> GoroutineSnapshot:
        """Collect the goroutine wait-graph snapshot while stopped."""
        self._require_concurrency_inspection("go")
        self._gate()
        return await self._go_collector.collect(self._ctrl)
```

`src/tdb/server/handlers.py` — three registry entries mirroring `rust_concurrency` exactly (same three lists at ~117/147/225: name, help text `"params: []  -- show Go goroutines, wait graph, and findings"`, dispatch to `self.action_goroutines`), plus:

```python
    async def action_goroutines(self, params: list[Any]) -> RpcResponse:
        """Return the stopped goroutine snapshot as structured JSON data."""
        if params:
            return RpcResponse.error("goroutines does not accept parameters")
        try:
            snapshot = await self._inspect.collect_go_concurrency()
        except SessionGateError as e:
            return self._gate_error(e, "inspect goroutines")
        except Exception as e:
            return RpcResponse.error(f"Failed to collect goroutines: {e}")
        summary = (
            f"goroutines: {len(snapshot.goroutines)} collected "
            f"({snapshot.uncollected} beyond cap), "
            f"{len(snapshot.edges)} wait edge(s), "
            f"{len(snapshot.findings)} finding(s)"
        )
        return RpcResponse.ok_data(snapshot.to_dict(), summary)
```

`src/tdb/mcp/server.py`, next to the `rust_concurrency` tool:

```python
    @mcp.tool(
        description=(
            "Show goroutines with states (chan send/recv, select, mutex, "
            "WaitGroup, sleep, syscall), the channel/semaphore wait graph, "
            "and stuck-channel/convoy/leak findings. Go sessions only, "
            "while stopped; returns a JSON snapshot."
        )
    )
    async def goroutines() -> str:
        """Return a structured goroutine snapshot for a stopped Go session."""
        return _format(await sess._call("goroutines", []))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_go_inspect_service.py tests/unit -k "rpc or handlers" -v`
Expected: PASS (existing RPC tests confirm the registry lists stayed consistent).

- [ ] **Step 5: Commit**

```bash
git add src/tdb/session/inspect_service.py src/tdb/server/handlers.py src/tdb/mcp/server.py tests/unit/test_go_inspect_service.py
git commit -m "Expose goroutine snapshots via InspectService, RPC action, MCP tool"
```

---

### Task 9: GoroutinesModal

**Files:**
- Create: `src/tdb/widgets/goroutines_modal.py`
- Test: `tests/unit/test_goroutines_modal.py`

**Interfaces:**
- Consumes: `GoroutineSnapshot`/`GoroutineInfo`/`GoFinding` (Task 6), `_InspectableListModal`, `VariableView`.
- Produces: `GoroutinesModal(snapshot, current_thread_id)` with messages `RefreshSnapshot`, `LoadGoroutineDetail(thread_id)`, `SelectGoroutine(thread_id)`, `SelectFrame(thread_id, frame_id)`; methods `update_snapshot(snapshot)`, `show_thread_detail(thread_id, frames, scopes, variables)`; binding `a` toggles runtime goroutines.

- [ ] **Step 1: Write the failing tests** (pure-logic tests; full TUI behavior is covered by the integration task)

```python
"""tests/unit/test_goroutines_modal.py"""
from tdb.go_concurrency.models import (
    Confidence, GoFinding, GoFindingKind, GoroutineInfo, GoroutineSnapshot,
    GoroutineState,
)
from tdb.widgets.goroutines_modal import GoroutinesModal


def _g(tid, state=GoroutineState.RUNNING, runtime=False, res=None, op=None):
    return GoroutineInfo(tid, tid, f"main.f{tid}", state, op, res, (), runtime)


def _snap(goroutines, findings=(), uncollected=0):
    return GoroutineSnapshot(
        goroutines=tuple(goroutines), resources=(), edges=(),
        findings=tuple(findings), uncollected=uncollected, warnings=(),
    )


def test_runtime_goroutines_hidden_by_default():
    m = GoroutinesModal(_snap([_g(1), _g(2, GoroutineState.RUNTIME, runtime=True)]), 1)
    assert [g.thread_id for g in m.visible_items()] == [1]
    m._show_runtime = True
    assert [g.thread_id for g in m.visible_items()] == [1, 2]


def test_finding_members_marked():
    f = GoFinding(GoFindingKind.STUCK_CHANNEL, (2,), "stuck", Confidence.CONFIRMED)
    m = GoroutinesModal(_snap([_g(1), _g(2)], findings=[f]), 1)
    assert not m._in_finding(1)
    assert m._in_finding(2)


def test_header_reports_uncollected():
    m = GoroutinesModal(_snap([_g(1)], uncollected=7), 1)
    assert "7 more not collected" in m._header_text()


def test_row_shows_wait_target():
    g = _g(3, GoroutineState.CHAN_RECV, res="chan:0xc000024180", op="recv")
    m = GoroutinesModal(_snap([g]), 1)
    cells = m._format_row(g)
    assert any("chan:0xc000024180" in str(c) for c in cells)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_goroutines_modal.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement**

Create `src/tdb/widgets/goroutines_modal.py` following `rust_concurrency_modal.py`'s structure exactly (read it first: `src/tdb/widgets/rust_concurrency_modal.py`). Differences from the Rust modal, in full:

```python
"""Go goroutine inspection workspace.

Same shape as RustConcurrencyModal: an _InspectableListModal wrapped in
a TabbedContent — goroutine list + live detail, wait-graph tree,
findings. The snapshot is immutable (tdb.go_concurrency.models);
live stack/locals for the highlighted goroutine arrive via
LoadGoroutineDetail exactly like the Rust workspace's thread detail
(goroutines ARE DAP threads under Delve).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import DataTable, Label, Static, TabbedContent, TabPane, Tree

from rich.text import Text

from tdb.go_concurrency.models import (
    Confidence,
    GoroutineInfo,
    GoroutineSnapshot,
)
from tdb.widgets._inspection_modal import _InspectableListModal
from tdb.widgets.variable_view import VariableView

if TYPE_CHECKING:
    from tdb.dap.types import Scope, StackFrame, Variable


class GoroutinesModal(_InspectableListModal[GoroutineInfo]):
    """Near-full-screen view of one immutable goroutine snapshot."""

    KIND_LABEL = "Goroutines"
    TABLE_COLUMNS = ("ID", "State", "Function", "Waiting on")
    FOOTER_HINT = (
        "ESC close  |  r refresh  |  a show runtime  |  Enter select  |  tab tabs"
    )

    BINDINGS = _InspectableListModal.BINDINGS + [
        Binding("a", "toggle_runtime", "Show all", show=False),
    ]

    DEFAULT_CSS = """
    GoroutinesModal TabbedContent { height: 1fr; }
    GoroutinesModal #info { height: 5; max-height: 5; padding: 0 2; overflow-y: auto; }
    GoroutinesModal #frames-table { height: 1fr; min-height: 3; }
    GoroutinesModal #vars { height: 1fr; min-height: 3; }
    GoroutinesModal #wait-graph-tree { height: 1fr; width: 1fr; }
    GoroutinesModal #findings-list { padding: 1 2; overflow-y: auto; height: 1fr; }
    """

    def __init__(
        self, snapshot: GoroutineSnapshot, current_thread_id: int | None
    ) -> None:
        super().__init__()
        self._snapshot = snapshot
        self._current_thread_id = current_thread_id
        self._show_runtime = False
        self._items: list[GoroutineInfo] = self.visible_items()
        self._detail_thread_id: int | None = None
        self._detail_frames: list[StackFrame] = []

    # --- Snapshot-derived helpers ------------------------------------

    def visible_items(self) -> list[GoroutineInfo]:
        return [
            g
            for g in self._snapshot.goroutines
            if self._show_runtime or not g.is_runtime
        ]

    def _in_finding(self, thread_id: int) -> bool:
        return any(thread_id in f.thread_ids for f in self._snapshot.findings)

    # --- Messages ------------------------------------------------------

    class RefreshSnapshot(Message):
        """Request one fresh goroutine snapshot."""

    class LoadGoroutineDetail(Message):
        def __init__(self, thread_id: int) -> None:
            self.thread_id = thread_id
            super().__init__()

    class SelectGoroutine(Message):
        def __init__(self, thread_id: int) -> None:
            self.thread_id = thread_id
            super().__init__()

    class SelectFrame(Message):
        def __init__(self, thread_id: int, frame_id: int) -> None:
            self.thread_id = thread_id
            self.frame_id = frame_id
            super().__init__()
```

Then, copied structurally from `RustConcurrencyModal` with these substitutions (write them out in the file — do not reference the Rust class at runtime):

- `compose()` / `_compose_body()` / `on_mount()`: identical shape; TabPanes are `("Goroutines", id="goroutines-tab")`, `("Wait Graph", id="wait-graph-tab")` containing only the `Tree(id="wait-graph-tree")`, `("Findings", id="findings-tab")` containing `Static(id="findings-list")`; `initial="goroutines-tab"`.
- `update_snapshot(snapshot)`: also re-derives `self._items = self.visible_items()`, then same repopulate flow as Rust's, ending with `self._render_wait_graph()` and `self._render_findings()`.
- `action_toggle_runtime()`: flips `self._show_runtime`, then runs the same repopulate flow as `update_snapshot` (factor it into `_repopulate()` used by both).
- `_header_text()`:

```python
    def _header_text(self) -> str:
        extra = ""
        if self._snapshot.uncollected:
            extra = f" — {self._snapshot.uncollected} more not collected"
        if self._snapshot.warnings:
            extra += f" — {len(self._snapshot.warnings)} warning(s)"
        return f"{self.KIND_LABEL} ({len(self.visible_items())}){extra}"
```

- `_format_row(item)`:

```python
    def _format_row(self, item: GoroutineInfo) -> tuple:
        gid = Text(f"Go {item.goid}" if item.goid is not None else str(item.thread_id))
        state = Text(item.state.value)
        func = Text(item.function)
        wait = Text(item.resource_id or "—")
        if item.thread_id == self._current_thread_id:
            gid.stylize("bold")
            func.stylize("bold")
        if self._in_finding(item.thread_id):
            for cell in (gid, state, func, wait):
                cell.stylize("red")
        return (gid, state, func, wait)
```

- `_empty_state_text()`: `"No goroutines found"`.
- `_initial_cursor_index()`, `_render_loading_detail(item)` (show ID/state/function, wait target when present, and the finding summaries touching this goroutine), `_on_after_show_detail(item)` (post `LoadGoroutineDetail`), `_select_id_for`, `_make_select_message` (→ `SelectGoroutine`), `_make_refresh_message` (→ `RefreshSnapshot`), `show_thread_detail(...)`, and the `#frames-table` `on_data_table_row_selected` handler: all structurally identical to the Rust modal's versions (including the "do not call super()" comment on the frames-table handler).
- `_render_wait_graph()`: tree of resource → waiter leaves:

```python
    def _render_wait_graph(self) -> None:
        tree = self.query_one("#wait-graph-tree", Tree)
        tree.clear()
        by_resource: dict[str, list] = {}
        for edge in self._snapshot.edges:
            by_resource.setdefault(edge.resource_id, []).append(edge)
        if not by_resource:
            tree.root.add_leaf(Text("No observed waits", style="dim"))
            return
        labels = {r.resource_id: r.label for r in self._snapshot.resources}
        funcs = {g.thread_id: g.function for g in self._snapshot.goroutines}
        goids = {g.thread_id: g.goid for g in self._snapshot.goroutines}
        for rid, edges in sorted(by_resource.items()):
            node = tree.root.add(Text(labels.get(rid, rid), style="bold"))
            for e in edges:
                goid = goids.get(e.thread_id)
                who = f"Go {goid}" if goid is not None else f"thread {e.thread_id}"
                node.add_leaf(Text(f"{who} — {e.operation} — {funcs.get(e.thread_id, '?')}"))
            node.expand()
```

- `_render_findings()`: one section, style by confidence (`Confidence.CONFIRMED` → `"bold red"`, `Confidence.PROBABLE` → `"yellow"`), `"  none"` dim when empty, warnings block on top like Rust's.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_goroutines_modal.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/widgets/goroutines_modal.py tests/unit/test_goroutines_modal.py
git commit -m "Add GoroutinesModal: goroutine list, wait-graph tree, findings tabs"
```

---

### Task 10: TUI wiring — workflows, routing, panels, menu label

**Files:**
- Modify: `src/tdb/app_handlers/ui_panels.py` (add `goroutines` slot; extend `clear()` and add a dismiss helper mirroring `dismiss_rust_concurrency`)
- Modify: `src/tdb/app_handlers/inspection.py` (dispatch in `open_threads` ~line 244; new `open_goroutines`/`refresh_goroutines`/`load_goroutine_detail`; relabel in `update_thread_count`)
- Modify: `src/tdb/app_handlers/routing.py` (four handlers after the Rust block ~line 205)
- Test: `tests/unit/test_go_workflows.py`

**Interfaces:**
- Consumes: `InspectService.collect_go_concurrency` (Task 8), `GoroutinesModal` (Task 9).
- Produces: `InspectionWorkflows.open_goroutines() -> bool`, `refresh_goroutines()`, `load_goroutine_detail(thread_id)`; `UIPanels.goroutines: GoroutinesModal | None`.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/unit/test_go_workflows.py

open_threads dispatch: a Go profile routes to the goroutine workspace,
and falls back to the generic thread list when snapshot collection
fails. Uses the same App-stubbing style as the existing inspection
workflow tests (see tests/unit/test_* for ThreadsModal workflows; if
none exists, this stub-based approach stands alone).
"""
import pytest

from tdb.app_handlers.inspection import InspectionWorkflows
from tdb.session.inspect_service import SessionGateError


class _Caps:
    concurrency_inspection = "go"
    classify_threads = None
    task_inspection = False


class _Profile:
    capabilities = _Caps()
    display_name = "Go"

    class presentation:
        frame_name = None


class _State:
    is_terminated = False
    is_running = False
    current_thread_id = 1
    threads = []


class _Ctrl:
    profile = _Profile()
    state = _State()


class _App:
    def __init__(self):
        self.controller = _Ctrl()
        self.pushed = []
        self.notifications = []

        class _Panels:
            goroutines = None
            threads = None

        self.panels = _Panels()

    def push_screen(self, modal, callback=None):
        self.pushed.append(modal)

    def notify(self, *a, **k):
        self.notifications.append(a)


@pytest.mark.asyncio
async def test_open_threads_routes_to_goroutines(monkeypatch):
    app = _App()
    wf = InspectionWorkflows(app)

    from tdb.go_concurrency.models import GoroutineSnapshot
    snap = GoroutineSnapshot((), (), (), (), 0, ())

    async def fake_collect():
        return snap

    monkeypatch.setattr(wf._svc, "collect_go_concurrency", fake_collect)
    await wf.open_threads()
    assert len(app.pushed) == 1
    from tdb.widgets.goroutines_modal import GoroutinesModal
    assert isinstance(app.pushed[0], GoroutinesModal)
    assert app.panels.goroutines is app.pushed[0]


@pytest.mark.asyncio
async def test_open_threads_falls_back_when_snapshot_fails(monkeypatch):
    app = _App()
    wf = InspectionWorkflows(app)

    async def boom():
        raise RuntimeError("collector broke")

    async def fake_list_threads():
        return []  # fallback path: "No threads found" notification

    monkeypatch.setattr(wf._svc, "collect_go_concurrency", boom)
    monkeypatch.setattr(wf._svc, "list_threads", fake_list_threads)
    await wf.open_threads()
    assert app.pushed == []  # nothing opened
    assert app.notifications  # but the user heard about it
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_go_workflows.py -v`
Expected: FAIL — no goroutine dispatch.

- [ ] **Step 3: Implement**

`src/tdb/app_handlers/ui_panels.py`: add the TYPE_CHECKING import for `GoroutinesModal`, the field `goroutines: "GoroutinesModal | None" = None`, include it wherever `clear()` resets the modal fields, and a `dismiss_goroutines()` helper copied from `dismiss_rust_concurrency` with names substituted.

`src/tdb/app_handlers/inspection.py`:

- Import `from tdb.widgets.goroutines_modal import GoroutinesModal`.
- In `open_threads`, replace the rust-only divert with a dispatch:

```python
        ci = ctrl.profile.capabilities.concurrency_inspection
        if ci == "rust":
            if await self.open_rust_concurrency():
                return
        elif ci == "go":
            if await self.open_goroutines():
                return
            # Snapshot failed: fall through to the generic thread list so
            # a Go session never loses its threads view entirely.
```

- New methods, mirroring the three rust ones (`open_rust_concurrency` at ~line 274, `refresh_rust_concurrency`, `load_rust_thread_detail` at ~line 334):

```python
    async def open_goroutines(self) -> bool:
        """Collect a goroutine snapshot and open the Go workspace.

        Returns False only when snapshot collection itself failed — the
        caller then falls back to the generic Threads modal. Session-gate
        refusals return True: the generic list would be gated identically.
        """
        ctrl = self.app.controller
        try:
            snapshot = await self._svc.collect_go_concurrency()
        except SessionGateError:
            return True
        except Exception:
            log.exception("Error collecting goroutine snapshot")
            self.app.notify(
                "Goroutine snapshot failed — showing plain thread list",
                title="Goroutines",
            )
            return False
        modal = GoroutinesModal(snapshot, ctrl.state.current_thread_id)
        self.app.panels.goroutines = modal
        self.app.push_screen(modal, callback=self._on_goroutines_dismissed)
        return True

    def _on_goroutines_dismissed(self, _result: object) -> None:
        self.app.panels.goroutines = None

    async def refresh_goroutines(self) -> None:
        """Replace every Go workspace tab from one fresh snapshot."""
        try:
            snapshot = await self._svc.collect_go_concurrency()
        except SessionGateError:
            return
        except Exception:
            log.exception("Error refreshing goroutine snapshot")
            self.app.notify(
                "Refresh failed — showing the previous snapshot",
                title="Goroutines",
            )
            return
        modal = self.app.panels.goroutines
        if modal is not None:
            modal.update_snapshot(snapshot)

    async def load_goroutine_detail(self, thread_id: int) -> None:
        """Live DAP stack + locals for the highlighted goroutine (it is
        a DAP thread, so thread_stack serves it unchanged)."""
        modal = self.app.panels.goroutines
        if modal is None:
            return
        try:
            frames, scopes, variables = await self._svc.thread_stack(thread_id)
        except SessionGateError:
            return
        except Exception:
            log.debug("Failed to fetch goroutine detail for %d", thread_id)
            frames, scopes, variables = [], [], {}
        modal.show_thread_detail(thread_id, frames, scopes, variables)
```

- In `update_thread_count`, relabel for Go (goroutine counts are interesting from 1 up):

```python
        if self.app.controller.profile.capabilities.concurrency_inspection == "go":
            label = f"Goroutines ({len(threads)})" if threads else "Goroutines"
            menu_bar.update_action_label("threads-label", label)
            return
```

(placed before the existing `>= 2` logic.)

`src/tdb/app_handlers/routing.py` — import `GoroutinesModal`, then four handlers copied from the Rust block with names substituted:

```python
    # --- Modal: Goroutines --------------------------------------------

    async def on_goroutines_modal_refresh_snapshot(
        self,
        message: GoroutinesModal.RefreshSnapshot,
    ) -> None:
        await self._inspection.refresh_goroutines()

    async def on_goroutines_modal_load_goroutine_detail(
        self,
        message: GoroutinesModal.LoadGoroutineDetail,
    ) -> None:
        await self._inspection.load_goroutine_detail(message.thread_id)

    async def on_goroutines_modal_select_goroutine(
        self,
        message: GoroutinesModal.SelectGoroutine,
    ) -> None:
        if isinstance(self.screen, GoroutinesModal):  # type: ignore[attr-defined]
            self.screen.dismiss(None)  # type: ignore[attr-defined]
        await self.controller.switch_active_thread(message.thread_id)
        self._sync_views_to_top_frame()
        self.query_one("#code-view", CodeView).focus()  # type: ignore[attr-defined]

    async def on_goroutines_modal_select_frame(
        self,
        message: GoroutinesModal.SelectFrame,
    ) -> None:
        if isinstance(self.screen, GoroutinesModal):  # type: ignore[attr-defined]
            self.screen.dismiss(None)  # type: ignore[attr-defined]
        await self.controller.switch_active_thread(message.thread_id)
        await self.controller.select_frame(message.frame_id)
        self._sync_views_to_top_frame()
        self.query_one("#code-view", CodeView).focus()  # type: ignore[attr-defined]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_go_workflows.py tests/unit -k "inspection or threads" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/app_handlers/ui_panels.py src/tdb/app_handlers/inspection.py src/tdb/app_handlers/routing.py tests/unit/test_go_workflows.py
git commit -m "Wire goroutine workspace into threads flow, routing, and menu label"
```

---

### Task 11: Integration tests — real dlv, real Go programs

**Files:**
- Create: `tests/integration/fixtures/go_simple/main.go`
- Create: `tests/integration/fixtures/go_blocked/main.go`
- Create: `tests/integration/fixtures/go_testmode/mathy.go`, `tests/integration/fixtures/go_testmode/mathy_test.go`, `tests/integration/fixtures/go_testmode/go.mod`
- Create: `tests/integration/test_go_session.py`

**Interfaces:**
- Consumes: everything above, `DebugController`, `ServerEventHandler` (see `tests/integration/test_cpp_session.py` for the session fixture pattern — copy its `session` fixture and stop-waiting helpers, substituting `build_go_profile()`).

- [ ] **Step 1: Write the fixtures**

`tests/integration/fixtures/go_simple/main.go`:

```go
package main

import "fmt"

func add(a int, b int) int {
	result := a + b
	return result // BP line 7
}

func main() {
	x := 5
	y := add(x, 7)
	fmt.Println("total =", y)
}
```

`tests/integration/fixtures/go_blocked/main.go` (three workers parked on one channel, one on a mutex — the snapshot test's subject; program self-terminates on a timer so teardown is clean):

```go
package main

import (
	"fmt"
	"sync"
	"time"
)

var mu sync.Mutex

func recvWorker(id int, ch chan int) {
	v := <-ch // parks: nothing ever sends
	fmt.Println(id, v)
}

func lockWorker() {
	mu.Lock() // parks: main holds mu
	defer mu.Unlock()
}

func main() {
	ch := make(chan int)
	mu.Lock()
	for i := 0; i < 3; i++ {
		go recvWorker(i, ch)
	}
	go lockWorker()
	time.Sleep(200 * time.Millisecond) // let workers park
	marker := 42
	fmt.Println("marker =", marker) // BP line 30
	time.Sleep(10 * time.Second)    // window for inspection; test kills earlier
}
```

`tests/integration/fixtures/go_testmode/go.mod`:

```
module tdbfixtures/mathy

go 1.21
```

`tests/integration/fixtures/go_testmode/mathy.go`:

```go
package mathy

func Double(x int) int {
	return x * 2
}
```

`tests/integration/fixtures/go_testmode/mathy_test.go`:

```go
package mathy

import "testing"

func TestDouble(t *testing.T) {
	got := Double(21) // BP line 6
	if got != 42 {
		t.Fatalf("got %d", got)
	}
}
```

- [ ] **Step 2: Write the integration tests**

`tests/integration/test_go_session.py` — copy the module skeleton of `tests/integration/test_cpp_session.py` (skip guard, WAIT constant, `session` fixture with teardown, its stop-wait helper), with:

```python
pytestmark = pytest.mark.skipif(
    shutil.which("go") is None or shutil.which("dlv") is None,
    reason="go toolchain or dlv not installed",
)
```

and profile `build_go_profile()`. Tests to include (each following the launch → wait-for-stop → assert → continue/stop rhythm the cpp/perl session tests use):

1. `test_registry_detects_built_go_binary` — `go build` the go_simple fixture into `tmp_path`, assert `registry.detect(binary) == "go"` (real-world buildinfo sniff).
2. `test_debug_mode_breakpoint_and_evaluate` — launch `fixtures/go_simple/main.go` (debug mode), breakpoint at line 7 of `main.go`, wait for stop, assert stopped frame is in `add`, `evaluate("a + b")` returns `12`, `evaluate("result")` after one step returns `12`, continue to exit; assert console output contains `total = 12`.
3. `test_exec_mode_prebuilt_binary` — `go build` go_simple, launch the binary path (adapter must infer `mode: "exec"`), same breakpoint hits.
4. `test_test_mode_runs_test_binary` — `build_go_profile(program=pkg_dir, test=True)`, launch the `go_testmode` package dir, breakpoint at `mathy_test.go:6`, wait for stop, assert frame name contains `TestDouble`, continue to exit.
5. `test_goroutine_snapshot_states_and_findings` — launch `go_blocked/main.go` with a breakpoint at the `fmt.Println("marker =", marker)` line; on stop, run `GoConcurrencyCollector().collect(ctrl)` directly; assert: at least 5 goroutines; at least 3 in `GoroutineState.CHAN_RECV`; those 3 share one `resource_id`; at least one goroutine in `MUTEX_WAIT`; at least one `STUCK_CHANNEL` finding whose `thread_ids` cover the 3 receivers (confidence may be `PROBABLE` — main is stopped at a breakpoint, but the sleeper/runtime goroutines may classify as RUNNING on some Go versions; assert kind + membership, not confidence).
6. `test_panic_parsed_into_error` — a tiny inline fixture (write `tmp_path/"boom.go"` with an integer divide-by-zero) launched with no breakpoints; wait for termination; feed the captured stderr through `parse_go_error` and assert the frames include the panicking function. (If the controller exposes collected stderr differently, follow how `test_perl_exit_code.py` gets at it.)

- [ ] **Step 3: Run the integration tests**

Run: `pytest tests/integration/test_go_session.py -v`
Expected: PASS on a machine with `go` + `dlv`; SKIP cleanly otherwise. Iterate here — this is where Delve's real body shapes (thread-name format, evaluate `memoryReference` availability, stopOnEntry behavior) get verified against the plan's assumptions. Fix `src/tdb/languages/go.py` / collector details as reality dictates, keeping unit tests in sync.

- [ ] **Step 4: Run the full suite**

Run: `pytest tests/unit -x -q` then `pytest tests/integration -q`
Expected: no regressions anywhere.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/fixtures/go_simple tests/integration/fixtures/go_blocked tests/integration/fixtures/go_testmode tests/integration/test_go_session.py
git commit -m "Integration tests: Go debug/exec/test modes and goroutine snapshots"
```

---

### Task 12: CI toolchain, README, misc docs

**Files:**
- Modify: `Dockerfile` (language toolchain section, lines ~24-66)
- Modify: `README.md` ("Multi-Language Debugging" section, lines ~183-332)

**Interfaces:** none (docs/CI only).

- [ ] **Step 1: Dockerfile**

Following the pattern of the existing per-language blocks, add (adjust base-image package manager to match the file):

```dockerfile
# Go + Delve (Go debugging integration tests)
RUN apt-get install -y --no-install-recommends golang \
    && GOBIN=/usr/local/bin go install github.com/go-delve/delve/cmd/dlv@latest \
    && rm -rf /root/go /root/.cache/go-build
```

Verify `.github/workflows/test.yml` consumes the Dockerfile (it does for other languages); no separate workflow change expected.

- [ ] **Step 2: README**

- Add Go to the language support table: detection (`.go`, package dirs, Go-buildinfo binaries), adapter `dlv` (Delve >= 1.21, install command), modes (`tdb main.go`, `tdb ./pkg`, `tdb ./binary`, `tdb --test ./pkg`, `tdb -a PID`, `tdb -r host:port --lang go` against `dlv dap --listen`).
- Document the Goroutines workspace (opens from the Threads action in Go sessions; `a` reveals runtime goroutines; wait graph and findings tabs; 150-goroutine collection cap with explicit "more not collected" reporting).
- Limitations list: `--terminal` unsupported; select-case wait edges not shown; no mutex-holder identification (impossible in Go); `exec.Command` children not debugged; detection scans only the first 16MB of very large binaries (`--lang go` overrides).

- [ ] **Step 3: Verify docs build/lint (if the repo has a docs check) and run the full unit suite once more**

Run: `pytest tests/unit -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add Dockerfile README.md
git commit -m "Docs and CI: Go toolchain, README section for Go debugging"
```

---

## Self-review checklist (run after Task 12)

1. Spec coverage: every spec section maps to a task (S1→T1/T3, S2→T2/T5, S3→T6/T7/T8, S4→T9/T10, error handling→T4 + collector degradation in T7, testing→T11, out-of-scope items→T12 README).
2. `grep -rn "TODO\|TBD" src/tdb/go_concurrency src/tdb/languages/go.py` returns nothing.
3. Run `pytest tests/unit -q` and `pytest tests/integration/test_go_session.py -q` one final time; paste output into the finishing summary — no green claims without output.

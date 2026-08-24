# `--terminal` for All Languages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `tdb --terminal X program` work for Perl, Bash, Tcsh, and C/C++ (lldb-dap) by implementing the DAP `runInTerminal` reverse-request flow in each adapter.

**Architecture:** tdb's client side is already language-agnostic (`TerminalLauncher` answers `runInTerminal` for any adapter; the controller passes `console="externalTerminal"` and advertises `supportsRunInTerminalRequest`). Each in-repo adapter (perl, bash, tcsh) learns to send the reverse request instead of spawning the debuggee, with all control channels path/network-based (an emulator-spawned debuggee inherits no fds and cannot be `wait()`ed). Exit codes are reported (guardian status line for tcsh; `/bin/sh -c '...; echo $? > file'` wrapper for perl/bash), not reaped. lldb-dap needs only a launch-body flag; gdb errors out.

**Tech Stack:** Python asyncio, DAP, POSIX FIFOs, bash ≥ 4.4, perl ≥ 5.18, tcsh, GDB≥14/lldb-dap.

**Spec:** `docs/superpowers/specs/2026-08-14-terminal-all-languages-design.md` — read it first.

## Global Constraints

- Repo root is the project root; run tests with `uv run pytest <path> -q -p no:cacheprovider --no-cov` from it. Full-suite gates: host suite AND `docker build --target test .` (the CI path — no init reaper at PID 1, BusyBox userland).
- Ruff must stay clean on `src/` and `tests/` (`ruff check src tests`).
- A PostToolUse hook may reformat files repo-wide on edit; if unrelated files change, commit that noise separately as a `style:` commit (verify with AST comparison) before your feature commit.
- Never use `pytest.raises(ProcessLookupError)`/`os.kill(pid, 0)` as a "process is dead" probe in new tests — unreaped zombies keep pids alive in init-less CI. Use `tdb.adapters.tcsh.guardian._process_is_gone(pid)`.
- Control channels for emulator-spawned debuggees must be FIFOs/sockets/files — never inherited fds. Reverse-request commands must not wrap the debuggee in `setsid` (it would detach the controlling tty and break ^C).
- All tests must pass headlessly: tests act as the DAP client and spawn `runInTerminal` commands themselves; no real emulator in CI.
- `--terminal` stays launch-only and Unix-only. The `_TERMINAL_SPECS` emulator table is unchanged.
- Do not merge to main or create PRs; commit to the current branch `support_terminal_on_all_languages`.

---

### Task 1: Pass `console` through the custom-adapter launch bodies

The controller already calls every profile's `launch_body(..., console=...)`, but the perl/bash/tcsh profiles drop it. The adapters can't branch on what they never receive.

**Files:**
- Modify: `src/tdb/languages/perl.py` (launch_body, ~line 49)
- Modify: `src/tdb/languages/bash.py` (launch_body, ~line 44)
- Modify: `src/tdb/languages/tcsh.py` (launch_body, ~line 62)
- Test: `tests/unit/test_perl_profile.py`, `tests/unit/test_bash_profile.py`, `tests/unit/test_tcsh_profile.py`

**Interfaces:**
- Produces: each launch body dict gains `"console": <console>` (string, e.g. `"internalConsole"` / `"externalTerminal"`). Adapter servers in Tasks 6, 7, 9 read `arguments.get("console")`.

- [ ] **Step 1: Write the failing tests** — one per profile file, following each file's existing launch_body test style. Example for tcsh (mirror for perl/bash):

```python
def test_launch_body_carries_console() -> None:
    adapter = TcshAdapter()
    body = adapter.launch_body(
        program="/tmp/x.csh",
        args=[],
        cwd="/tmp",
        env=None,
        stop_on_entry=True,
        console="externalTerminal",
        opts={},
    )
    assert body["console"] == "externalTerminal"
```

- [ ] **Step 2: Run to verify all three fail** — `uv run pytest tests/unit/test_perl_profile.py tests/unit/test_bash_profile.py tests/unit/test_tcsh_profile.py -q` → 3 KeyError failures.
- [ ] **Step 3: Implement** — in each of the three `launch_body` methods, add `"console": console,` to the body dict literal.
- [ ] **Step 4: Re-run the three test files** → pass. Also run `uv run pytest tests/unit -q -k "profile"` for collateral.
- [ ] **Step 5: Commit** — `git commit -m "terminal: pass console through perl/bash/tcsh launch bodies"`.

---

### Task 2: CLI rejects `--terminal` with attach mode

**Files:**
- Modify: `src/tdb/cli.py` — inside `_validate_terminal_choice` (~line 260) or immediately after its call site in `parse_args` (~line 559)
- Test: `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: `parse_args(argv: list[str] | None) -> argparse.Namespace` (existing), `args.remote_attach` (set by `-r/--remote-attach`), `args.terminal`.
- Produces: `parse_args(["--terminal", "xterm", "-r", "5678"])` exits via `parser.error`.

- [ ] **Step 1: Write the failing test** in `tests/unit/test_cli.py` (house style uses bare `parse_args` + `pytest.raises(SystemExit)`):

```python
def test_terminal_rejected_with_remote_attach():
    with pytest.raises(SystemExit):
        parse_args(["--terminal", "xterm", "-r", "5678"])
```

- [ ] **Step 2: Run** `uv run pytest tests/unit/test_cli.py::test_terminal_rejected_with_remote_attach -q` → FAIL (parses fine today; note `--terminal xterm` requires xterm on PATH — if the dev box lacks xterm the test would exit for the wrong reason, so put the new check BEFORE the PATH check in Step 3 and assert the message via `capsys` if needed).
- [ ] **Step 3: Implement** — in `_validate_terminal_choice`, before the PATH check:

```python
    if args.terminal and getattr(args, "remote_attach", None):
        parser.error(
            "--terminal only applies when tdb launches the program; "
            "it cannot be combined with -r/--remote-attach"
        )
```

- [ ] **Step 4: Run** the test → PASS; run `uv run pytest tests/unit/test_cli.py -q` → all pass.
- [ ] **Step 5: Commit** — `git commit -m "terminal: reject --terminal in attach mode"`.

---

### Task 3: C/C++ — lldb-dap flag, gdb clear error

**Files:**
- Modify: `src/tdb/languages/cpp.py` (`LldbDapAdapter.launch_body` ~line 43, `GdbDapAdapter.launch_body` ~line 97)
- Test: `tests/unit/test_cpp_profile.py`

**Interfaces:**
- Produces: lldb launch body gains `"runInTerminal": True` iff `console == "externalTerminal"`; gdb `launch_body` raises `LanguageNotSupportedError` for it.

- [ ] **Step 1: Write the failing tests:**

```python
def test_lldb_launch_body_external_terminal_sets_run_in_terminal() -> None:
    body = LldbDapAdapter().launch_body(
        program="/bin/x",
        args=[],
        cwd="/",
        env=None,
        stop_on_entry=False,
        console="externalTerminal",
        opts={},
    )
    assert body["runInTerminal"] is True


def test_lldb_launch_body_internal_console_omits_run_in_terminal() -> None:
    body = LldbDapAdapter().launch_body(
        program="/bin/x",
        args=[],
        cwd="/",
        env=None,
        stop_on_entry=False,
        console="internalConsole",
        opts={},
    )
    assert "runInTerminal" not in body


def test_gdb_launch_body_rejects_external_terminal() -> None:
    with pytest.raises(LanguageNotSupportedError, match="lldb-dap"):
        GdbDapAdapter().launch_body(
            program="/bin/x",
            args=[],
            cwd="/",
            env=None,
            stop_on_entry=False,
            console="externalTerminal",
            opts={},
        )
```

- [ ] **Step 2: Run** `uv run pytest tests/unit/test_cpp_profile.py -q` → 3 new FAIL.
- [ ] **Step 3: Implement** — in `LldbDapAdapter.launch_body`, after the body literal: `if console == "externalTerminal": body["runInTerminal"] = True`. In `GdbDapAdapter.launch_body`, first lines:

```python
        if console == "externalTerminal":
            raise LanguageNotSupportedError(
                "--terminal is not supported with the gdb adapter (gdb's "
                "DAP mode has no terminal integration) — use "
                "`--adapter lldb-dap`"
            )
```

- [ ] **Step 4: Run** the file → PASS.
- [ ] **Step 5: Commit** — `git commit -m "terminal: lldb-dap runInTerminal flag; clear gdb error"`.

---

### Task 4: `ReverseRequester` — adapters send requests to the client

The perl and bash servers share message plumbing (`tdb.dap.messages`); both need to send `runInTerminal` and route the client's `Response` back. Their `run()` loops currently discard non-`Request` messages.

**Files:**
- Create: `src/tdb/dap/reverse.py`
- Modify: `src/tdb/adapters/perl/server.py` (`__init__`, `run()` ~line 128)
- Modify: `src/tdb/adapters/bash/server.py` (`__init__`, `run()` ~line 90)
- Test: `tests/unit/test_dap_reverse.py` (new)

**Interfaces:**
- Produces:
  - `class ReverseRequestError(Exception)`
  - `class ReverseRequester:`
    - `__init__(self, write: Callable[[dict], None], next_seq: Callable[[], int])`
    - `async request(self, command: str, arguments: dict, timeout: float = 30.0) -> Response` — raises `ReverseRequestError` on failure response, `TimeoutError` on timeout
    - `route(self, msg: object) -> bool` — True if `msg` was a `Response` for a pending reverse request (consumed)
  - Both servers hold it as `self._reverse` and their run loops call `self._reverse.route(msg)` before the `isinstance(msg, Request)` check.

- [ ] **Step 1: Write failing tests** in `tests/unit/test_dap_reverse.py`:

```python
import asyncio

import pytest

from tdb.dap.messages import Response
from tdb.dap.reverse import ReverseRequester, ReverseRequestError


def make_requester(sent: list[dict]) -> ReverseRequester:
    seq = iter(range(100, 200))
    return ReverseRequester(sent.append, lambda: next(seq))


async def test_request_resolves_on_matching_response():
    sent: list[dict] = []
    requester = make_requester(sent)
    task = asyncio.ensure_future(requester.request("runInTerminal", {"args": ["true"]}))
    await asyncio.sleep(0)
    assert sent[0]["command"] == "runInTerminal"
    assert sent[0]["type"] == "request"
    response = Response(
        seq=1,
        request_seq=sent[0]["seq"],
        command="runInTerminal",
        success=True,
        body={},
    )
    assert requester.route(response) is True
    assert (await task).success is True


async def test_failure_response_raises():
    sent: list[dict] = []
    requester = make_requester(sent)
    task = asyncio.ensure_future(requester.request("runInTerminal", {}))
    await asyncio.sleep(0)
    requester.route(
        Response(
            seq=1,
            request_seq=sent[0]["seq"],
            command="runInTerminal",
            success=False,
            message="no emulator",
        )
    )
    with pytest.raises(ReverseRequestError, match="no emulator"):
        await task


async def test_route_ignores_unrelated_messages():
    requester = make_requester([])
    assert requester.route(object()) is False
    assert (
        requester.route(Response(seq=1, request_seq=999, command="x", success=True))
        is False
    )
```

- [ ] **Step 2: Run** `uv run pytest tests/unit/test_dap_reverse.py -q` → import error.
- [ ] **Step 3: Implement** `src/tdb/dap/reverse.py`:

```python
"""Adapter-to-client (reverse) DAP requests, e.g. runInTerminal."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from tdb.dap.messages import Request, Response


class ReverseRequestError(Exception):
    """The client answered a reverse request with success=false."""


class ReverseRequester:
    """Send requests from a DAP adapter to its client and await replies."""

    def __init__(
        self,
        write: Callable[[dict[str, Any]], None],
        next_seq: Callable[[], int],
    ) -> None:
        self._write = write
        self._next_seq = next_seq
        self._pending: dict[int, asyncio.Future[Response]] = {}

    async def request(
        self, command: str, arguments: dict[str, Any], timeout: float = 30.0
    ) -> Response:
        seq = self._next_seq()
        future: asyncio.Future[Response] = asyncio.get_running_loop().create_future()
        self._pending[seq] = future
        try:
            self._write(
                Request(seq=seq, command=command, arguments=arguments).to_dict()
            )
            response = await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(seq, None)
        if not response.success:
            raise ReverseRequestError(
                response.message or f"{command} was refused by the client"
            )
        return response

    def route(self, msg: object) -> bool:
        if not isinstance(msg, Response):
            return False
        future = self._pending.get(msg.request_seq)
        if future is None:
            return False
        if not future.done():
            future.set_result(msg)
        return True
```

- [ ] **Step 4: Run** the new test file → PASS.
- [ ] **Step 5: Wire into both servers.** In `PerlDapServer.__init__` and `BashDapServer.__init__` (each already has `self._write` and `_next_seq`): `self._reverse = ReverseRequester(self._write, self._next_seq)` (+ `from tdb.dap.reverse import ReverseRequester`). In BOTH `run()` loops replace

```python
            if not isinstance(msg, Request):
                continue
```

with

```python
            if self._reverse.route(msg):
                await self._writer.drain()
                continue
            if not isinstance(msg, Request):
                continue
```

- [ ] **Step 6: Run** `uv run pytest tests/unit/test_perl_dap_server.py tests/unit -q -k "bash"` and the perl/bash integration files → all pass (no behavior change yet).
- [ ] **Step 7: Commit** — `git commit -m "terminal: ReverseRequester for adapter-to-client DAP requests"`.

---

### Task 5: Bash control channels move to FIFOs (both modes)

Prerequisite for terminal mode: inherited fds don't survive emulator spawning. One channel implementation for both modes.

**Files:**
- Modify: `src/tdb/adapters/bash/session.py` (`launch` ~lines 190-234, `stop` ~line 380)
- Modify: `src/tdb/adapters/bash/tdb_harness.sh` (guard block, ~line 34)
- Test: `tests/integration/test_bash_session.py` (existing suite is the main gate; add one new test)

**Interfaces:**
- Produces: harness env contract becomes `__TDB_CMD_PATH` / `__TDB_RESP_PATH` (FIFO paths in the session tmpdir) + existing `__TDB_TMP`; `BashSession.debuggee_pid: int | None` (from the harness `ready $$ $BASH_VERSION` line). Task 7 relies on both.

- [ ] **Step 1: Write the failing test** (append to `tests/integration/test_bash_session.py`; uses the existing `Recorder` and fixture style of that file):

```python
async def test_harness_connects_over_fifo_paths_and_reports_pid(tmp_path):
    script = tmp_path / "t.sh"
    script.write_text("x=1\nx=2\n")
    rec = Recorder()
    s = BashSession(rec.on_output, rec.on_stop, rec.on_exit)
    try:
        await s.launch(program=str(script), args=[], cwd=str(tmp_path), env=None)
        assert s.debuggee_pid is not None
        os.kill(s.debuggee_pid, 0)  # alive, and it is bash itself
        assert "__TDB_CMD_FD" not in s._launch_env_snapshot
        assert s._launch_env_snapshot["__TDB_CMD_PATH"].endswith("cmd.fifo")
    finally:
        await s.stop()
```

- [ ] **Step 2: Run** it → FAIL (`debuggee_pid` attribute missing / env has fds).
- [ ] **Step 3: Convert `BashSession.launch`.** Replace the pipe/pass_fds block:

```python
        cmd_r, cmd_w = os.pipe()  # adapter writes, bash reads
        resp_r, resp_w = os.pipe()  # bash writes, adapter reads
        os.set_inheritable(cmd_r, True)
        os.set_inheritable(resp_w, True)
        child_env = dict(env or os.environ)
        child_env["BASH_ENV"] = HARNESS
        child_env["__TDB_CMD_FD"] = str(cmd_r)
        child_env["__TDB_RESP_FD"] = str(resp_w)
```

with FIFO creation (adapter opens the command FIFO `O_RDWR` so the harness's read-open never blocks and never sees EOF while the session lives; the response FIFO is opened read-only non-blocking so harness exit still delivers EOF exactly like the pipe did):

```python
        cmd_path = os.path.join(self._tmpdir.name, "cmd.fifo")
        resp_path = os.path.join(self._tmpdir.name, "resp.fifo")
        os.mkfifo(cmd_path, 0o600)
        os.mkfifo(resp_path, 0o600)
        cmd_w = os.open(cmd_path, os.O_RDWR | os.O_CLOEXEC)
        resp_r = os.open(resp_path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        child_env = dict(env or os.environ)
        child_env["BASH_ENV"] = HARNESS
        child_env["__TDB_CMD_PATH"] = cmd_path
        child_env["__TDB_RESP_PATH"] = resp_path
```

Drop `pass_fds=(cmd_r, resp_w)` and the two `os.close(cmd_r)` / `os.close(resp_w)` calls from the spawn; keep `stdin/stdout/stderr` exactly as is. `stdin=asyncio.subprocess.DEVNULL` stays for pipe mode.
- [ ] **Step 4: Patch the harness guard** (top of `tdb_harness.sh`). Replace

```bash
[[ -n ${__TDB_CMD_FD:-} && -n ${__TDB_RESP_FD:-} && -n ${__TDB_TMP:-} ]] || return 0
```

with

```bash
[[ -n ${__TDB_CMD_PATH:-} && -n ${__TDB_RESP_PATH:-} && -n ${__TDB_TMP:-} ]] || return 0
# Open the FIFOs into shell-allocated fds; every later reference goes
# through $__TDB_CMD_FD/$__TDB_RESP_FD exactly as before. The adapter
# already holds the command FIFO open O_RDWR (our read-open cannot
# block) and a read end of the response FIFO (our write-open cannot
# block). Children inherit these fds like they inherited the pipes.
exec {__TDB_CMD_FD}<"$__TDB_CMD_PATH" || return 0
exec {__TDB_RESP_FD}>"$__TDB_RESP_PATH" || return 0
unset __TDB_CMD_PATH __TDB_RESP_PATH
```

(Keep the `unset BASH_ENV` and version check exactly where they are — the version check must run BEFORE the FIFO opens so the "bash >= 4.4" stderr diagnostic still reaches the adapter through the stderr pipe; `exec {var}<file` itself needs 4.1+, fine.)
- [ ] **Step 5: Record the pid.** In `BashSession.__init__` add `self.debuggee_pid: int | None = None`. In `_resp_loop`'s `ready` frame handling (the branch that resolves `self._ready` — find it via `grep -n '"ready"' src/tdb/adapters/bash/session.py` or the `fields[0]` dispatch), add before resolving:

```python
                self.debuggee_pid = int(fields[1]) if len(fields) > 1 else None
```

- [ ] **Step 6: Run the whole bash suite** — `uv run pytest tests/integration/test_bash_session.py tests/integration/test_bash_adapter_breakpoints.py tests/integration/test_bash_adapter_inspection.py tests/integration/test_bash_adapter_launch.py tests/integration/test_bash_adapter_stepping.py tests/integration/test_bash_edge_cases.py tests/unit/test_bash_declares.py tests/unit/test_bash_session_canonical.py tests/unit/test_bash_session_launch.py -q` → all pass. The pre-existing suite passing over FIFOs is the real acceptance test.
- [ ] **Step 7: Commit** — `git commit -m "bash: harness control channels over FIFOs (path-based, emulator-safe)"`.

---

### Task 6: Perl terminal-mode launch

**Files:**
- Modify: `src/tdb/adapters/perl/session.py` (`PerlSession.__init__`, `launch` ~line 95, `wait_exit_code` ~line 74, `stop` ~line 304)
- Modify: `src/tdb/adapters/perl/server.py` (`__init__`, `_on_initialize`, `_on_launch` ~line 165)
- Modify: `tests/integration/perl_adapter_harness.py` (reverse-request hook)
- Test: `tests/integration/test_perl_terminal.py` (new)

**Interfaces:**
- Consumes: `ReverseRequester` (Task 4), `"console"` in launch args (Task 1).
- Produces:
  - `RunInTerminal = Callable[[list[str], str, dict[str, str]], Awaitable[None]]` (module-level alias in `perl/session.py`; args = full command argv, cwd, env)
  - `PerlSession.launch(..., run_in_terminal: RunInTerminal | None = None)`
  - `PerlSession.debuggee_pid: int | None`
  - `AdapterClient.on_reverse_request: Callable[[dict], Awaitable[dict]] | None` in the test harness — Tasks 7's bash test reuses the same pattern.

- [ ] **Step 1: Extend the test harness.** In `tests/integration/perl_adapter_harness.py` add `self.on_reverse_request = None` in `__init__`, and in `_read_loop` after the `elif body["type"] == "response":` branch:

```python
            elif body["type"] == "request":
                asyncio.ensure_future(self._answer_reverse(body))
```

and the method (plus keep a strong ref set `self._reverse_tasks: set = set()` per the repo's task-GC pitfall):

```python
async def _answer_reverse(self, body: dict):
    handler = self.on_reverse_request
    try:
        result = await handler(body) if handler else {}
        ok = handler is not None
    except Exception as e:  # noqa: BLE001
        result, ok = {"message": str(e)}, False
    self.seq += 1
    reply = {
        "seq": self.seq,
        "type": "response",
        "request_seq": body["seq"],
        "command": body["command"],
        "success": ok,
    }
    if ok:
        reply["body"] = result
    else:
        reply["message"] = result.get("message", "unhandled reverse request")
    data = json.dumps(reply).encode()
    self.proc.stdin.write(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
    await self.proc.stdin.drain()
```

(Wrap the `ensure_future` result: `t = asyncio.ensure_future(...); self._reverse_tasks.add(t); t.add_done_callback(self._reverse_tasks.discard)`.)
- [ ] **Step 2: Write the failing integration test** `tests/integration/test_perl_terminal.py`:

```python
"""Terminal-mode (runInTerminal) launches of the perl adapter.

The test IS the DAP client: it receives the adapter's runInTerminal
reverse request and spawns the command itself — no emulator needed.
"""

import asyncio
import shutil
import subprocess

import pytest

from .perl_adapter_harness import AdapterClient

pytestmark = pytest.mark.skipif(
    shutil.which("perl") is None
    or subprocess.run(["perl", "-e", "require v5.18"]).returncode != 0,
    reason="perl >= 5.18 required",
)


class TerminalSpawner:
    def __init__(self):
        self.requests: list[dict] = []
        self.proc = None

    async def __call__(self, body: dict) -> dict:
        self.requests.append(body)
        args = body["arguments"]
        self.proc = await asyncio.create_subprocess_exec(
            *args["args"],
            cwd=args.get("cwd"),
            env=args.get("env") or None,
            stdin=asyncio.subprocess.DEVNULL,
        )
        return {}


@pytest.fixture
async def client():
    c = AdapterClient()
    await c.start()
    yield c
    await c.stop()


async def test_terminal_launch_steps_and_reports_exit_code(client, tmp_path):
    script = tmp_path / "t.pl"
    script.write_text("my $x = 1;\nmy $y = 2;\nexit 7;\n")
    spawner = TerminalSpawner()
    client.on_reverse_request = spawner
    await client.request(
        "initialize",
        {"adapterID": "perl-tdb", "supportsRunInTerminalRequest": True},
    )
    launch_fut = client.send(
        "launch",
        {
            "program": str(script),
            "args": [],
            "cwd": str(tmp_path),
            "stopOnEntry": True,
            "console": "externalTerminal",
        },
    )
    await client.wait_event("initialized")
    await client.request("configurationDone")
    assert (await asyncio.wait_for(launch_fut, 30))["success"] is True
    assert spawner.requests[0]["command"] == "runInTerminal"
    assert spawner.requests[0]["arguments"]["kind"] == "external"
    await client.wait_event("stopped")
    await client.request("next")
    await client.wait_event("stopped")
    await client.request("continue")
    exited = await client.wait_event("exited")
    assert exited["body"]["exitCode"] == 7
    await client.wait_event("terminated")


async def test_terminal_launch_without_capability_fails(client, tmp_path):
    script = tmp_path / "t.pl"
    script.write_text("my $x = 1;\n")
    await client.request("initialize", {"adapterID": "perl-tdb"})
    resp = await client.request(
        "launch",
        {
            "program": str(script),
            "cwd": str(tmp_path),
            "console": "externalTerminal",
        },
    )
    assert resp["success"] is False
    assert "runInTerminal" in resp["message"]
```

- [ ] **Step 3: Run** `uv run pytest tests/integration/test_perl_terminal.py -q` → FAIL.
- [ ] **Step 4: Implement `PerlSession` terminal mode.** In `session.py` add near the top:

```python
RunInTerminal = Callable[[list[str], str, dict[str, str]], Awaitable[None]]
```

`__init__` gains `self.debuggee_pid: int | None = None` and `self._exit_status_path: str | None = None`. In `launch(..., run_in_terminal: RunInTerminal | None = None)`, replace the `create_subprocess_exec` block with a branch (`argv`, `child_env`, `cwd` already built; keep the socket/server setup identical):

```python
        if run_in_terminal is not None:
            # The debuggee is spawned by the client inside a terminal
            # emulator; we cannot reap it, so a /bin/sh wrapper writes
            # $? (128+n for signal deaths) where wait_exit_code() reads.
            status_dir = tempfile.mkdtemp(prefix="tdb-perl-")
            self._exit_status_path = os.path.join(status_dir, "exit-status")
            wrapped = [
                "/bin/sh", "-c",
                f"{shlex.join(argv)}; printf %s $? > "
                f"{shlex.quote(self._exit_status_path)}",
            ]
            await run_in_terminal(wrapped, cwd, child_env)
        else:
            self._process = await asyncio.create_subprocess_exec(
                ... unchanged ...
            )
            self._pump_tasks = [... unchanged ...]
```

(Add `import shlex`, `import tempfile`.) Bump the connect-back timeout: `timeout = 30.0 if run_in_terminal is not None else 15.0` and use it in both `wait_for(server_ready, ...)` and `_await_prompt(...)`, updating the timeout error message to mention the external terminal when in terminal mode. After the helpers `do '...'` command, in terminal mode only, capture the pid:

```python
        if run_in_terminal is not None:
            reply = await self.command("p $$")
            digits = "".join(ch for ch in reply if ch.isdigit())
            self.debuggee_pid = int(digits) if digits else None
```

- [ ] **Step 5: Exit code + stop.** In `wait_exit_code`, before the `self._process is None: return 0` line:

```python
        if self._process is None and self._exit_status_path is not None:
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                try:
                    text = open(self._exit_status_path).read().strip()
                except OSError:
                    text = ""
                if text:
                    return int(text)
                await asyncio.sleep(0.05)
            return -1
```

In `stop()`, after the existing owned-process handling, add the terminal-mode force-kill (plain `os.kill`, NOT killpg — the debuggee shares the emulator's process group; and clean the status dir):

```python
if self._process is None and self.debuggee_pid is not None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(self.debuggee_pid, sig)
        except (ProcessLookupError, PermissionError):
            break
        await asyncio.sleep(0.05)
if self._exit_status_path is not None:
    shutil.rmtree(os.path.dirname(self._exit_status_path), ignore_errors=True)
    self._exit_status_path = None
```

(Add `import shutil`.)
- [ ] **Step 6: Implement the server side.** `PerlDapServer.__init__`: `self._client_supports_run_in_terminal = False`. `_on_initialize`: before sending the response, `self._client_supports_run_in_terminal = bool(request.arguments.get("supportsRunInTerminalRequest"))`. In `_on_launch`, after the preflight and before constructing the session:

```python
run_in_terminal = None
if args.get("console") == "externalTerminal":
    if not self._client_supports_run_in_terminal:
        self.send_error(
            request,
            "externalTerminal launch requires a client that "
            "supports the runInTerminal reverse request",
        )
        return

    async def run_in_terminal(cmd, cwd, env):
        await self._reverse.request(
            "runInTerminal",
            {
                "kind": "external",
                "title": "tdb perl debuggee",
                "cwd": cwd,
                "args": cmd,
                "env": env,
            },
        )
```

and pass `run_in_terminal=run_in_terminal` into `self.session.launch(...)`. A `ReverseRequestError` escaping `launch` is caught by the existing `except PerlProtocolError` — it is NOT one, so extend that except clause: `except (PerlProtocolError, ReverseRequestError) as e:` and use `str(e)` when it has no `.tail` (`getattr(e, "tail", "")`).
- [ ] **Step 7: Run** `uv run pytest tests/integration/test_perl_terminal.py tests/integration/test_perl_adapter_launch.py tests/integration/test_perl_session.py -q` → PASS. Then the full perl set: `uv run pytest tests -q -k "perl"`.
- [ ] **Step 8: Commit** — `git commit -m "perl: externalTerminal launch via runInTerminal"`.

---

### Task 7: Bash terminal-mode launch

**Files:**
- Modify: `src/tdb/adapters/bash/session.py` (`launch`, `_reap`/exit watch, `stop`)
- Modify: `src/tdb/adapters/bash/server.py` (`__init__`, `_on_initialize`, `_on_launch`)
- Test: `tests/integration/test_bash_terminal.py` (new; drive `BashDapServer` end-to-end the same way `tests/integration/test_perl_terminal.py` drives perl — via a module-subprocess client. Copy `perl_adapter_harness.AdapterClient` usage with `module="tdb.adapters.bash"`.)

**Interfaces:**
- Consumes: FIFO channels + `debuggee_pid` (Task 5), `ReverseRequester` (Task 4), harness `ready $$` line.
- Produces: `BashSession.launch(..., run_in_terminal: Callable[[list[str], str, dict[str, str]], Awaitable[None]] | None = None)`.

- [ ] **Step 1: Write the failing integration test** `tests/integration/test_bash_terminal.py` (same `TerminalSpawner` class as the perl test — duplicate it here; the two harness clients differ only in `module=`):

```python
"""Terminal-mode (runInTerminal) launches of the bash adapter."""

import asyncio

import pytest

from .bash_adapter_harness import bash_ok
from .perl_adapter_harness import AdapterClient

pytestmark = pytest.mark.skipif(not bash_ok(), reason="needs bash >= 4.4")


class TerminalSpawner:
    def __init__(self):
        self.requests: list[dict] = []
        self.proc = None

    async def __call__(self, body: dict) -> dict:
        self.requests.append(body)
        args = body["arguments"]
        self.proc = await asyncio.create_subprocess_exec(
            *args["args"],
            cwd=args.get("cwd"),
            env=args.get("env") or None,
            stdin=asyncio.subprocess.DEVNULL,
        )
        return {}


@pytest.fixture
async def client():
    c = AdapterClient()
    await c.start(module="tdb.adapters.bash")
    yield c
    await c.stop()


async def test_terminal_launch_breakpoint_and_exit_code(client, tmp_path):
    script = tmp_path / "t.sh"
    script.write_text("x=1\nx=2\nexit 5\n")
    spawner = TerminalSpawner()
    client.on_reverse_request = spawner
    await client.request("initialize", {"supportsRunInTerminalRequest": True})
    launch_fut = client.send(
        "launch",
        {
            "program": str(script),
            "args": [],
            "cwd": str(tmp_path),
            "stopOnEntry": True,
            "console": "externalTerminal",
        },
    )
    await client.wait_event("initialized")
    await client.request("configurationDone")
    assert (await asyncio.wait_for(launch_fut, 30))["success"] is True
    assert spawner.requests[0]["command"] == "runInTerminal"
    await client.wait_event("stopped")
    await client.request("next")
    await client.wait_event("stopped")
    await client.request("continue")
    exited = await client.wait_event("exited")
    assert exited["body"]["exitCode"] == 5
    await client.wait_event("terminated")
```

- [ ] **Step 2: Run** it → FAIL.
- [ ] **Step 3: Implement `BashSession` terminal mode.** `launch()` gains `run_in_terminal=None` parameter. After the FIFO/env setup (Task 5) branch the spawn:

```python
        if run_in_terminal is not None:
            self._exit_status_path = os.path.join(
                self._tmpdir.name, "exit-status"
            )
            command = [bash_path, program, *args]
            wrapped = [
                "/bin/sh", "-c",
                f"{shlex.join(command)}; printf %s $? > "
                f"{shlex.quote(self._exit_status_path)}",
            ]
            await run_in_terminal(wrapped, cwd, child_env)
        else:
            self._process = await asyncio.create_subprocess_exec(
                ... unchanged ...
            )
```

(`import shlex`; `self._exit_status_path: str | None = None` in `__init__`.) The task list changes: `_pump` tasks and `_reap` only exist when `self._process is not None`; in terminal mode start `asyncio.create_task(self._terminal_exit_watch())` instead. Ready timeout: `await asyncio.wait_for(self._ready, 30.0 if run_in_terminal is not None else 15.0)`, and extend the timeout message with "— did the external terminal window open?" in terminal mode.
- [ ] **Step 4: Terminal exit watch.** New method on `BashSession` — the response FIFO delivers EOF when bash and its fd-inheriting children are gone (`_resp_loop` returns), then the wrapper's status file gives the code:

```python
async def _terminal_exit_watch(self) -> None:
    await self._tasks[0]  # _resp_loop: returns on response-FIFO EOF
    code = -1
    deadline = asyncio.get_running_loop().time() + 2.0
    while asyncio.get_running_loop().time() < deadline:
        try:
            text = open(self._exit_status_path).read().strip()
        except OSError:
            text = ""
        if text:
            code = int(text)
            break
        await asyncio.sleep(0.05)
    self.exit_code = code
    if self._ready and not self._ready.done():
        self._ready.set_exception(
            BashProtocolError(
                "bash exited before the harness reported ready (external terminal)"
            )
        )
        return
    if self._pending and not self._pending.done():
        self._pending.set_exception(BashProtocolError("debuggee exited"))
    self._on_exit(code)
```

Order the `self._tasks` list in terminal mode as `[resp_loop_task, exit_watch_task]` so index 0 stays `_resp_loop` (match the existing pipe-mode convention where `_tasks[0]` is `_resp_loop`).
- [ ] **Step 5: `stop()` terminal path.** After the existing owned-process branch: when `self._process is None and self.debuggee_pid is not None`, send `os.kill(self.debuggee_pid, signal.SIGTERM)` then `SIGKILL` exactly as in the perl Task 6 Step 5 code block (plain kill, not killpg — the debuggee shares the emulator's group).
- [ ] **Step 6: Server side.** Mirror perl Task 6 Step 6 in `BashDapServer`: `_client_supports_run_in_terminal` recorded in `_on_initialize` (this server's `_on_initialize` currently ignores `request.arguments` — read them); `_on_launch` builds the same `run_in_terminal` closure via `self._reverse.request("runInTerminal", {...,"title": "tdb bash debuggee"})` and errors when the capability is absent; extend the launch `except BashProtocolError` to also catch `ReverseRequestError`.
- [ ] **Step 7: Run** `uv run pytest tests/integration/test_bash_terminal.py -q` → PASS; then `uv run pytest tests -q -k "bash"` → all pass.
- [ ] **Step 8: Commit** — `git commit -m "bash: externalTerminal launch via runInTerminal"`.

---

### Task 8: Guardian path-based channels, pid line, exit-status line

**Files:**
- Modify: `src/tdb/adapters/tcsh/guardian.py` (`main()` argument loop ~line 42; status reporting ~line 96)
- Test: `tests/unit/test_tcsh_guardian.py`

**Interfaces:**
- Produces: guardian accepts `--status-path <fifo>` / `--control-path <fifo>` (mutually exclusive with the `-fd` forms); in path mode it writes, in order: `armed`, `ok`, `pid <guardianpid>`, then normal status traffic, and finally `exit <code>` or `signal <n>` immediately before exiting. Task 9's session consumes this exact sequence.

- [ ] **Step 1: Write the failing test** (append to `tests/unit/test_tcsh_guardian.py`; reuse its `read_descriptor_line` helper):

```python
@pytest.mark.asyncio
async def test_guardian_path_mode_handshake_pid_and_exit_status(
    tmp_path: Path,
) -> None:
    status_path = tmp_path / "status.fifo"
    control_path = tmp_path / "control.fifo"
    os.mkfifo(status_path, 0o600)
    os.mkfifo(control_path, 0o600)
    status_reader = os.open(status_path, os.O_RDONLY | os.O_NONBLOCK)
    control_writer = os.open(control_path, os.O_RDWR)
    guardian_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "tdb"
        / "adapters"
        / "tcsh"
        / "guardian.py"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(guardian_path),
        "--status-path",
        str(status_path),
        "--control-path",
        str(control_path),
        "--",
        "/bin/sh",
        "-c",
        "exit 3",
        cwd=str(tmp_path),
        stdin=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        line = await asyncio.wait_for(
            asyncio.to_thread(read_descriptor_line, status_reader), 5
        )
        assert line == b"armed\n"
        os.write(control_writer, b"start\n")
        assert (
            await asyncio.wait_for(
                asyncio.to_thread(read_descriptor_line, status_reader), 5
            )
            == b"ok\n"
        )
        pid_line = await asyncio.wait_for(
            asyncio.to_thread(read_descriptor_line, status_reader), 5
        )
        assert pid_line == f"pid {process.pid}\n".encode()
        assert (
            await asyncio.wait_for(
                asyncio.to_thread(read_descriptor_line, status_reader), 5
            )
            == b"exit 3\n"
        )
        assert await asyncio.wait_for(process.wait(), 5) == 3
    finally:
        os.close(status_reader)
        os.close(control_writer)
        if process.returncode is None:
            process.kill()
            await process.wait()
```

Also a signal-death case: same setup, child `"kill -KILL $$"` via `/bin/sh -c`, expect `b"signal 9\n"` after `pid`, and `process.returncode == -signal.SIGKILL`.
- [ ] **Step 2: Run** both → FAIL (unknown option consumed as command).
- [ ] **Step 3: Implement in `guardian.main()`.** Extend the option loop to accept the path forms (`while len(arguments) >= 2 and arguments[0] in {"--status-fd", "--control-fd", "--status-path", "--control-path"}`), opening FIFOs (order matters — the adapter opens its status read end and holds the control FIFO O_RDWR before spawning, so neither open blocks):

```python
        elif option == "--status-path":
            status_descriptor = os.open(value, os.O_WRONLY)
            status_from_path = True
        elif option == "--control-path":
            control_descriptor = os.open(value, os.O_RDONLY)
```

(`status_from_path = False` initialized before the loop; the int() parse only applies to the `-fd` forms.) Then report `pid` right after the existing `"ok"` report:

```python
    _report_status(status_descriptor, "ok", close=control_descriptor is None)
    if status_from_path:
        _report_status(status_descriptor, f"pid {os.getpid()}", close=False)
```

and just before each `return returncode` / the signal re-raise at the end of `main()`, in path mode only:

```python
    if status_from_path:
        if returncode < 0:
            _report_status(status_descriptor, f"signal {-returncode}", close=False)
        else:
            _report_status(status_descriptor, f"exit {returncode}", close=False)
```

(Place this once, right after the `returncode` is final and before the `if returncode < 0:` re-raise block, so both exit shapes emit it. Do NOT call `os.setsid()` — the emulator makes the command a session leader with the window's tty as controlling terminal, which the drain/terminate logic keys on and which keeps ^C working.)
- [ ] **Step 4: Run** the new tests + `uv run pytest tests/unit/test_tcsh_guardian.py -q` → all pass.
- [ ] **Step 5: Commit** — `git commit -m "tcsh: guardian --status-path/--control-path with pid and exit-status lines"`.

---

### Task 9: Tcsh session + server terminal mode

**Files:**
- Modify: `src/tdb/adapters/tcsh/session.py` (`LaunchConfig`, `DebugSession.__init__`, `start()` ~line 247, `_monitor_process` area ~line 792, `_request_guardian_termination` ~line 942, `_stop_process_group`)
- Modify: `src/tdb/adapters/tcsh/server.py` (`__init__`, `_initialize` ~line 251, `_launch_config` ~line 434, response routing in `_handle_request` ~line 160, reverse-request sender)
- Modify: `tests/integration/tcsh_dap_client.py` (runInTerminal handler)
- Test: `tests/integration/test_tcsh_terminal.py` (new), `tests/unit/test_tcsh_session.py`, `tests/unit/test_tcsh_server.py`

**Interfaces:**
- Consumes: guardian path mode + `pid`/`exit`/`signal` lines (Task 8), `"console"` in launch body (Task 1).
- Produces:
  - `LaunchConfig` gains `external_terminal: bool = False`
  - `DebugSession.__init__(..., run_in_terminal: Callable[[list[str], str, dict[str, str]], Awaitable[None]] | None = None)`
  - `DAPServer._send(message) -> int` (returns the stamped seq)
  - `DAPServer` answers client `Response` messages by resolving its pending reverse request.

- [ ] **Step 1: Extend the tcsh test DAP client.** In `tests/integration/tcsh_dap_client.py` `__init__`: `self.on_reverse_request = None` and `self._spawned: list = []`. In `_collect_messages`, after appending the message and before the response/event classification, add:

```python
                if message.get("type") == "request":
                    await self._answer_reverse(message)
                    continue
```

with:

```python
    async def _answer_reverse(self, message: dict[str, object]) -> None:
        handler = self.on_reverse_request
        success = handler is not None
        body: dict[str, object] = {}
        failure = "no reverse-request handler installed"
        if handler is not None:
            try:
                body = await handler(message)
            except Exception as error:  # noqa: BLE001
                success, failure = False, str(error)
        reply: dict[str, object] = {
            "seq": self._next_seq,
            "type": "response",
            "request_seq": message["seq"],
            "command": message["command"],
            "success": success,
        }
        self._next_seq += 1
        if success:
            reply["body"] = body
        else:
            reply["message"] = failure
        assert self.process.stdin is not None
        self.process.stdin.write(encode_message(reply))
        await self.process.stdin.drain()
```

- [ ] **Step 2: Write the failing integration test** `tests/integration/test_tcsh_terminal.py`:

```python
"""Terminal-mode (runInTerminal) launches of the tcsh adapter."""

import asyncio

import pytest

from tests.integration.tcsh_dap_client import DAPClient
from tests.integration.test_tcsh_adapter import configure, stack_frames


@pytest.mark.asyncio
async def test_terminal_launch_steps_and_reports_exit_code(
    dap_client: DAPClient, tcsh_path, tcsh_fixtures_dir, tmp_path
) -> None:
    program = tmp_path / "t.csh"
    program.write_text("set x = 1\nset y = 2\nexit 4\n")
    spawned: list[asyncio.subprocess.Process] = []

    async def spawn(message):
        args = message["arguments"]
        assert args["kind"] == "external"
        proc = await asyncio.create_subprocess_exec(
            *args["args"],
            cwd=args.get("cwd"),
            env=args.get("env") or None,
            stdin=asyncio.subprocess.DEVNULL,
        )
        spawned.append(proc)
        return {}

    dap_client.on_reverse_request = spawn
    await dap_client.request(
        "initialize",
        {"adapterID": "tcsh", "supportsRunInTerminalRequest": True},
    )
    await dap_client.wait_for_event("initialized")
    await dap_client.launch(
        program, tcshPath=str(tcsh_path), console="externalTerminal"
    )
    await configure(dap_client)
    stopped = await dap_client.wait_for_event("stopped")
    assert stopped["body"]["reason"] == "entry"
    frames = await stack_frames(dap_client)
    assert frames[0]["line"] == 1
    assert (await dap_client.request("next", {"threadId": 1}))["success"]
    await dap_client.wait_for_event("stopped")
    assert (await dap_client.request("continue", {"threadId": 1}))["success"]
    exited = await dap_client.wait_for_event("exited", timeout=15)
    assert exited["body"]["exitCode"] == 4
    await dap_client.wait_for_event("terminated")
    assert spawned and await spawned[0].wait() == 4


@pytest.mark.asyncio
async def test_terminal_launch_without_capability_fails(
    dap_client: DAPClient, tcsh_path, tmp_path
) -> None:
    program = tmp_path / "t.csh"
    program.write_text("set x = 1\n")
    await dap_client.initialize()
    response = await dap_client.request(
        "launch",
        {
            "program": str(program),
            "tcshPath": str(tcsh_path),
            "console": "externalTerminal",
        },
    )
    assert response["success"] is False
    assert "runInTerminal" in response["message"]
```

(The DAPClient's `initialize()` helper sends no capability — use the raw `request` form for the capable case, as shown.)
- [ ] **Step 3: Run** → FAIL.
- [ ] **Step 4: Session changes** (`src/tdb/adapters/tcsh/session.py`):
  - `LaunchConfig` gains `external_terminal: bool = False`.
  - `DebugSession.__init__` gains keyword `run_in_terminal: Callable[[list[str], str, dict[str, str]], Awaitable[None]] | None = None`, stored as `self._run_in_terminal`.
  - In `start()`, branch after `tcsh_argv`/`environment` are built. Terminal path — create the guardian FIFOs in the session workspace, open our ends FIRST (status read non-blocking; control O_RDWR so the guardian's read-open never blocks and EOF semantics match the pipe), send the reverse request, then run the SAME handshake code:

```python
        if self.config.external_terminal:
            assert self._run_in_terminal is not None
            assert self.workspace is not None
            status_path = self.workspace / "guardian-status.fifo"
            control_path = self.workspace / "guardian-control.fifo"
            os.mkfifo(status_path, 0o600)
            os.mkfifo(control_path, 0o600)
            self._guardian_status_descriptor = os.open(
                status_path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC
            )
            self._guardian_control_descriptor = os.open(
                control_path, os.O_RDWR | os.O_CLOEXEC
            )
            control_writer = self._guardian_control_descriptor
            argv = (
                sys.executable, str(guardian),
                "--status-path", str(status_path),
                "--control-path", str(control_path),
                "--", *tcsh_argv,
            )
            await self._run_in_terminal(
                list(argv), str(self.config.cwd), environment
            )
        else:
            ... existing pipe/pass_fds spawn, unchanged ...
```

  After the `"ok"` status check, in terminal mode read one more line and adopt the guardian's identity (replacing the `self.process.pid`-derived values):

```python
            if self.config.external_terminal:
                pid_line = await self._read_guardian_status()
                if not pid_line.startswith(b"pid "):
                    raise OSError("guardian did not report its pid")
                guardian_pid = int(pid_line.split()[1])
                self._process_group_id = guardian_pid
                self._process_session_id = guardian_pid
```

  (In terminal mode `self.process` stays `None`; audit `start()`'s error paths — `_abort_launched_process` already tolerates `process is None`.)
  - Replace the tail of `start()`: `self._wait_task = asyncio.create_task(self._monitor_process() if self.process is not None else self._monitor_terminal())`.
  - New `_monitor_terminal()` — reads guardian status lines until the exit line; forwards any interleaved termination-ack lines to `_request_guardian_termination` via a queue (`self._guardian_ack_queue: asyncio.Queue[bytes]` created in terminal-mode `start()`):

```python
    async def _monitor_terminal(self) -> None:
        try:
            while True:
                line = await self._read_guardian_status()
                if line.startswith(b"exit "):
                    returncode = int(line.split()[1])
                    break
                if line.startswith(b"signal "):
                    returncode = -int(line.split()[1])
                    break
                if line == b"":
                    returncode = -1  # status FIFO closed with no report
                    break
                await self._guardian_ack_queue.put(line)
            await self._emit_process_termination(returncode)
        except BaseException as error:  # noqa: BLE001
            self.failure = self.failure or error
            self.state = SessionState.TERMINATED
            raise
```

  - In `_request_guardian_termination`, read the ack from the queue in terminal mode instead of the raw descriptor (the monitor owns the descriptor there):

```python
            if self._guardian_ack_queue is not None:
                status = await asyncio.wait_for(
                    self._guardian_ack_queue.get(),
                    timeout=_TERMINATE_TIMEOUT_SECONDS + 1,
                )
            else:
                status = await asyncio.wait_for(
                    self._read_guardian_status(),
                    timeout=_TERMINATE_TIMEOUT_SECONDS + 1,
                )
```

  - `_stop_process_group`: its `process is None: return 0` early-return must, in terminal mode, still write `terminate` to the guardian control descriptor when one exists — route on `self._guardian_control_descriptor is not None` rather than `process is None` (follow the existing method structure; the killpg fallback already uses `_process_group_id`, which now holds the guardian pid).
- [ ] **Step 5: Server changes** (`src/tdb/adapters/tcsh/server.py`):
  - `_send` returns the stamped seq (`return framed["seq"]` — adjust its annotation to `-> int`).
  - `__init__`: `self._client_supports_run_in_terminal = False` and `self._reverse_pending: dict[int, asyncio.Future[Mapping[str, object]]] = {}`.
  - `_initialize`: `self._client_supports_run_in_terminal = bool(arguments.get("supportsRunInTerminalRequest"))`.
  - `_handle_request`: BEFORE the `request.get("type") != "request"` rejection, route responses:

```python
        if request.get("type") == "response":
            future = self._reverse_pending.pop(int(request.get("request_seq", -1)), None)
            if future is not None and not future.done():
                future.set_result(request)
            return
```

  - New method:

```python
async def _send_reverse_request(
    self, command: str, arguments: Mapping[str, object]
) -> None:
    future: asyncio.Future[Mapping[str, object]] = (
        asyncio.get_running_loop().create_future()
    )
    seq = await self._send(
        {"type": "request", "command": command, "arguments": dict(arguments)}
    )
    self._reverse_pending[seq] = future
    try:
        response = await asyncio.wait_for(future, 30.0)
    finally:
        self._reverse_pending.pop(seq, None)
    if not response.get("success"):
        raise LaunchError(str(response.get("message") or f"{command} was refused"))
```

    NOTE an ordering subtlety: register the future in `_reverse_pending` BEFORE awaiting `_send` (compute nothing from the seq before send returns — restructure: `_send` acquires the write lock, so the response cannot arrive before `_send` returns from `drain()`... it CAN arrive before the `self._reverse_pending[seq] = future` line runs on this task. Register defensively: change `_send` usage to a two-step — take the seq under the same lock. Simplest correct form: make `_send` return the seq, then `self._reverse_pending[seq] = future` immediately after, and have the router (`_handle_request`) buffer unmatched responses: keep `self._unmatched_responses: dict[int, Mapping[str, object]] = {}`; router stores there when no future is pending; `_send_reverse_request` checks `_unmatched_responses.pop(seq, None)` before awaiting. Implement it exactly so.)
  - `_launch` / `_launch_config`: read `console`; when `externalTerminal`, require the capability (`RequestError` with "externalTerminal launch requires a client that supports the runInTerminal reverse request") and build the config with `external_terminal=True`; construct the session as `self._session_factory(config, sink)` then set its callback:

```python
if config.external_terminal:

    async def run_in_terminal(cmd, cwd, env):
        await self._send_reverse_request(
            "runInTerminal",
            {
                "kind": "external",
                "title": "tdb tcsh debuggee",
                "cwd": cwd,
                "args": cmd,
                "env": env,
            },
        )

    session._run_in_terminal = run_in_terminal
```

    (Setting the attribute post-construction keeps the `SessionFactory` signature — and every existing test factory — unchanged.)
- [ ] **Step 6: Run** `uv run pytest tests/integration/test_tcsh_terminal.py -q` → PASS. Then the full tcsh set: `uv run pytest tests -q -k "tcsh"` → all pass (the fd-mode paths must be untouched).
- [ ] **Step 7: Unit tests for the seams** (append to `tests/unit/test_tcsh_server.py`): initialize records the capability; a `launch` with `console: "externalTerminal"` and no capability fails with the exact message; a stray client `response` message is consumed without an "unsupported command" error reply. Follow that file's existing black-box or handler-level style.
- [ ] **Step 8: Commit** — `git commit -m "tcsh: externalTerminal launch via runInTerminal + guardian path mode"`.

---

### Task 10: Window-close semantics, README, full verification

**Files:**
- Modify: `tests/integration/test_perl_terminal.py`, `tests/integration/test_bash_terminal.py`, `tests/integration/test_tcsh_terminal.py` (one test each)
- Modify: `README.md` (the `--terminal` section)
- Test: full suite, both platforms

**Interfaces:** none new.

- [ ] **Step 1: Write the window-close tests** — after a launch reaches its first stop, kill the client-spawned process tree (`spawner.proc.kill()` — SIGKILL on the `sh`/guardian wrapper approximates the window closing) and assert the adapter emits `terminated` (and `exited`, code `-1` or the real signal code) within 15 s instead of hanging. One per language; run them → they should pass with the machinery already built. If any hangs, fix the EOF path it exposes before proceeding.
- [ ] **Step 2: TerminalLauncher fake-emulator test.** Add to `tests/unit/` (new file `tests/unit/test_terminal_launcher.py` if none exists; check for an existing one first with `grep -rn "TerminalLauncher" tests/`): monkeypatch `tdb.session.terminal._TERMINAL_SPECS` with `{"fakeem": ("fakeem", ["-e"])}` and put a `fakeem` script on PATH (write to `tmp_path`, `chmod +x`, monkeypatch `PATH`):

```python
FAKE_EMULATOR = """#!/bin/sh
# records argv then execs the payload after the -e flag
printf '%s\\n' "$@" > "$FAKEEM_LOG"
shift   # drop -e
exec "$@"
"""


async def test_launcher_wraps_command_in_emulator(tmp_path, monkeypatch):
    log = tmp_path / "argv.log"
    exe = tmp_path / "fakeem"
    exe.write_text(FAKE_EMULATOR)
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKEEM_LOG", str(log))
    monkeypatch.setattr(
        "tdb.session.terminal._TERMINAL_SPECS",
        {"fakeem": ("fakeem", ["-e"])},
    )
    marker = tmp_path / "ran"
    launcher = TerminalLauncher("fakeem")
    request = Request(
        seq=1,
        command="runInTerminal",
        arguments={
            "args": ["/bin/sh", "-c", f"echo done > {marker}"],
            "cwd": str(tmp_path),
        },
    )
    body = await launcher.handle_run_in_terminal(request)
    assert body == {}
    deadline = asyncio.get_running_loop().time() + 5
    while not marker.exists():
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.02)
    assert log.read_text().splitlines()[0] == "-e"
```

Run it → PASS (this is existing behavior; the test pins it for the new multi-language consumers).

- [ ] **Step 3: README.** Update the `--terminal` documentation: works for Python, Perl, Bash, Tcsh, and C/C++ with `--adapter lldb-dap`; gdb is not supported (error message points at lldb-dap); attach modes reject it; debuggee I/O (including stdin) happens in the external window. Mention it in the per-language limitation lists where those exist (remove/adjust any "Python-only" phrasing).
- [ ] **Step 4: Full verification.** `ruff check src tests` clean; host: `uv run pytest -q -p no:cacheprovider --no-cov` all pass; CI parity: `docker build --target test .` succeeds (tcsh/bash/perl terminal tests run headlessly there; remember there is no init reaper — the Global Constraints zombie rule applies).
- [ ] **Step 5: Manual smoke test** (requires a local X session): `uv run tdb --terminal xterm examples/<any>.py`, then the same with a `.pl`, `.sh`, and `.csh` script, and a C binary with `--adapter lldb-dap`; verify a window opens, output appears there (not in the Console View), stepping works, exit closes the session cleanly; verify `^C` in the window interrupts a sleeping debuggee. Record results in the final report to the user (do not skip; this is the only test with a real emulator).
- [ ] **Step 6: Commit** — `git commit -m "terminal: window-close tests + README for all-language --terminal"`.

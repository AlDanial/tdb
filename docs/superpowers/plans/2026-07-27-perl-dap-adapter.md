# Perl DAP Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Debug Perl programs from tdb via a bundled Python DAP adapter that drives stock `perl -d` (perl5db) for execution control and injects JSON-returning helper subs for all data extraction, including debugpy-style remote attach (`listen()` / `wait_for_client()`).

**Architecture:** Three processes: tdb ↔ (DAP over stdio) ↔ adapter (`python -m tdb.adapters.perl`) ↔ (TCP text channel) ↔ perl5db inside the debuggee. The adapter parses exactly one thing from the perl5db socket — the `DB<n>` prompt — plus `TDB>>>{json}<<<TDB` marked lines emitted by injected helpers (package `Devel::TdbHelper`). Remote attach ships a debuggee-side `Devel::TdbRemote` module; the adapter connects out to it. tdb integration is a `LanguageProfile` (`languages/perl.py`) mirroring `cpp.py`.

**Tech Stack:** Python 3.12 + asyncio (adapter), Perl ≥ 5.18 + JSON::PP + core B (helpers), existing `tdb.dap.protocol`/`tdb.dap.messages` for DAP framing.

**Spec:** `docs/superpowers/specs/2026-07-27-perl-dap-adapter-design.md` — read it first.

## Global Constraints

- Repo: `/home/al/projects/tdbg/work`, branch `perl_dap`. Python venv: ALWAYS `.venv/bin/pytest` (bare `pytest` on PATH lacks pytest-cov and dies on addopts) and `.venv/bin/python`.
- Commit ONLY the explicit paths named in each commit step. NEVER `git add -A`, `git add .`, or `git commit -a` — the working tree contains the user's personal untracked files.
- Perl floor: 5.18. Tests that need perl use the module-level skip shown in Task 5 and must skip cleanly when perl is missing or too old.
- Adapter id: `"perl-tdb"`. Language id: `"perl"`. Helper JSON marker: `TDB>>>...<<<TDB`. Helper protocol version: integer `1`. Prompt regex (the parser's ONLY perl5db regex): `rb"\r?\n?\s*DB<+\d+>+ $"`.
- perl5db command vocabulary (complete — nothing else is ever sent): `b <line> [cond]`, `b <file>:<line> [cond]`, `B <line>`, `n`, `s`, `r`, `c`, `do '<path>'`, and helper-call expressions. Output of human commands (`T`, `V`, `y`, `.`) is never parsed.
- The plan text is authoritative for interfaces; where the live repo differs in details (e.g. helper param names), adapt with disclosure in your report.
- Unix (Linux/macOS) only for v1 testing; do not introduce constructs that are Windows-impossible (no Unix sockets, no fcntl).
- A PostToolUse formatter hook may reformat files after edits; if an Edit old_string stops matching, re-Read the file.

---

### Task 1: `to_dict()` for Response and Event

The adapter is a DAP *server*: it sends responses and events. `tdb/dap/messages.py` has `Request.to_dict()` but `Response`/`Event` only deserialize.

**Files:**
- Modify: `src/tdb/dap/messages.py`
- Test: `tests/unit/test_dap_messages.py` (create if absent; append if present)

**Interfaces:**
- Produces: `Response.to_dict() -> dict` (keys: seq, type="response", request_seq, command, success; body only if non-empty; message only if not None). `Event.to_dict() -> dict` (keys: seq, type="event", event; body only if non-empty). Task 4's server calls both.

- [ ] **Step 1: Write the failing tests**

Append to (or create) `tests/unit/test_dap_messages.py`:

```python
from tdb.dap.messages import Event, Response


def test_response_to_dict_round_trips():
    resp = Response(seq=3, request_seq=1, command="initialize",
                    success=True, body={"supportsConfigurationDoneRequest": True})
    d = resp.to_dict()
    assert d == {
        "seq": 3, "type": "response", "request_seq": 1,
        "command": "initialize", "success": True,
        "body": {"supportsConfigurationDoneRequest": True},
    }
    assert Response.from_dict(d) == resp


def test_response_to_dict_error_message():
    resp = Response(seq=4, request_seq=2, command="launch",
                    success=False, message="perl not found")
    d = resp.to_dict()
    assert d["message"] == "perl not found"
    assert "body" not in d


def test_event_to_dict_round_trips():
    ev = Event(seq=5, event="stopped", body={"reason": "step", "threadId": 1})
    d = ev.to_dict()
    assert d == {"seq": 5, "type": "event", "event": "stopped",
                 "body": {"reason": "step", "threadId": 1}}
    assert Event.from_dict(d) == ev
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_dap_messages.py -q`
Expected: FAIL / AttributeError: 'Response' object has no attribute 'to_dict'

- [ ] **Step 3: Implement**

In `src/tdb/dap/messages.py`, add to `Response`:

```python
    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "seq": self.seq,
            "type": "response",
            "request_seq": self.request_seq,
            "command": self.command,
            "success": self.success,
        }
        if self.body:
            d["body"] = self.body
        if self.message is not None:
            d["message"] = self.message
        return d
```

and to `Event`:

```python
    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"seq": self.seq, "type": "event", "event": self.event}
        if self.body:
            d["body"] = self.body
        return d
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_dap_messages.py -q`
Expected: PASS. Also run the full unit suite once: `.venv/bin/pytest tests/unit -q` → all pass.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/dap/messages.py tests/unit/test_dap_messages.py
git commit -m "feat: serialize DAP Response/Event for the adapter server side"
```

---

### Task 2: perl5db stream parser

Incremental parser for the socket byte stream: emits marked-JSON events, prompt events, and passthrough text. This is the ONLY place perl5db output is interpreted.

**Files:**
- Create: `src/tdb/adapters/__init__.py` (empty), `src/tdb/adapters/perl/__init__.py` (empty), `src/tdb/adapters/perl/protocol.py`
- Test: `tests/unit/test_perl_protocol.py`

**Interfaces:**
- Produces: `StreamParser` with `feed(data: bytes) -> list[tuple[str, object]]`. Tuples are `("json", dict)`, `("prompt", None)`, `("text", str)`. Task 8's `PerlSession` consumes it. Module constants `PROMPT_RE`, `MARK_RE` as in Global Constraints.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_perl_protocol.py`:

```python
from tdb.adapters.perl.protocol import StreamParser


def test_prompt_detected():
    p = StreamParser()
    assert p.feed(b"  DB<1> ") == [("prompt", None)]


def test_marked_json_then_prompt():
    p = StreamParser()
    events = p.feed(b'TDB>>>{"a": 1}<<<TDB\n  DB<2> ')
    assert events == [("json", {"a": 1}), ("prompt", None)]


def test_marker_split_across_chunks():
    p = StreamParser()
    assert p.feed(b'TDB>>>{"file": "t') == []
    events = p.feed(b'.pl"}<<<TDB\n  DB<3> ')
    assert events == [("json", {"file": "t.pl"}), ("prompt", None)]


def test_chatter_is_text():
    p = StreamParser()
    events = p.feed(b"main::(t.pl:3):\tmy $x = 1;\n  DB<1> ")
    assert events == [("text", "main::(t.pl:3):\tmy $x = 1;\n"), ("prompt", None)]


def test_prompt_split_across_chunks():
    p = StreamParser()
    assert p.feed(b"  DB<1") == []
    assert p.feed(b"> ") == [("prompt", None)]


def test_nested_prompt_numbers():
    # perl5db uses DB<<2>> style inside nested evals
    p = StreamParser()
    assert p.feed(b"  DB<<2>> ") == [("prompt", None)]


def test_invalid_json_in_marker_is_text():
    p = StreamParser()
    events = p.feed(b"TDB>>>not json<<<TDB\n  DB<1> ")
    assert events[0][0] == "text"
    assert events[-1] == ("prompt", None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_perl_protocol.py -q`
Expected: FAIL with ModuleNotFoundError

- [ ] **Step 3: Implement**

Create the two empty `__init__.py` files, then `src/tdb/adapters/perl/protocol.py`:

```python
"""Incremental parser for the perl5db socket stream.

Contract: the adapter interprets exactly two shapes — the perl5db
prompt (end of a command's response / an unsolicited stop) and
TDB>>>{json}<<<TDB lines printed by the injected helpers. Everything
else is passthrough text.
"""

from __future__ import annotations

import json
import re

PROMPT_RE = re.compile(rb"\r?\n?\s*DB<+\d+>+ $")
MARK_RE = re.compile(rb"TDB>>>(.*?)<<<TDB\r?\n?", re.DOTALL)
# Longest prefix that could still grow into a marker or prompt; used to
# hold back tail bytes instead of flushing them as text prematurely.
_HOLD_RE = re.compile(rb"(?:TDB>?>?>?[^<]*(?:<(?:<(?:TDB?)?)?)?|\r?\n?\s*DB<*\d*>*\x20?)$")

Event = tuple[str, object]


class StreamParser:
    def __init__(self) -> None:
        self._buf = b""

    def feed(self, data: bytes) -> list[Event]:
        self._buf += data
        events: list[Event] = []
        while True:
            m = MARK_RE.search(self._buf)
            if m is None:
                break
            before = self._buf[: m.start()]
            if before:
                events.append(("text", before.decode("utf-8", errors="replace")))
            try:
                events.append(("json", json.loads(m.group(1).decode("utf-8"))))
            except (ValueError, UnicodeDecodeError):
                events.append(
                    ("text", m.group(0).decode("utf-8", errors="replace"))
                )
            self._buf = self._buf[m.end() :]
        pm = PROMPT_RE.search(self._buf)
        if pm is not None:
            before = self._buf[: pm.start()]
            if before:
                events.append(("text", before.decode("utf-8", errors="replace")))
            events.append(("prompt", None))
            self._buf = self._buf[pm.end() :]
        else:
            hold = _HOLD_RE.search(self._buf)
            flush_upto = hold.start() if hold else len(self._buf)
            if flush_upto:
                events.append(
                    ("text", self._buf[:flush_upto].decode("utf-8", errors="replace"))
                )
                self._buf = self._buf[flush_upto:]
        return events
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_perl_protocol.py -q`
Expected: 7 passed. If `test_chatter_is_text` produces two "text" events instead of one, merge adjacent text events before returning (add a small `_coalesce(events)` at the end of `feed`) — the tests define the contract.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/adapters/__init__.py src/tdb/adapters/perl/__init__.py src/tdb/adapters/perl/protocol.py tests/unit/test_perl_protocol.py
git commit -m "feat: perl5db stream parser (prompt + marked-JSON events)"
```

---

### Task 3: variables-reference registry

Maps DAP `variablesReference` ints to what they denote: a scope root (frame + kind) or a debuggee-side expandable object (helper id). Reset on every stop.

**Files:**
- Create: `src/tdb/adapters/perl/refs.py`
- Test: `tests/unit/test_perl_refs.py`

**Interfaces:**
- Produces: `RefRegistry` with `add_scope(frame: int, kind: str) -> int`, `add_object(helper_id: int) -> int`, `get(ref: int) -> dict | None` (returns `{"kind": "scope", "frame": int, "scope": str}` or `{"kind": "object", "helper_id": int}`), `reset() -> None`. Ids start at 1 and are never 0. Tasks 11 consumes.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_perl_refs.py`:

```python
from tdb.adapters.perl.refs import RefRegistry


def test_scope_and_object_refs_round_trip():
    reg = RefRegistry()
    r1 = reg.add_scope(0, "lexicals")
    r2 = reg.add_object(17)
    assert r1 != r2 and r1 > 0 and r2 > 0
    assert reg.get(r1) == {"kind": "scope", "frame": 0, "scope": "lexicals"}
    assert reg.get(r2) == {"kind": "object", "helper_id": 17}


def test_unknown_ref_returns_none():
    assert RefRegistry().get(99) is None


def test_reset_clears_and_restarts_ids():
    reg = RefRegistry()
    reg.add_scope(0, "globals")
    reg.reset()
    assert reg.get(1) is None
    assert reg.add_scope(1, "specials") == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_perl_refs.py -q` → ModuleNotFoundError

- [ ] **Step 3: Implement**

Create `src/tdb/adapters/perl/refs.py`:

```python
"""variablesReference bookkeeping for the Perl adapter.

DAP hands out integer handles; the debuggee-side helper keeps its own
stash of expandable refs (cleared per stop). This registry maps DAP
handles to either a scope root or a helper stash id. Reset on every
stop so stale handles can't resolve to freed objects.
"""

from __future__ import annotations


class RefRegistry:
    def __init__(self) -> None:
        self._next = 1
        self._entries: dict[int, dict] = {}

    def _add(self, entry: dict) -> int:
        ref = self._next
        self._next += 1
        self._entries[ref] = entry
        return ref

    def add_scope(self, frame: int, kind: str) -> int:
        return self._add({"kind": "scope", "frame": frame, "scope": kind})

    def add_object(self, helper_id: int) -> int:
        return self._add({"kind": "object", "helper_id": helper_id})

    def get(self, ref: int) -> dict | None:
        return self._entries.get(ref)

    def reset(self) -> None:
        self._next = 1
        self._entries.clear()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_perl_refs.py -q` → 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/tdb/adapters/perl/refs.py tests/unit/test_perl_refs.py
git commit -m "feat: perl adapter variablesReference registry"
```

---

### Task 4: DAP stdio server skeleton

The adapter's DAP endpoint: request loop, dispatch table, initialize/disconnect. No perl yet — session wiring lands in Task 9.

**Files:**
- Create: `src/tdb/adapters/perl/server.py`, `src/tdb/adapters/perl/__main__.py`
- Test: `tests/unit/test_perl_dap_server.py`

**Interfaces:**
- Consumes: `tdb.dap.protocol.read_message/encode_message`, `tdb.dap.messages` (+ Task 1 to_dict).
- Produces: `class PerlDapServer` with `__init__(self, reader: asyncio.StreamReader, writer)`, `async run()` (loop until disconnect), `send_event(event: str, body: dict) -> None`, `send_response(request, body=None) -> None`, `send_error(request, message: str) -> None`, and a `self.handlers: dict[str, Callable]` dispatch keyed by DAP command. `writer` needs only `.write(bytes)` and `async .drain()`. Handlers are `async def h(self, request: Request) -> None` methods named `_on_<command>`; unknown commands get an error response. Tasks 9-12 add handlers by defining more `_on_*` methods.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_perl_dap_server.py`:

```python
import asyncio
import json

import pytest

from tdb.adapters.perl.server import PerlDapServer
from tdb.dap.protocol import encode_message


class SinkWriter:
    def __init__(self):
        self.chunks: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.chunks.append(data)

    async def drain(self) -> None:
        pass


def _messages(writer: SinkWriter) -> list[dict]:
    blob = b"".join(writer.chunks)
    out = []
    while blob:
        header, _, rest = blob.partition(b"\r\n\r\n")
        length = int(header.split(b":")[1])
        out.append(json.loads(rest[:length]))
        blob = rest[length:]
    return out


async def _run_conversation(requests: list[dict]) -> list[dict]:
    reader = asyncio.StreamReader()
    for req in requests:
        reader.feed_data(encode_message(req))
    reader.feed_eof()
    writer = SinkWriter()
    server = PerlDapServer(reader, writer)
    await asyncio.wait_for(server.run(), timeout=5)
    return _messages(writer)


async def test_initialize_then_disconnect():
    out = await _run_conversation([
        {"seq": 1, "type": "request", "command": "initialize",
         "arguments": {"adapterID": "perl-tdb"}},
        {"seq": 2, "type": "request", "command": "disconnect"},
    ])
    init = out[0]
    assert init["type"] == "response" and init["command"] == "initialize"
    assert init["success"] is True
    assert init["body"]["supportsConfigurationDoneRequest"] is True
    assert init["body"]["supportsConditionalBreakpoints"] is True
    disc = [m for m in out if m.get("command") == "disconnect"][0]
    assert disc["success"] is True


async def test_unknown_command_errors_but_survives():
    out = await _run_conversation([
        {"seq": 1, "type": "request", "command": "frobnicate"},
        {"seq": 2, "type": "request", "command": "disconnect"},
    ])
    frob = [m for m in out if m.get("command") == "frobnicate"][0]
    assert frob["success"] is False
    assert "frobnicate" in frob["message"]
    assert any(m.get("command") == "disconnect" for m in out)


async def test_handler_exception_becomes_error_response():
    reader = asyncio.StreamReader()
    reader.feed_data(encode_message(
        {"seq": 1, "type": "request", "command": "initialize", "arguments": {}}))
    reader.feed_data(encode_message(
        {"seq": 2, "type": "request", "command": "disconnect"}))
    reader.feed_eof()
    writer = SinkWriter()
    server = PerlDapServer(reader, writer)

    async def boom(request):
        raise RuntimeError("kaput")

    server.handlers["initialize"] = boom
    await asyncio.wait_for(server.run(), timeout=5)
    init = [m for m in _messages(writer) if m.get("command") == "initialize"][0]
    assert init["success"] is False and "kaput" in init["message"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_perl_dap_server.py -q` → ModuleNotFoundError

- [ ] **Step 3: Implement**

Create `src/tdb/adapters/perl/server.py`:

```python
"""DAP stdio server for the Perl adapter.

One request at a time is dispatched from the read loop; handlers are
`_on_<command>` methods collected into `self.handlers` so later tasks
extend the surface by adding methods. Events may be emitted at any
time via send_event (the session driver calls it from its own task).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from tdb.dap.messages import Event, Request, Response, parse_message
from tdb.dap.protocol import encode_message, read_message

log = logging.getLogger(__name__)

CAPABILITIES = {
    "supportsConfigurationDoneRequest": True,
    "supportsConditionalBreakpoints": True,
    "supportsTerminateRequest": True,
}


class PerlDapServer:
    def __init__(self, reader: asyncio.StreamReader, writer: Any) -> None:
        self._reader = reader
        self._writer = writer
        self._seq = 0
        self._done = asyncio.Event()
        self.handlers: dict[str, Callable[[Request], Awaitable[None]]] = {}
        for name in dir(self):
            if name.startswith("_on_"):
                self.handlers[name[4:]] = getattr(self, name)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _write(self, msg: dict) -> None:
        self._writer.write(encode_message(msg))

    def send_response(self, request: Request, body: dict | None = None) -> None:
        self._write(
            Response(
                seq=self._next_seq(),
                request_seq=request.seq,
                command=request.command,
                success=True,
                body=body or {},
            ).to_dict()
        )

    def send_error(self, request: Request, message: str) -> None:
        self._write(
            Response(
                seq=self._next_seq(),
                request_seq=request.seq,
                command=request.command,
                success=False,
                message=message,
            ).to_dict()
        )

    def send_event(self, event: str, body: dict | None = None) -> None:
        self._write(Event(seq=self._next_seq(), event=event, body=body or {}).to_dict())

    async def run(self) -> None:
        while not self._done.is_set():
            try:
                raw = await read_message(self._reader)
            except (ConnectionError, asyncio.IncompleteReadError, EOFError):
                break
            msg = parse_message(raw)
            if not isinstance(msg, Request):
                continue
            handler = self.handlers.get(msg.command)
            if handler is None:
                self.send_error(msg, f"unsupported command: {msg.command}")
                continue
            try:
                await handler(msg)
            except Exception as e:
                log.exception("handler %s failed", msg.command)
                self.send_error(msg, str(e))
            await self._writer.drain()
        await self._writer.drain()

    async def _on_initialize(self, request: Request) -> None:
        self.send_response(request, CAPABILITIES)

    async def _on_disconnect(self, request: Request) -> None:
        self.send_response(request)
        self._done.set()

    async def _on_terminate(self, request: Request) -> None:
        self.send_response(request)
        self._done.set()
```

Create `src/tdb/adapters/perl/__main__.py`:

```python
"""python -m tdb.adapters.perl — run the Perl DAP adapter on stdio."""

import asyncio
import sys

from tdb.adapters.perl.server import PerlDapServer


async def main() -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout.buffer
    )
    writer = asyncio.StreamWriter(transport, protocol, None, loop)
    await PerlDapServer(reader, writer).run()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_perl_dap_server.py -q` → 3 passed.
Smoke the entry point: `echo | .venv/bin/python -m tdb.adapters.perl` → exits 0 quickly (EOF ends the loop).

- [ ] **Step 5: Commit**

```bash
git add src/tdb/adapters/perl/server.py src/tdb/adapters/perl/__main__.py tests/unit/test_perl_dap_server.py
git commit -m "feat: perl adapter DAP stdio server skeleton"
```

---

### Task 5: helpers.pl — infrastructure, location, stack, breakable, source

The injected helper library, part 1. Tested by running functions under plain `perl` (no debugger) — the caller-skipping logic is exercised for real in Task 8's session tests.

**Files:**
- Create: `src/tdb/adapters/perl/helpers.pl`
- Create: `tests/unit/perl_helpers/conftest.py` (perl runner fixture)
- Test: `tests/unit/perl_helpers/test_helpers_core.py`

**Interfaces:**
- Produces (Perl, package `Devel::TdbHelper`): `location()`, `stack()`, `breakable($file)`, `source($file)` — each prints exactly one `TDB>>>{json}<<<TDB\n` line to `$DB::OUT` (falls back to STDOUT when `$DB::OUT` is closed, which is the plain-perl test mode) and returns nothing. `location()` JSON keys: `version` (int 1), `file`, `line`, `sub`. `stack()` keys: `frames` = list of `{file, line, sub}` (innermost first). `breakable()` keys: `lines` (ints ascending). `source()` keys: `text`. Internal: `_emit($hashref)`, `_user_frames()` (caller walk skipping `DB`, `Devel::TdbHelper`, and eval frames). Constant `$Devel::TdbHelper::PROTOCOL = 1`.
- Consumed by: Task 8 (injection + calls), Task 13 (TdbRemote loads it).

- [ ] **Step 1: Write the perl runner fixture + failing tests**

Create `tests/unit/perl_helpers/conftest.py`:

```python
"""Run Devel::TdbHelper functions under plain perl and capture the
marked JSON they emit. Skips the whole directory when perl >= 5.18 is
not available."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HELPERS = (
    Path(__file__).resolve().parents[3] / "src/tdb/adapters/perl/helpers.pl"
)
MARK = re.compile(r"TDB>>>(.*?)<<<TDB", re.S)


def _perl_ok() -> bool:
    perl = shutil.which("perl")
    if perl is None:
        return False
    return subprocess.run([perl, "-e", "require v5.18"]).returncode == 0


pytestmark_skip = pytest.mark.skipif(not _perl_ok(), reason="perl >= 5.18 required")


@pytest.fixture
def run_helper():
    """run_helper(perl_code) -> list of decoded JSON payloads."""

    def _run(code: str) -> list[dict]:
        script = f"do {str(HELPERS)!r} or die $@ || $!;\n{code}\n"
        proc = subprocess.run(
            ["perl", "-e", script], capture_output=True, text=True, timeout=30
        )
        assert proc.returncode == 0, proc.stderr
        return [json.loads(m) for m in MARK.findall(proc.stdout)]

    return _run
```

Create `tests/unit/perl_helpers/test_helpers_core.py`:

```python
import pytest

from .conftest import pytestmark_skip

pytestmark = pytestmark_skip


def test_location_reports_caller_and_protocol_version(run_helper, tmp_path):
    payloads = run_helper("Devel::TdbHelper::location();")
    (loc,) = payloads
    assert loc["version"] == 1
    assert loc["file"].endswith("-e") or loc["file"] == "-e"
    assert isinstance(loc["line"], int)


def test_stack_skips_helper_frames(run_helper):
    code = (
        "sub inner { Devel::TdbHelper::stack() }\n"
        "sub outer { inner() }\n"
        "outer();"
    )
    (payload,) = run_helper(code)
    subs = [f["sub"] for f in payload["frames"]]
    assert not any("TdbHelper" in (s or "") for s in subs)
    assert any("inner" in (s or "") for s in subs)
    assert any("outer" in (s or "") for s in subs)


def test_breakable_and_source_read_perl_line_tables(tmp_path):
    # %{"_<$file"} / @{"_<$file"} line tables only exist under -d, so
    # this test runs perl -d with a stub no-op debugger via PERL5DB.
    import json
    import os
    import re
    import subprocess

    from .conftest import HELPERS

    target = tmp_path / "toy.pl"
    target.write_text("my $a = 1;\n\nmy $b = 2;\nprint $a + $b;\n")
    driver = tmp_path / "driver.pl"
    driver.write_text(
        f"do {str(HELPERS)!r} or die $@ || $!;\n"
        f"do {str(target)!r};\n"
        f"Devel::TdbHelper::breakable({str(target)!r});\n"
        f"Devel::TdbHelper::source({str(target)!r});\n"
    )
    proc = subprocess.run(
        ["perl", "-d", str(driver)],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "PERL5DB": "sub DB::DB {}"},
    )
    assert proc.returncode == 0, proc.stderr
    payloads = [
        json.loads(m)
        for m in re.findall(r"TDB>>>(.*?)<<<TDB", proc.stdout, re.S)
    ]
    lines_payload, source_payload = payloads
    assert 1 in lines_payload["lines"] and 3 in lines_payload["lines"]
    assert 2 not in lines_payload["lines"]  # blank line is not breakable
    assert "my $a = 1;" in source_payload["text"]
```

NOTE for the implementer: if the exact `perl -d` + `PERL5DB` stub
invocation needs adjusting on your perl (5.40 here), adapt the *test
harness* freely — the assertion content (breakable lines 1 and 3, not
2; source text round-trip) is the contract.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/perl_helpers -q`
Expected: FAIL (helpers.pl does not exist → `do` dies)

- [ ] **Step 3: Implement helpers.pl part 1**

Create `src/tdb/adapters/perl/helpers.pl`:

```perl
# Devel::TdbHelper -- data-extraction helpers injected into the
# debuggee by tdb's Perl DAP adapter. Every public sub prints exactly
# one TDB>>>{json}<<<TDB line and returns nothing. All entry points
# trap their own errors: a helper bug must degrade to a JSON error
# reply, never a debuggee crash.
package Devel::TdbHelper;

use strict;
use warnings;
use JSON::PP ();
use Scalar::Util qw(blessed reftype);

our $PROTOCOL = 1;
my $JSON = JSON::PP->new->canonical->allow_unknown;

# Expandable-ref stash: id -> ref. Cleared at each stop (location()).
our %REG;
our $NEXT_ID = 1;

sub _out {
    my ($line) = @_;
    my $fh = ( defined fileno(*DB::OUT) ) ? \*DB::OUT : \*STDOUT;
    print {$fh} $line;
    return;
}

sub _emit {
    my ($data) = @_;
    my $enc = eval { $JSON->encode($data) };
    $enc = '{"error":"json encode failed"}' unless defined $enc;
    _out("TDB>>>$enc<<<TDB\n");
    return;
}

sub _emit_error {
    my ($msg) = @_;
    $msg =~ s/\s+\z//;
    _emit({ error => "$msg" });
    return;
}

# Walk caller() skipping adapter/debugger frames. Returns a list of
# [file, line, subname] innermost-first. The frame a payload describes
# is the debuggee's, never ours.
sub _user_frames {
    my @frames;
    my $i = 0;
    while ( my @c = caller($i) ) {
        my ( $pkg, $file, $line ) = @c[ 0, 1, 2 ];
        $i++;
        next if $pkg =~ /\A(?:DB\b|Devel::TdbHelper)/;
        next if $file =~ /\(eval \d+\)/;
        my $sub = ( caller($i) )[3];    # sub that contains this frame
        push @frames, [ $file, $line, $sub ];
    }
    return @frames;
}

sub location {
    eval {
        %REG     = ();
        $NEXT_ID = 1;
        my @frames = _user_frames();
        my $top = $frames[0] || [ '?', 0, undef ];
        _emit(
            {
                version => $PROTOCOL,
                file    => $top->[0],
                line    => $top->[1] + 0,
                sub     => $top->[2],
            }
        );
        1;
    } or _emit_error($@);
    return;
}

sub stack {
    eval {
        my @out;
        for my $f (_user_frames()) {
            push @out, { file => $f->[0], line => $f->[1] + 0, sub => $f->[2] };
        }
        _emit( { frames => \@out } );
        1;
    } or _emit_error($@);
    return;
}

sub breakable {
    my ($file) = @_;
    eval {
        no strict 'refs';
        my $src = \@{"main::_<$file"};
        my @lines;
        for my $n ( 1 .. $#{$src} ) {
            no warnings 'numeric', 'uninitialized';
            push @lines, $n if defined $src->[$n] && $src->[$n] != 0;
        }
        _emit( { lines => \@lines } );
        1;
    } or _emit_error($@);
    return;
}

sub source {
    my ($file) = @_;
    eval {
        no strict 'refs';
        my $src = \@{"main::_<$file"};
        my $text = join( '', grep { defined } @{$src}[ 1 .. $#{$src} ] );
        _emit( { text => $text } );
        1;
    } or _emit_error($@);
    return;
}

1;
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/perl_helpers -q` → 3 passed (or skipped where perl absent).

- [ ] **Step 5: Commit**

```bash
git add src/tdb/adapters/perl/helpers.pl tests/unit/perl_helpers/conftest.py tests/unit/perl_helpers/test_helpers_core.py
git commit -m "feat: perl helper subs — location/stack/breakable/source"
```

---

### Task 6: helpers.pl — scopes, vars, expand, rich previews

Part 2: the variable machinery. Values are stashed debuggee-side in `%REG` by integer id; the adapter never round-trips access-path strings.

**Files:**
- Modify: `src/tdb/adapters/perl/helpers.pl`
- Test: `tests/unit/perl_helpers/test_helpers_vars.py`

**Interfaces:**
- Produces (Perl): `scopes($frame)` → `{scopes: [{name:"Lexicals"|"Globals"|"Specials", kind:"lexicals"|"globals"|"specials"}]}`; `vars($frame, $kind)` → `{vars: [{name, value, id}]}` where `id` is 0 for atoms or a `%REG` stash id for expandables; `expand($id)` → same `vars` shape, one level; `_preview($value)` → `(display_string, stash_id_or_0)`. Lexicals resolution order: PadWalker → core-B pad walk → `{degraded: "..."}` marker key in the vars() payload. Frame numbering matches `stack()` order (0 = innermost user frame).
- Consumed by: Task 11 (DAP variables/scopes), Task 7 (evaluate reuses `_preview`).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/perl_helpers/test_helpers_vars.py`:

```python
from .conftest import pytestmark_skip

pytestmark = pytestmark_skip


def test_preview_and_expand_hash(run_helper):
    code = (
        'my $h = { name => "al", nums => [1, 2, 3] };\n'
        "Devel::TdbHelper::_test_preview($h);\n"
    )
    (payload,) = run_helper(code)
    assert payload["value"].startswith("HASH")
    assert payload["id"] > 0


def test_expand_returns_one_level(run_helper):
    code = (
        'my $h = { name => "al", nums => [1, 2, 3] };\n'
        "my ($v, $id) = Devel::TdbHelper::_preview($h);\n"
        "Devel::TdbHelper::expand($id);\n"
    )
    (payload,) = run_helper(code)
    by_name = {v["name"]: v for v in payload["vars"]}
    assert by_name["name"]["value"] == "'al'"
    assert by_name["name"]["id"] == 0
    assert by_name["nums"]["value"].startswith("ARRAY")
    assert by_name["nums"]["id"] > 0


def test_blessed_overloaded_tied_and_circular(run_helper):
    code = """
package Point;
use overload '""' => sub { die "overload must not be triggered" };
sub new { my $s = bless { x => 1 }, shift; return $s }
package main;
my $p = Point->new;
my $circ = {};
$circ->{self} = $circ;
my @tied;
{ package NoisyTie; require Tie::Array; our @ISA = ('Tie::StdArray'); }
tie @tied, 'NoisyTie';
Devel::TdbHelper::_test_preview($p);
Devel::TdbHelper::_test_preview($circ);
Devel::TdbHelper::_test_preview(\\@tied);
"""
    p, circ, tied = run_helper(code)
    assert p["value"].startswith("Point=")
    assert circ["id"] > 0  # circular is just expandable, never a crash
    assert "tied" in tied["value"]


def test_undef_distinct_from_empty_string(run_helper):
    code = (
        "Devel::TdbHelper::_test_preview(undef);\n"
        "Devel::TdbHelper::_test_preview('');\n"
    )
    u, e = run_helper(code)
    assert u["value"] == "undef"
    assert e["value"] == "''"


def test_lexicals_via_pad_walk_or_degraded(run_helper):
    code = (
        "sub target { my $inside = 42; my @list = (1,2); "
        "Devel::TdbHelper::vars(0, 'lexicals') }\n"
        "target();"
    )
    (payload,) = run_helper(code)
    if "degraded" in payload:
        assert "PadWalker" in payload["degraded"]
    else:
        names = {v["name"] for v in payload["vars"]}
        assert {"$inside", "@list"} <= names
```

`_test_preview` is a tiny test-only wrapper the implementation must
provide: it calls `_preview` and `_emit`s `{value, id}`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/perl_helpers/test_helpers_vars.py -q` → FAIL (subs undefined)

- [ ] **Step 3: Implement**

Append to `helpers.pl` (before the final `1;`):

```perl
use overload ();

my $HAVE_PADWALKER = eval { require PadWalker; 1 } ? 1 : 0;

sub _stash {
    my ($ref) = @_;
    my $id = $NEXT_ID++;
    $REG{$id} = $ref;
    return $id;
}

# Returns (display_string, expand_id). expand_id 0 => atom.
sub _preview {
    my ($v) = @_;
    return ( 'undef', 0 ) unless defined $v;
    my $rt = reftype($v);
    if ( !defined $rt ) {
        # plain scalar
        return ( "$v", 0 ) if $v =~ /\A-?\d+(?:\.\d+)?\z/;
        my $s = "$v";
        $s = substr( $s, 0, 120 ) . '...' if length($s) > 120;
        $s =~ s/'/\\'/g;
        return ( "'$s'", 0 );
    }
    my $class = blessed($v);
    my $tied =
        $rt eq 'HASH'   ? tied %$v
      : $rt eq 'ARRAY'  ? tied @$v
      : $rt eq 'SCALAR' ? tied $$v
      :                   undef;
    my $base =
        $rt eq 'HASH'  ? sprintf( 'HASH(%d keys)',  scalar keys %$v )
      : $rt eq 'ARRAY' ? sprintf( 'ARRAY(%d)',      scalar @$v )
      : $rt eq 'CODE'  ? overload::StrVal($v)
      :                  overload::StrVal($v);
    $base = $class . '=' . overload::StrVal($v) if defined $class;
    $base .= ' (tied via ' . ref($tied) . ')' if $tied;
    my $expandable = ( $rt eq 'HASH' || $rt eq 'ARRAY' || $rt eq 'SCALAR' ) ? 1 : 0;
    $expandable = 0 if $rt eq 'SCALAR' && !ref($$v) && !defined blessed($v);
    return ( $base, $expandable ? _stash($v) : 0 );
}

sub _test_preview {
    my ($v) = @_;
    my ( $value, $id ) = _preview($v);
    _emit( { value => $value, id => $id } );
    return;
}

sub _entry {
    my ( $name, $v ) = @_;
    my ( $value, $id ) = _preview($v);
    return { name => "$name", value => $value, id => $id };
}

sub expand {
    my ($id) = @_;
    eval {
        my $ref = $REG{$id};
        if ( !defined $ref ) { _emit( { error => "stale ref $id" } ); return 1; }
        my $rt = reftype($ref);
        my @out;
        if ( $rt eq 'HASH' ) {
            push @out, _entry( $_, $ref->{$_} ) for sort keys %$ref;
        }
        elsif ( $rt eq 'ARRAY' ) {
            push @out, _entry( "[$_]", $ref->[$_] ) for 0 .. $#{$ref};
        }
        elsif ( $rt eq 'SCALAR' || $rt eq 'REF' ) {
            push @out, _entry( 'deref', $$ref );
        }
        _emit( { vars => \@out } );
        1;
    } or _emit_error($@);
    return;
}

# --- lexicals -------------------------------------------------------
# Order: PadWalker (if installed) -> core-B read-only pad walk for
# named subs -> degraded marker. Frame numbering matches _user_frames.

sub _lexicals_for_frame {
    my ($frame) = @_;
    if ($HAVE_PADWALKER) {
        # +1: peek_my counts from *this* sub; walk out through our own
        # frames the same way _user_frames skips them.
        my $level = 1;
        my $i     = 0;
        while ( my @c = caller($i) ) {
            $i++;
            next if $c[0] =~ /\A(?:DB\b|Devel::TdbHelper)/;
            next if $c[1] =~ /\(eval \d+\)/;
            last if $frame-- == 0;
        }
        my $pad = eval { PadWalker::peek_my($i) };
        return ( undef, "PadWalker peek failed: $@" ) unless $pad;
        return ( $pad, undef );
    }
    # Core-B fallback: resolve the frame's containing sub by name and
    # read its pad. Anonymous subs and evals can't be resolved by name.
    my @frames  = _user_frames();
    my $subname = $frames[$frame] && $frames[$frame][2];
    return ( undef, 'lexicals unavailable -- install PadWalker' )
      if !$subname || $subname =~ /__ANON__/;
    my ( $pad, $err ) = eval {
        require B;
        no strict 'refs';
        my $cv = \&{$subname};
        my $b  = B::svref_2object($cv);
        return ( undef, 'no pad' ) unless $b->isa('B::CV');
        my $padlist = $b->PADLIST;
        my @names   = $padlist->ARRAYelt(0)->ARRAY;
        my $depth   = $b->DEPTH || 1;
        my @vals    = $padlist->ARRAYelt($depth)->ARRAY;
        my %pad;
        for my $i ( 0 .. $#names ) {
            my $n = $names[$i];
            next unless ref($n) && $n->can('PV') && !$n->isa('B::SPECIAL');
            my $name = eval { $n->PV } or next;
            next unless $name =~ /\A[\$\@\%]\w/;
            my $sv = $vals[$i] or next;
            $pad{$name} = eval { $sv->object_2svref };
        }
        ( \%pad, undef );
    };
    return ( undef, 'lexicals unavailable -- install PadWalker' )
      if !$pad || $@;
    return ( $pad, undef );
}

sub scopes {
    my ($frame) = @_;
    eval {
        _emit(
            {
                scopes => [
                    { name => 'Lexicals', kind => 'lexicals' },
                    { name => 'Globals',  kind => 'globals' },
                    { name => 'Specials', kind => 'specials' },
                ]
            }
        );
        1;
    } or _emit_error($@);
    return;
}

sub vars {
    my ( $frame, $kind ) = @_;
    eval {
        my @out;
        if ( $kind eq 'lexicals' ) {
            my ( $pad, $degraded ) = _lexicals_for_frame($frame);
            if ($degraded) { _emit( { vars => [], degraded => $degraded } ); return 1; }
            for my $name ( sort keys %$pad ) {
                my $ref = $pad->{$name};
                my $val =
                    $name =~ /\A\$/ ? $$ref
                  : $name =~ /\A\@/ ? $ref
                  :                   $ref;
                push @out, _entry( $name, $val );
            }
        }
        elsif ( $kind eq 'globals' ) {
            my @frames = _user_frames();
            my $file = $frames[$frame] ? $frames[$frame][0] : '';
            no strict 'refs';
            my $pkg = 'main';
            for my $name ( sort keys %{"${pkg}::"} ) {
                next if $name =~ /::\z/ || $name =~ /\A(?:_<|[^a-zA-Z])/;
                my $full = "${pkg}::$name";
                push @out, _entry( "\$$name", ${$full} ) if defined ${$full};
            }
        }
        elsif ( $kind eq 'specials' ) {
            push @out, _entry( '$_',    $_ );
            push @out, _entry( '$@',    $@ );
            push @out, _entry( '$!',    "$!" );
            push @out, _entry( '$0',    $0 );
            push @out, _entry( '@ARGV', \@ARGV );
            push @out, _entry( '@INC',  \@INC );
            push @out, _entry( '%ENV',  \%ENV );
            push @out, _entry( '$/',    $/ );
            push @out, _entry( '$\\',   $\ );
        }
        _emit( { vars => \@out } );
        1;
    } or _emit_error($@);
    return;
}
```

Implementer notes: (a) run `.venv/bin/pytest tests/unit/perl_helpers -q`
after every sub — perl syntax errors surface as `do ... die` in the
first test; (b) the B branch is best-effort by design: if your perl's
B API rejects `DEPTH`/`ARRAYelt` calls as written, prefer returning the
degraded marker over fighting it — the PadWalker path and the degraded
path are the contract, the B walk is opportunistic; (c) `@_` handling
in specials is added in Task 7 (needs `@DB::args`, only meaningful
under the real debugger).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/perl_helpers -q` → all pass (PadWalker not installed here → degraded branch of the last test is legal iff B walk fails; on this box perl 5.40's B should succeed → names branch).

- [ ] **Step 5: Commit**

```bash
git add src/tdb/adapters/perl/helpers.pl tests/unit/perl_helpers/test_helpers_vars.py
git commit -m "feat: perl helper variable machinery (scopes/vars/expand, rich previews)"
```

---

### Task 7: helpers.pl — evaluate and @_ specials

**Files:**
- Modify: `src/tdb/adapters/perl/helpers.pl`
- Test: `tests/unit/perl_helpers/test_helpers_eval.py`

**Interfaces:**
- Produces (Perl): `emit_eval($results_arrayref, $err)` → emits `{value, id}` on success (single result unwrapped, multi-value results shown as list preview) or `{error}` when `$err` truthy. The ADAPTER composes the full command string (Task 11): `{ local $@; my $r = [ eval { <EXPR> } ]; Devel::TdbHelper::emit_eval($r, $@) }` — the `eval BLOCK` compiles inside the debugger-eval'd string, which is what makes top-frame lexicals visible. Also: `vars(..., 'specials')` gains `@_` sourced from `@DB::args` when the debugger populated it.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/perl_helpers/test_helpers_eval.py`:

```python
from .conftest import pytestmark_skip

pytestmark = pytestmark_skip

WRAP = "{ local $@; my $r = [ eval { %s } ]; Devel::TdbHelper::emit_eval($r, $@) }"


def test_eval_scalar_result(run_helper):
    (payload,) = run_helper(WRAP % "1 + 2")
    assert payload["value"] == "3"


def test_eval_ref_result_is_expandable(run_helper):
    (payload,) = run_helper(WRAP % "{ a => 1 }")
    assert payload["value"].startswith("HASH")
    assert payload["id"] > 0


def test_eval_list_result(run_helper):
    (payload,) = run_helper(WRAP % "(1, 2, 3)")
    assert payload["value"] == "(1, 2, 3)"


def test_eval_error_captured(run_helper):
    (payload,) = run_helper(WRAP % 'die "boom"')
    assert "boom" in payload["error"]


def test_eval_sees_lexicals_in_wrapping_scope(run_helper):
    code = "my $secret = 41;\n" + (WRAP % "$secret + 1")
    (payload,) = run_helper(code)
    assert payload["value"] == "42"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/perl_helpers/test_helpers_eval.py -q` → FAIL (emit_eval undefined)

- [ ] **Step 3: Implement**

Append to `helpers.pl` (before `1;`):

```perl
sub emit_eval {
    my ( $results, $err ) = @_;
    eval {
        if ($err) {
            my $msg = "$err";
            $msg =~ s/\s+\z//;
            _emit( { error => $msg } );
            return 1;
        }
        if ( @$results == 1 ) {
            my ( $value, $id ) = _preview( $results->[0] );
            _emit( { value => $value, id => $id } );
        }
        elsif ( @$results == 0 ) {
            _emit( { value => '()', id => 0 } );
        }
        else {
            my @parts = map { ( _preview($_) )[0] } @$results;
            my ( undef, $id ) = _preview( [@$results] );
            _emit( { value => '(' . join( ', ', @parts ) . ')', id => $id } );
        }
        1;
    } or _emit_error($@);
    return;
}
```

And inside `vars()`'s `specials` branch, add `@_` first:

```perl
            push @out, _entry( '@_', [ @DB::args ] ) if @DB::args;
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/perl_helpers -q` → all pass.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/adapters/perl/helpers.pl tests/unit/perl_helpers/test_helpers_eval.py
git commit -m "feat: perl helper evaluate support"
```

---

### Task 8: PerlSession — spawn, connect, inject, command queue

The driver that owns the perl child, the RemotePort listener, and the one-command-at-a-time discipline. Tested against real perl (skip-if-no-perl) WITHOUT any DAP.

**Files:**
- Create: `src/tdb/adapters/perl/session.py`
- Test: `tests/integration/test_perl_session_driver.py`

**Interfaces:**
- Consumes: `StreamParser` (Task 2), `helpers.pl` (Tasks 5-7).
- Produces: `class PerlSession` with:
  - `__init__(self, on_output: Callable[[str, str], None], on_stop: Callable[[], None])` — `on_output(text, category)` for program stdout/stderr; `on_stop()` fired on an UNSOLICITED prompt (breakpoint hit / step landed after a resume command).
  - `async launch(self, program: str, args: list[str], cwd: str, env: dict | None, perl: str = "perl") -> None` — bind listener on 127.0.0.1:0, spawn `perl -d program args` with `PERLDB_OPTS=RemotePort=127.0.0.1:<port>`, accept, read to first prompt, inject helpers (`do '<abs helpers.pl>'`), leaving the session stopped at entry.
  - `async attach_socket(self, reader, writer) -> None` — adopt an already-connected perl5db socket (Task 14 uses this; helpers are NOT injected — TdbRemote loaded them).
  - `async command(self, text: str, timeout: float = 20.0) -> list[tuple]` — send one line, collect parser events until the prompt, return them (prompt event excluded). Raises `PerlProtocolError(tail=...)` on timeout.
  - `async helper(self, expr: str, timeout: float = 20.0) -> dict` — `command()` + return the first `("json", ...)` payload; raise `PerlProtocolError` if none or if payload has an `"error"` key.
  - `resume(self, cmd: str) -> None` — send `c`/`n`/`s`/`r` WITHOUT awaiting a prompt; parser events after this are "running mode": text → on_output(category="console"), prompt → `on_stop()`.
  - `helpers_path() -> str` (module function) — absolute path of helpers.pl via `importlib.resources`.
  - `async stop(self)` — kill child (if owned), close socket.
  - `.pid` (int | None), `.stopped` (bool).
  - `class PerlProtocolError(Exception)` with `.tail: str` (last 500 bytes of socket text for diagnosis).

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/test_perl_session_driver.py`:

```python
"""PerlSession against real perl -d — no DAP involved."""

import asyncio
import shutil
import subprocess

import pytest

from tdb.adapters.perl.session import PerlSession, helpers_path

pytestmark = pytest.mark.skipif(
    shutil.which("perl") is None
    or subprocess.run(["perl", "-e", "require v5.18"]).returncode != 0,
    reason="perl >= 5.18 required",
)

WAIT = 20.0

SCRIPT = """\
my $x = 1;
my $y = 2;
sub add { my ($a, $b) = @_; return $a + $b }
my $z = add($x, $y);
print "z=$z\\n";
"""


@pytest.fixture
def script(tmp_path):
    p = tmp_path / "toy.pl"
    p.write_text(SCRIPT)
    return str(p)


@pytest.fixture
async def session(script):
    outputs: list[tuple[str, str]] = []
    stops: list[bool] = []
    s = PerlSession(
        on_output=lambda text, cat: outputs.append((text, cat)),
        on_stop=lambda: stops.append(True),
    )
    await asyncio.wait_for(
        s.launch(program=script, args=[], cwd=str(tmp_path_of(script)), env=None),
        WAIT,
    )
    yield s, outputs, stops
    await s.stop()


def tmp_path_of(script):
    import pathlib

    return pathlib.Path(script).parent


async def test_launch_stops_at_entry_with_helpers_injected(session, script):
    s, _, _ = session
    assert s.stopped
    loc = await s.helper("Devel::TdbHelper::location()")
    assert loc["version"] == 1
    assert loc["file"] == script
    assert loc["line"] == 1


async def test_breakpoint_continue_and_unsolicited_stop(session, script):
    s, _, stops = session
    events = await s.command(f"b 4")
    assert not any(e[0] == "json" for e in events)
    s.resume("c")
    for _ in range(200):
        if stops:
            break
        await asyncio.sleep(0.1)
    assert stops, "breakpoint stop never surfaced"
    loc = await s.helper("Devel::TdbHelper::location()")
    assert loc["line"] == 4


async def test_program_output_reaches_callback(session):
    s, outputs, stops = session
    s.resume("c")  # no breakpoints -> runs to completion
    for _ in range(200):
        if any("z=3" in t for t, _ in outputs):
            break
        await asyncio.sleep(0.1)
    assert any("z=3" in t for t, c in outputs if c == "stdout")


async def test_helper_timeout_raises_with_tail(session):
    s, _, _ = session
    from tdb.adapters.perl.session import PerlProtocolError

    with pytest.raises(PerlProtocolError) as exc:
        # print with no marker and no prompt-forcing newline content;
        # a bogus multi-line construct leaves perl5db waiting => timeout
        await s.command("print 'no prompt yet'; <STDIN>;", timeout=2.0)
    assert isinstance(exc.value.tail, str)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/integration/test_perl_session_driver.py -q` → ModuleNotFoundError

- [ ] **Step 3: Implement**

Create `src/tdb/adapters/perl/session.py`:

```python
"""perl5db session driver.

Owns the debuggee child process (launch mode) or an adopted socket
(attach mode), the stream parser, and the strict one-command-at-a-time
queue. perl5db is a human REPL: a command's response ends at the next
prompt; a prompt with NO command pending means the program stopped.
"""

from __future__ import annotations

import asyncio
import importlib.resources
import logging
import os
import signal
from typing import Callable

from tdb.adapters.perl.protocol import StreamParser

log = logging.getLogger(__name__)


class PerlProtocolError(Exception):
    def __init__(self, message: str, tail: str = "") -> None:
        super().__init__(message)
        self.tail = tail


def helpers_path() -> str:
    ref = importlib.resources.files("tdb.adapters.perl") / "helpers.pl"
    return str(ref)


class PerlSession:
    def __init__(
        self,
        on_output: Callable[[str, str], None],
        on_stop: Callable[[], None],
    ) -> None:
        self._on_output = on_output
        self._on_stop = on_stop
        self._parser = StreamParser()
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._pump_tasks: list[asyncio.Task] = []
        # Events collected for the in-flight command; None => no command
        # pending (running or idle-at-prompt).
        self._collect: list[tuple] | None = None
        self._prompt_evt = asyncio.Event()
        self._tail = b""
        self.stopped = False

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    async def launch(
        self,
        program: str,
        args: list[str],
        cwd: str,
        env: dict | None,
        perl: str = "perl",
    ) -> None:
        server_ready = asyncio.get_running_loop().create_future()

        async def _on_connect(reader, writer):
            if not server_ready.done():
                server_ready.set_result((reader, writer))

        server = await asyncio.start_server(_on_connect, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        child_env = dict(env or os.environ)
        child_env["PERLDB_OPTS"] = f"RemotePort=127.0.0.1:{port}"
        self._process = await asyncio.create_subprocess_exec(
            perl,
            "-d",
            program,
            *args,
            cwd=cwd,
            env=child_env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self._pump_tasks = [
            asyncio.create_task(self._pump(self._process.stdout, "stdout")),
            asyncio.create_task(self._pump(self._process.stderr, "stderr")),
        ]
        try:
            self._reader, self._writer = await asyncio.wait_for(server_ready, 15.0)
        except asyncio.TimeoutError:
            raise PerlProtocolError(
                "perl5db never connected — is perl installed and >= 5.18?"
            )
        finally:
            server.close()
        self._reader_task = asyncio.create_task(self._read_loop())
        await self._await_prompt(timeout=15.0)
        self.stopped = True
        await self.command(f"do '{helpers_path()}'")

    async def attach_socket(self, reader, writer) -> None:
        """Adopt an already-connected perl5db socket (attach mode)."""
        self._reader, self._writer = reader, writer
        self._reader_task = asyncio.create_task(self._read_loop())
        await self._await_prompt(timeout=15.0)
        self.stopped = True

    async def _pump(self, stream: asyncio.StreamReader, category: str) -> None:
        while True:
            data = await stream.read(4096)
            if not data:
                return
            self._on_output(data.decode("utf-8", errors="replace"), category)

    async def _read_loop(self) -> None:
        assert self._reader is not None
        while True:
            data = await self._reader.read(4096)
            if not data:
                self._on_stop_eof()
                return
            self._tail = (self._tail + data)[-500:]
            for ev in self._parser.feed(data):
                self._dispatch(ev)

    def _on_stop_eof(self) -> None:
        self.stopped = False
        self._prompt_evt.set()  # unblock any waiter; command() checks EOF

    def _dispatch(self, ev: tuple) -> None:
        kind = ev[0]
        if kind == "prompt":
            self.stopped = True
            if self._collect is not None:
                self._prompt_evt.set()
            else:
                self._on_stop()  # unsolicited: breakpoint / step landed
        elif self._collect is not None:
            self._collect.append(ev)
        elif kind == "text":
            # perl5db chatter while running (line info etc.)
            self._on_output(ev[1], "console")

    async def _await_prompt(self, timeout: float) -> None:
        self._collect = []
        try:
            await asyncio.wait_for(self._prompt_evt.wait(), timeout)
        except asyncio.TimeoutError:
            raise PerlProtocolError(
                "timed out waiting for perl5db prompt",
                tail=self._tail.decode("utf-8", errors="replace"),
            )
        finally:
            self._prompt_evt.clear()
            self._collect = None

    async def command(self, text: str, timeout: float = 20.0) -> list[tuple]:
        if self._writer is None:
            raise PerlProtocolError("session not connected")
        self._collect = []
        self._prompt_evt.clear()
        self.stopped = False
        self._writer.write(text.encode("utf-8") + b"\n")
        await self._writer.drain()
        try:
            await asyncio.wait_for(self._prompt_evt.wait(), timeout)
        except asyncio.TimeoutError:
            events, self._collect = self._collect, None
            raise PerlProtocolError(
                f"no prompt after command {text!r}",
                tail=self._tail.decode("utf-8", errors="replace"),
            )
        events, self._collect = self._collect, None
        self._prompt_evt.clear()
        self.stopped = True
        return events

    async def helper(self, expr: str, timeout: float = 20.0) -> dict:
        events = await self.command(expr, timeout=timeout)
        for ev in events:
            if ev[0] == "json":
                payload = ev[1]
                if "error" in payload:
                    raise PerlProtocolError(f"helper error: {payload['error']}")
                return payload
        raise PerlProtocolError(
            f"helper produced no JSON: {expr!r}",
            tail=self._tail.decode("utf-8", errors="replace"),
        )

    def resume(self, cmd: str) -> None:
        assert self._writer is not None
        self.stopped = False
        self._collect = None
        self._writer.write(cmd.encode("utf-8") + b"\n")

    def interrupt(self) -> bool:
        """SIGINT the owned child (launch-mode pause). False if not owned."""
        if self._process is None:
            return False
        try:
            os.kill(self._process.pid, signal.SIGINT)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    async def stop(self) -> None:
        for t in [self._reader_task, *self._pump_tasks]:
            if t:
                t.cancel()
        if self._writer is not None:
            self._writer.close()
        if self._process is not None:
            try:
                self._process.kill()
            except ProcessLookupError:
                pass
            await self._process.wait()
```

Implementer notes: (a) `test_helper_timeout_raises_with_tail` relies on
stdin being DEVNULL so `<STDIN>` returns immediately... if it does NOT
time out on your perl, replace the command with `sleep 5` and
timeout=2.0 — the contract is "no prompt within timeout → error with
tail", not the specific stalling construct; (b) perl5db may print a
banner before the first prompt — `_await_prompt` runs with `_collect`
set so the banner is swallowed, which is intended; (c) if the first
prompt never arrives because your perl5db wants a TTY despite
RemotePort, check `PERLDB_OPTS` quoting first — it must be the ONLY
env difference vs. the parent.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/integration/test_perl_session_driver.py -q` → 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/adapters/perl/session.py tests/integration/test_perl_session_driver.py
git commit -m "feat: PerlSession driver — spawn, inject, command queue, stops"
```

---

### Task 9: launch flow through the DAP server

Wire `PerlSession` into `PerlDapServer`: launch, configurationDone, stopped/continued/terminated events, stepping, threads, output forwarding. After this task the adapter debugs a script end-to-end over stdio.

**Files:**
- Modify: `src/tdb/adapters/perl/server.py`
- Create: `tests/integration/perl_adapter_harness.py` (scripted-DAP client helper)
- Test: `tests/integration/test_perl_adapter_launch.py`

**Interfaces:**
- Consumes: `PerlSession` (Task 8).
- Produces server behavior later tasks extend: `self.session: PerlSession | None`, `self.current_stop: dict | None` (last location payload), handler methods `_on_launch`, `_on_configurationDone` (dispatch key "configurationDone"), `_on_threads`, `_on_continue`, `_on_next`, `_on_stepIn`, `_on_stepOut`. Launch request arguments consumed: `program`, `args`, `cwd`, `env`, `stopOnEntry`, `perl` (optional interpreter path). DAP sequence: launch → preflight perl (`perl -e 'require v5.18'`) → session.launch → `initialized` event → (setBreakpoints from client, Task 10) → configurationDone → NOW send the launch response, then either `stopped` event (stopOnEntry) or resume("c"). Single thread: id 1, name "main". Step mapping: next→`n`, stepIn→`s`, stepOut→`r`, continue→`c`; each replies immediately and emits `continued`; the session's `on_stop` callback emits `stopped` with reason "breakpoint" if the new location matches a breakpoint Task 10 registered, else "step".

- [ ] **Step 1: Write the harness + failing test**

Create `tests/integration/perl_adapter_harness.py`:

```python
"""Minimal scripted DAP client for driving the perl adapter subprocess."""

import asyncio
import json
import sys


class AdapterClient:
    def __init__(self):
        self.proc = None
        self.seq = 0
        self.events: list[dict] = []
        self._responses: dict[int, asyncio.Future] = {}

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "tdb.adapters.perl",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        while True:
            try:
                header = b""
                while not header.endswith(b"\r\n\r\n"):
                    chunk = await self.proc.stdout.readexactly(1)
                    header += chunk
                length = int(header.split(b":")[1])
                body = json.loads(await self.proc.stdout.readexactly(length))
            except (asyncio.IncompleteReadError, ValueError):
                return
            if body["type"] == "event":
                self.events.append(body)
            elif body["type"] == "response":
                fut = self._responses.pop(body["request_seq"], None)
                if fut and not fut.done():
                    fut.set_result(body)

    async def request(self, command: str, arguments: dict | None = None,
                      timeout: float = 30.0) -> dict:
        self.seq += 1
        msg = {"seq": self.seq, "type": "request", "command": command}
        if arguments:
            msg["arguments"] = arguments
        fut = asyncio.get_running_loop().create_future()
        self._responses[self.seq] = fut
        body = json.dumps(msg).encode()
        self.proc.stdin.write(
            f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        await self.proc.stdin.drain()
        return await asyncio.wait_for(fut, timeout)

    def send(self, command: str, arguments: dict | None = None):
        """Fire a request without awaiting the response (launch/attach)."""
        self.seq += 1
        msg = {"seq": self.seq, "type": "request", "command": command}
        if arguments:
            msg["arguments"] = arguments
        fut = asyncio.get_running_loop().create_future()
        self._responses[self.seq] = fut
        body = json.dumps(msg).encode()
        self.proc.stdin.write(
            f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        return fut

    async def wait_event(self, name: str, timeout: float = 30.0) -> dict:
        for _ in range(int(timeout * 10)):
            for ev in self.events:
                if ev["event"] == name:
                    self.events.remove(ev)
                    return ev
            await asyncio.sleep(0.1)
        raise AssertionError(f"event {name!r} never arrived; saw {self.events}")

    async def stop(self):
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
```

Create `tests/integration/test_perl_adapter_launch.py`:

```python
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

SCRIPT = "my $x = 1;\nmy $y = 2;\nprint \"sum=\", $x + $y, \"\\n\";\n"


@pytest.fixture
def script(tmp_path):
    p = tmp_path / "toy.pl"
    p.write_text(SCRIPT)
    return str(p)


@pytest.fixture
async def client():
    c = AdapterClient()
    await c.start()
    yield c
    await c.stop()


async def test_launch_stop_on_entry_step_and_run_to_exit(client, script, tmp_path):
    await client.request("initialize", {"adapterID": "perl-tdb"})
    launch_fut = client.send("launch", {
        "program": script, "args": [], "cwd": str(tmp_path),
        "stopOnEntry": True,
    })
    await client.wait_event("initialized")
    await client.request("configurationDone")
    launch_resp = await asyncio.wait_for(launch_fut, 30)
    assert launch_resp["success"] is True
    stopped = await client.wait_event("stopped")
    assert stopped["body"]["reason"] == "entry"
    threads = await client.request("threads")
    assert threads["body"]["threads"] == [{"id": 1, "name": "main"}]
    await client.request("next")
    stopped = await client.wait_event("stopped")
    assert stopped["body"]["reason"] == "step"
    await client.request("continue")
    ev = await client.wait_event("output")
    outputs = [ev]
    while "sum=3" not in "".join(o["body"]["output"] for o in outputs):
        outputs.append(await client.wait_event("output"))
    await client.wait_event("terminated")


async def test_launch_missing_perl_program_errors(client, tmp_path):
    await client.request("initialize", {"adapterID": "perl-tdb"})
    launch_fut = client.send("launch", {
        "program": str(tmp_path / "nope.pl"), "args": [],
        "cwd": str(tmp_path), "stopOnEntry": True,
    })
    resp = await asyncio.wait_for(launch_fut, 30)
    assert resp["success"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/integration/test_perl_adapter_launch.py -q`
Expected: FAIL — launch gets "unsupported command: launch"

- [ ] **Step 3: Implement launch flow in server.py**

Add to `PerlDapServer.__init__`: `self.session = None`, `self.current_stop = None`, `self._launch_request = None`, `self._stop_on_entry = True`, `self._configured = asyncio.Event()`, `self.breakpoint_lines: dict[str, set[int]] = {}` (filled by Task 10), and capture `self._loop = asyncio.get_event_loop()` inside `run()`'s first line instead if construction happens without a loop.

Add handlers:

```python
    async def _on_launch(self, request: Request) -> None:
        args = request.arguments
        perl = args.get("perl") or "perl"
        preflight = await asyncio.create_subprocess_exec(
            perl, "-e", "require v5.18",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await preflight.communicate()
        if preflight.returncode != 0:
            self.send_error(
                request,
                f"perl >= 5.18 not usable ({perl!r}): "
                f"{err.decode(errors='replace').strip() or 'not found'} — "
                'install perl or set {"adapters": {"perl": "/path/to/perl"}} '
                "in tdb's config.json",
            )
            return
        program = args.get("program", "")
        if not os.path.isfile(program):
            self.send_error(request, f"program not found: {program}")
            return
        self._stop_on_entry = bool(args.get("stopOnEntry", True))
        self.session = PerlSession(
            on_output=self._forward_output, on_stop=self._on_unsolicited_stop
        )
        try:
            await self.session.launch(
                program=program,
                args=list(args.get("args") or []),
                cwd=args.get("cwd") or os.getcwd(),
                env=args.get("env"),
                perl=perl,
            )
        except PerlProtocolError as e:
            self.send_error(request, f"{e} [{e.tail}]")
            return
        self._launch_request = request
        self.send_event("initialized")
        # response is sent by _on_configurationDone (DAP ordering)

    async def _on_configurationDone(self, request: Request) -> None:
        self.send_response(request)
        if self._launch_request is not None:
            self.send_response(self._launch_request)
            self._launch_request = None
            if self._stop_on_entry:
                await self._emit_stopped("entry")
            else:
                self.session.resume("c")

    async def _emit_stopped(self, reason: str) -> None:
        try:
            self.current_stop = await self.session.helper(
                "Devel::TdbHelper::location()"
            )
        except PerlProtocolError as e:
            log.error("location() failed after stop: %s", e)
            self.current_stop = None
        self.send_event(
            "stopped",
            {"reason": reason, "threadId": 1, "allThreadsStopped": True},
        )

    def _forward_output(self, text: str, category: str) -> None:
        self.send_event("output", {"category": category, "output": text})

    def _on_unsolicited_stop(self) -> None:
        asyncio.ensure_future(self._classify_and_emit_stop())

    async def _classify_and_emit_stop(self) -> None:
        try:
            loc = await self.session.helper("Devel::TdbHelper::location()")
        except PerlProtocolError:
            loc = None
        self.current_stop = loc
        reason = "step"
        if loc and loc.get("line") in self.breakpoint_lines.get(loc.get("file"), set()):
            reason = "breakpoint"
        self.send_event(
            "stopped",
            {"reason": reason, "threadId": 1, "allThreadsStopped": True},
        )
        await self._writer.drain()

    async def _on_threads(self, request: Request) -> None:
        self.send_response(request, {"threads": [{"id": 1, "name": "main"}]})

    async def _resume(self, request: Request, cmd: str) -> None:
        if self.session is None or not self.session.stopped:
            self.send_error(request, "debuggee is not stopped")
            return
        self.current_stop = None
        self.send_response(request)
        self.send_event("continued", {"threadId": 1, "allThreadsContinued": True})
        self.session.resume(cmd)

    async def _on_continue(self, request: Request) -> None:
        await self._resume(request, "c")

    async def _on_next(self, request: Request) -> None:
        await self._resume(request, "n")

    async def _on_stepIn(self, request: Request) -> None:
        await self._resume(request, "s")

    async def _on_stepOut(self, request: Request) -> None:
        await self._resume(request, "r")
```

Also: `import os` at top; import `PerlSession, PerlProtocolError` from `.session`. Detect debuggee exit: in `_on_stop_eof` PerlSession already unblocks; add an `on_exit` callback wired the same way as `on_stop` OR simpler — in `PerlSession._on_stop_eof`, call `self._on_output("", "__eof__")` and have `_forward_output` translate `category == "__eof__"` into `send_event("terminated")` + `send_event("exited", {"exitCode": 0})` (skip the output event). Choose the simpler route and keep it internal to these two files; `_classify_and_emit_stop` must also tolerate `session=None` after teardown. Update `_on_disconnect`/`_on_terminate` to `await self.session.stop()` when a session exists.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/integration/test_perl_adapter_launch.py -q` → 2 passed.
Also: `.venv/bin/pytest tests/unit/test_perl_dap_server.py -q` still passes.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/adapters/perl/server.py tests/integration/perl_adapter_harness.py tests/integration/test_perl_adapter_launch.py
git commit -m "feat: perl adapter launch flow — stepping, output, lifecycle events"
```

---

### Task 10: breakpoints

**Files:**
- Modify: `src/tdb/adapters/perl/server.py`
- Test: `tests/integration/test_perl_adapter_breakpoints.py`

**Interfaces:**
- Produces: `_on_setBreakpoints` handling DAP `setBreakpoints {source: {path}, breakpoints: [{line, condition?}]}` → replaces all breakpoints in that file: delete previously-set lines (`B <line>`), then for each requested line: if not in `breakable(file)` lines, snap FORWARD to the next breakable line (or mark unverified if none); set with `b <file>:<line> [condition]`. Response body: `{"breakpoints": [{"verified": bool, "line": actual_line}]}` in request order. Updates `self.breakpoint_lines[path] = {actual lines}` (Task 9's stop classifier reads it). Requests arriving before launch complete are answered from a queue after injection — implement by simply requiring `self.session` (tdb sends setBreakpoints between `initialized` and `configurationDone`, which is after launch in this adapter, so the session exists).

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/test_perl_adapter_breakpoints.py` (reuse harness + skip marker + `script` fixture SHAPE from Task 9 — import the harness, copy the pytestmark):

```python
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

SCRIPT = """\
my $total = 0;

for my $i (1 .. 5) {
    $total += $i;
}
print "total=$total\\n";
"""


@pytest.fixture
def script(tmp_path):
    p = tmp_path / "bp.pl"
    p.write_text(SCRIPT)
    return str(p)


@pytest.fixture
async def started(script, tmp_path):
    c = AdapterClient()
    await c.start()
    await c.request("initialize", {"adapterID": "perl-tdb"})
    launch_fut = c.send("launch", {
        "program": script, "args": [], "cwd": str(tmp_path), "stopOnEntry": True,
    })
    await c.wait_event("initialized")
    yield c, script, launch_fut
    await c.stop()


async def test_set_hit_and_conditional_breakpoints(started):
    c, script, launch_fut = started
    resp = await c.request("setBreakpoints", {
        "source": {"path": script},
        "breakpoints": [{"line": 4, "condition": "$i == 3"}],
    })
    (bp,) = resp["body"]["breakpoints"]
    assert bp["verified"] is True and bp["line"] == 4
    await c.request("configurationDone")
    await asyncio.wait_for(launch_fut, 30)
    await c.wait_event("stopped")            # entry
    await c.request("continue")
    stopped = await c.wait_event("stopped")  # conditional breakpoint
    assert stopped["body"]["reason"] == "breakpoint"


async def test_blank_line_snaps_forward(started):
    c, script, launch_fut = started
    resp = await c.request("setBreakpoints", {
        "source": {"path": script},
        "breakpoints": [{"line": 2}],   # blank line
    })
    (bp,) = resp["body"]["breakpoints"]
    assert bp["verified"] is True
    assert bp["line"] == 3


async def test_replace_clears_old_breakpoints(started):
    c, script, launch_fut = started
    await c.request("setBreakpoints", {
        "source": {"path": script}, "breakpoints": [{"line": 4}]})
    await c.request("setBreakpoints", {
        "source": {"path": script}, "breakpoints": []})
    await c.request("configurationDone")
    await asyncio.wait_for(launch_fut, 30)
    await c.wait_event("stopped")   # entry
    await c.request("continue")
    await c.wait_event("terminated", timeout=30)   # ran through: no bp left
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/integration/test_perl_adapter_breakpoints.py -q`
Expected: FAIL — "unsupported command: setBreakpoints"

- [ ] **Step 3: Implement `_on_setBreakpoints`**

```python
    async def _on_setBreakpoints(self, request: Request) -> None:
        if self.session is None or not self.session.stopped:
            self.send_error(request, "cannot set breakpoints while running")
            return
        path = request.arguments.get("source", {}).get("path", "")
        wanted = request.arguments.get("breakpoints", [])
        for old_line in self.breakpoint_lines.get(path, set()):
            await self.session.command(f"B {old_line}")
        try:
            breakable = set(
                (await self.session.helper(
                    f"Devel::TdbHelper::breakable({self._perl_str(path)})"
                ))["lines"]
            )
        except PerlProtocolError:
            breakable = set()
        results = []
        actual_lines: set[int] = set()
        for bp in wanted:
            line = bp["line"]
            target = line
            if breakable and line not in breakable:
                later = sorted(n for n in breakable if n > line)
                target = later[0] if later else None
            if target is None:
                results.append({"verified": False, "line": line})
                continue
            cond = bp.get("condition")
            cmd = f"b {path}:{target}" + (f" {cond}" if cond else "")
            events = await self.session.command(cmd)
            failed = any(
                e[0] == "text" and "not breakable" in e[1] for e in events
            )
            if failed:
                results.append({"verified": False, "line": line})
            else:
                results.append({"verified": True, "line": target})
                actual_lines.add(target)
        self.breakpoint_lines[path] = actual_lines
        self.send_response(request, {"breakpoints": results})

    @staticmethod
    def _perl_str(s: str) -> str:
        return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/integration/test_perl_adapter_breakpoints.py tests/integration/test_perl_adapter_launch.py -q` → all pass.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/adapters/perl/server.py tests/integration/test_perl_adapter_breakpoints.py
git commit -m "feat: perl adapter breakpoints — diff, snap, conditions"
```

---

### Task 11: stack, scopes, variables, evaluate over DAP

**Files:**
- Modify: `src/tdb/adapters/perl/server.py`
- Test: `tests/integration/test_perl_adapter_inspection.py`

**Interfaces:**
- Consumes: helpers (Tasks 5-7), `RefRegistry` (Task 3).
- Produces: `_on_stackTrace` (body `{"stackFrames": [{id, name, line, column: 1, source: {path}}], "totalFrames": n}`, frame id = index into the helper's stack, 1000 + index so ids are distinct from variablesReferences is NOT needed — DAP scopes take frameId verbatim; use plain index), `_on_scopes` (`{"scopes": [{name, variablesReference, expensive: false}]}` from `refs.add_scope(frame, kind)`), `_on_variables` (dispatch on `refs.get(...)["kind"]`: scope → `vars(frame, kind)` helper; object → `expand(id)` helper; body `{"variables": [{name, value, variablesReference}]}` where reference is `refs.add_object(id)` for id>0 else 0; a `degraded` payload key becomes one pseudo-variable named `<lexicals>` with the message as value), `_on_evaluate` (compose `{ local $@; my $r = [ eval { <EXPR> } ]; Devel::TdbHelper::emit_eval($r, $@) }`, reply `{"result": value, "variablesReference": ref}`; helper `{"error"}` payload → DAP error response). `RefRegistry.reset()` is called in `_emit_stopped`/`_classify_and_emit_stop` (stale handles must die at each stop, matching the helper's `%REG` reset in `location()`).

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/test_perl_adapter_inspection.py` (same skip marker and harness import as Task 10):

```python
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

SCRIPT = """\
sub work {
    my ($label) = @_;
    my %info = ( label => $label, nums => [ 10, 20 ] );
    my $marker = 1;   # line 4: breakpoint here
    return \\%info;
}
my $r = work("go");
print "done\\n";
"""


@pytest.fixture
async def at_breakpoint(tmp_path):
    p = tmp_path / "insp.pl"
    p.write_text(SCRIPT)
    c = AdapterClient()
    await c.start()
    await c.request("initialize", {"adapterID": "perl-tdb"})
    launch_fut = c.send("launch", {
        "program": str(p), "args": [], "cwd": str(tmp_path), "stopOnEntry": False,
    })
    await c.wait_event("initialized")
    await c.request("setBreakpoints", {
        "source": {"path": str(p)}, "breakpoints": [{"line": 4}]})
    await c.request("configurationDone")
    await asyncio.wait_for(launch_fut, 30)
    await c.wait_event("stopped")
    yield c, str(p)
    await c.stop()


async def test_stack_scopes_variables_expand(at_breakpoint):
    c, path = at_breakpoint
    st = await c.request("stackTrace", {"threadId": 1})
    frames = st["body"]["stackFrames"]
    assert frames[0]["source"]["path"] == path
    assert frames[0]["line"] == 4
    assert any("work" in f["name"] for f in frames)

    sc = await c.request("scopes", {"frameId": frames[0]["id"]})
    by_name = {s["name"]: s for s in sc["body"]["scopes"]}
    assert {"Lexicals", "Globals", "Specials"} <= set(by_name)

    lex = await c.request(
        "variables", {"variablesReference": by_name["Lexicals"]["variablesReference"]})
    lex_vars = {v["name"]: v for v in lex["body"]["variables"]}
    if "<lexicals>" in lex_vars:
        assert "PadWalker" in lex_vars["<lexicals>"]["value"]
    else:
        assert "%info" in lex_vars
        nested = await c.request(
            "variables",
            {"variablesReference": lex_vars["%info"]["variablesReference"]})
        names = {v["name"] for v in nested["body"]["variables"]}
        assert {"label", "nums"} <= names

    spec = await c.request(
        "variables", {"variablesReference": by_name["Specials"]["variablesReference"]})
    spec_names = {v["name"] for v in spec["body"]["variables"]}
    assert "@_" in spec_names or "$0" in spec_names


async def test_evaluate_in_top_frame(at_breakpoint):
    c, _ = at_breakpoint
    ok = await c.request("evaluate", {"expression": "1 + 2", "context": "repl"})
    assert ok["body"]["result"] == "3"
    lex = await c.request("evaluate", {"expression": "$label", "context": "repl"})
    assert lex["body"]["result"] == "'go'"
    err = await c.request("evaluate", {"expression": "die 'nope'", "context": "repl"})
    assert err["success"] is False and "nope" in err["message"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/integration/test_perl_adapter_inspection.py -q` → "unsupported command: stackTrace"

- [ ] **Step 3: Implement the four handlers**

Add `from tdb.adapters.perl.refs import RefRegistry`, `self.refs = RefRegistry()` in `__init__`, `self.refs.reset()` first line of both `_emit_stopped` and `_classify_and_emit_stop`, then:

```python
    async def _on_stackTrace(self, request: Request) -> None:
        payload = await self.session.helper("Devel::TdbHelper::stack()")
        frames = []
        for i, f in enumerate(payload["frames"]):
            frames.append({
                "id": i,
                "name": f.get("sub") or "main",
                "line": f["line"],
                "column": 1,
                "source": {"path": f["file"]},
            })
        self.send_response(
            request, {"stackFrames": frames, "totalFrames": len(frames)}
        )

    async def _on_scopes(self, request: Request) -> None:
        frame = request.arguments.get("frameId", 0)
        payload = await self.session.helper(
            f"Devel::TdbHelper::scopes({frame})"
        )
        scopes = [
            {
                "name": s["name"],
                "variablesReference": self.refs.add_scope(frame, s["kind"]),
                "expensive": False,
            }
            for s in payload["scopes"]
        ]
        self.send_response(request, {"scopes": scopes})

    async def _on_variables(self, request: Request) -> None:
        ref = request.arguments.get("variablesReference", 0)
        entry = self.refs.get(ref)
        if entry is None:
            self.send_error(request, f"stale variablesReference {ref}")
            return
        if entry["kind"] == "scope":
            payload = await self.session.helper(
                f"Devel::TdbHelper::vars({entry['frame']}, "
                f"{self._perl_str(entry['scope'])})"
            )
        else:
            payload = await self.session.helper(
                f"Devel::TdbHelper::expand({entry['helper_id']})"
            )
        variables = []
        if payload.get("degraded"):
            variables.append({
                "name": "<lexicals>",
                "value": payload["degraded"],
                "variablesReference": 0,
            })
        for v in payload.get("vars", []):
            variables.append({
                "name": v["name"],
                "value": v["value"],
                "variablesReference": (
                    self.refs.add_object(v["id"]) if v["id"] else 0
                ),
            })
        self.send_response(request, {"variables": variables})

    async def _on_evaluate(self, request: Request) -> None:
        expr = request.arguments.get("expression", "")
        cmd = (
            "{ local $@; my $r = [ eval { " + expr + " } ]; "
            "Devel::TdbHelper::emit_eval($r, $@) }"
        )
        try:
            payload = await self.session.helper(cmd)
        except PerlProtocolError as e:
            self.send_error(request, str(e))
            return
        self.send_response(request, {
            "result": payload["value"],
            "variablesReference": (
                self.refs.add_object(payload["id"]) if payload.get("id") else 0
            ),
        })
```

Note: `helper()` raises on `{"error": ...}` payloads, so a `die` in the
user's expression surfaces as the DAP error the last test expects.
Multi-line expressions would break the one-line protocol: strip/reject
newlines in `expr` (`expr.replace("\n", " ")`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/integration/test_perl_adapter_inspection.py -q` → 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/adapters/perl/server.py tests/integration/test_perl_adapter_inspection.py
git commit -m "feat: perl adapter inspection — stack/scopes/variables/evaluate"
```

---

### Task 12: pause, DAP source request, error-path polish

**Files:**
- Modify: `src/tdb/adapters/perl/server.py`
- Test: `tests/integration/test_perl_adapter_pause_source.py`

**Interfaces:**
- Produces: `_on_pause` (launch mode: `session.interrupt()` → perl5db stops at next statement → the normal unsolicited-stop path emits `stopped` with reason "pause" — add `self._pause_pending = True` consumed by `_classify_and_emit_stop`; when `interrupt()` returns False (attach mode until Task 15) → error response "pause is not available for this session"); `_on_source` (arguments `{"source": {"path": ...}}` → `source(path)` helper → `{"content": text}`; empty text → error response so tdb's fallback placeholder kicks in).

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/test_perl_adapter_pause_source.py` (same skip + harness):

```python
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

LOOP_SCRIPT = "my $i = 0;\nwhile (1) { $i++; select undef, undef, undef, 0.01 }\n"


@pytest.fixture
async def running_loop(tmp_path):
    p = tmp_path / "loop.pl"
    p.write_text(LOOP_SCRIPT)
    c = AdapterClient()
    await c.start()
    await c.request("initialize", {"adapterID": "perl-tdb"})
    fut = c.send("launch", {"program": str(p), "args": [],
                            "cwd": str(tmp_path), "stopOnEntry": False})
    await c.wait_event("initialized")
    await c.request("configurationDone")
    await asyncio.wait_for(fut, 30)
    await asyncio.sleep(1.0)  # let it spin
    yield c, str(p)
    await c.stop()


async def test_pause_stops_running_program(running_loop):
    c, _ = running_loop
    resp = await c.request("pause", {"threadId": 1})
    assert resp["success"] is True
    stopped = await c.wait_event("stopped")
    assert stopped["body"]["reason"] == "pause"


async def test_source_request_serves_compiled_file(running_loop):
    c, path = running_loop
    await c.request("pause", {"threadId": 1})
    await c.wait_event("stopped")
    resp = await c.request("source", {"source": {"path": path}})
    assert "while (1)" in resp["body"]["content"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/integration/test_perl_adapter_pause_source.py -q` → "unsupported command: pause"

- [ ] **Step 3: Implement**

```python
    async def _on_pause(self, request: Request) -> None:
        if self.session is None:
            self.send_error(request, "no session")
            return
        if self.session.stopped:
            self.send_response(request)
            return
        if not self.session.interrupt():
            self.send_error(request, "pause is not available for this session")
            return
        self._pause_pending = True
        self.send_response(request)

    async def _on_source(self, request: Request) -> None:
        path = request.arguments.get("source", {}).get("path", "")
        payload = await self.session.helper(
            f"Devel::TdbHelper::source({self._perl_str(path)})"
        )
        if not payload.get("text"):
            self.send_error(request, f"no compiled source for {path}")
            return
        self.send_response(request, {"content": payload["text"]})
```

In `_classify_and_emit_stop`, before the breakpoint check: `if getattr(self, "_pause_pending", False): self._pause_pending = False; reason = "pause"` (breakpoint match still wins if both apply — check breakpoint first, pause second, then step). Initialize `self._pause_pending = False` in `__init__`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/integration/test_perl_adapter_pause_source.py -q` → 2 passed. Run all perl adapter tests: `.venv/bin/pytest tests/integration -k perl -q` → all pass.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/adapters/perl/server.py tests/integration/test_perl_adapter_pause_source.py
git commit -m "feat: perl adapter pause + DAP source request"
```

---

### Task 13: Devel::TdbRemote — debuggee-side listen()/wait_for_client()

The debugpy-look-and-feel library. Pure Perl, tested with a raw TCP socket from pytest — no adapter involvement yet.

**Files:**
- Create: `src/tdb/adapters/perl/Devel/TdbRemote.pm`
- Test: `tests/integration/test_perl_tdbremote.py`

**Interfaces:**
- Produces (Perl, package `Devel::TdbRemote`): `listen($port, $host = '0.0.0.0')` — opens the listening socket, returns immediately; `wait_for_client()` — blocks in accept(), installs the accepted socket as perl5db's `*DB::IN`/`*DB::OUT`, loads `helpers.pl` from its own directory (`__FILE__` sibling `../helpers.pl`), sets `$DB::single = 1`, returns. On load (BEGIN): arm the debugger if the host perl didn't (`$^P` flags + `require 'perl5db.pl'` under `PERLDB_OPTS=NonStop=1`). Works via `use Devel::TdbRemote;` (first line), `perl -d:TdbRemote`, or `PERL5OPT=-d:TdbRemote`.
- Consumed by: Task 14 (adapter connects to it), users' remote programs.

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_perl_tdbremote.py`:

```python
"""Devel::TdbRemote handshake probed with a raw TCP client."""

import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("perl") is None
    or subprocess.run(["perl", "-e", "require v5.18"]).returncode != 0,
    reason="perl >= 5.18 required",
)

PKG_DIR = Path(__file__).resolve().parents[2] / "src/tdb/adapters/perl"

SCRIPT = """\
use Devel::TdbRemote;
my $before = 40;
open my $fh, '>', $ARGV[1] or die;   # port-ready handshake file
Devel::TdbRemote::listen($ARGV[0], '127.0.0.1');
print {$fh} "listening\\n";
close $fh;
Devel::TdbRemote::wait_for_client();
my $after = $before + 2;
print "after=$after\\n";
"""


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_wait_for_client_stops_and_serves_helpers(tmp_path):
    prog = tmp_path / "remote_prog.pl"
    prog.write_text(SCRIPT)
    ready = tmp_path / "ready"
    port = _free_port()
    proc = subprocess.Popen(
        ["perl", f"-I{PKG_DIR}", str(prog), str(port), str(ready)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        for _ in range(100):
            if ready.exists() and ready.read_text().startswith("listening"):
                break
            time.sleep(0.1)
        else:
            proc.kill()
            pytest.fail(f"never listened; stderr={proc.stderr.read()}")
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        sock.settimeout(10)
        buf = b""
        while b"DB<" not in buf:            # stopped: prompt arrives
            buf += sock.recv(4096)
        sock.sendall(b"Devel::TdbHelper::location()\n")
        buf = b""
        while b"<<<TDB" not in buf:         # helpers were preloaded
            buf += sock.recv(4096)
        assert b'"version":1' in buf.replace(b" ", b"")
        sock.sendall(b"c\n")                # detach-ish: let it finish
        out, _ = proc.communicate(timeout=15)
        assert "after=42" in out
    finally:
        if proc.poll() is None:
            proc.kill()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/integration/test_perl_tdbremote.py -q`
Expected: FAIL — "Can't locate Devel/TdbRemote.pm"

- [ ] **Step 3: Implement TdbRemote.pm**

Create `src/tdb/adapters/perl/Devel/TdbRemote.pm`:

```perl
# Devel::TdbRemote -- debugpy-style remote attach for tdb.
#
#   use Devel::TdbRemote;                 # FIRST line of your program
#   ...
#   Devel::TdbRemote::listen(5678);       # non-blocking
#   Devel::TdbRemote::wait_for_client();  # blocks until tdb connects
#
# Also works via `perl -d:TdbRemote prog.pl` or PERL5OPT=-d:TdbRemote.
# Only code compiled AFTER the debugger is armed can be stepped or
# breakpointed -- that is why the `use` line must come first.
package Devel::TdbRemote;

use strict;
use warnings;
use IO::Socket::INET ();
use File::Basename   ();

our $VERSION = '1.0';
my $LISTENER;

BEGIN {
    # Arm the debugger unless perl already did (-d / -d:TdbRemote).
    # NonStop: perl5db initializes without a TTY and lets the program
    # run freely until we flip $DB::single in wait_for_client().
    $ENV{PERLDB_OPTS} = 'NonStop=1'
      unless defined $ENV{PERLDB_OPTS} && length $ENV{PERLDB_OPTS};
    $^P = 0x73f unless $^P & 0x02;
    unless ( defined &DB::DB ) {
        package DB;
        require 'perl5db.pl';
    }
}

sub listen {
    my ( $port, $host ) = @_;
    $host = '0.0.0.0' unless defined $host;
    $LISTENER = IO::Socket::INET->new(
        LocalAddr => $host,
        LocalPort => $port,
        Listen    => 1,
        ReuseAddr => 1,
    ) or die "Devel::TdbRemote: cannot listen on $host:$port: $!\n";
    return;
}

sub wait_for_client {
    die "Devel::TdbRemote: call listen(\$port) first\n" unless $LISTENER;
    my $client = $LISTENER->accept
      or die "Devel::TdbRemote: accept failed: $!\n";
    $client->autoflush(1);

    # Install the socket as perl5db's terminal (both directions).
    open *DB::IN,  '<&', $client or die "TdbRemote: dup IN: $!\n";
    open *DB::OUT, '>&', $client or die "TdbRemote: dup OUT: $!\n";
    select( ( select(*DB::OUT), $| = 1 )[0] );
    { no warnings 'once'; $DB::LINEINFO = *DB::OUT; }

    # Load the data-extraction helpers that live next to this module.
    my $dir     = File::Basename::dirname(__FILE__);
    my $helpers = "$dir/../helpers.pl";
    do $helpers or die "TdbRemote: cannot load $helpers: " . ( $@ || $! ) . "\n";

    # Stop at the statement after this call, debugpy-style.
    $DB::single = 1;
    return;
}

1;
```

Implementer notes: this is the file most likely to need adaptation
against the real perl5db (5.40 here). The contract is the TEST, not the
exact handle wiring: stopped-prompt over the socket, helpers preloaded,
`c` resumes. Known adjustment points if the test stalls: (a) perl5db
may want `$DB::fork_TTY`/`DB::set_tty` style switching instead of raw
glob dup — try `*DB::IN = $client; *DB::OUT = $client;` glob assignment
first; (b) NonStop mode may require `$DB::signal = 0` cleared before
`$DB::single = 1`; (c) if `require 'perl5db.pl'` under `package DB`
misbehaves when loaded from `use` (non-`-d` invocation), test the
`-d:TdbRemote` invocation first (`perl -d:TdbRemote -I<pkg> prog.pl`)
to separate arming problems from socket problems, and disclose whatever
you had to change in your report.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/integration/test_perl_tdbremote.py -q` → 1 passed.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/adapters/perl/Devel/TdbRemote.pm tests/integration/test_perl_tdbremote.py
git commit -m "feat: Devel::TdbRemote — listen()/wait_for_client() remote attach"
```

---

### Task 14: adapter attach mode + attach_via_adapter quirk

Two halves: (a) the adapter's DAP `attach` handler connects OUT to a waiting TdbRemote debuggee; (b) tdb's controller learns that some adapters mediate attach themselves (spawn adapter subprocess + attach request) instead of tdb dialing TCP directly (debugpy).

**Files:**
- Modify: `src/tdb/adapters/perl/server.py`, `src/tdb/languages/base.py`, `src/tdb/session/controller.py`
- Test: `tests/integration/test_perl_adapter_attach.py`, additions to `tests/unit/test_languages_base.py` and `tests/unit/test_controller.py` (or nearest controller unit-test file — check `ls tests/unit | grep controller` and append to the existing one)

**Interfaces:**
- Produces: `AdapterQuirks.attach_via_adapter: bool = False` (True → controller spawns the adapter with `client.start()` before sending attach; False → current `client.connect(host, port)` path). Adapter `_on_attach`: arguments `{host, port}` → `asyncio.open_connection(host, port)` (10s timeout) → `session.attach_socket(reader, writer)` → verify helper protocol: `location()` must return `version == 1` (mismatch → error response naming both files to update; TdbRemote already loaded helpers) → `initialized` event; attach response is held and sent by `_on_configurationDone` exactly like launch (generalize `self._launch_request` to `self._start_request`), after which the entry stop is reported (reason "entry" — TdbRemote's `$DB::single` stopped it). Pause in attach mode: NOT wired — `session.interrupt()` returns False (no owned pid) → clear DAP error (spec's gate-if-flaky decision defaults to gated; a control-channel pause is future work recorded in Task 16's docs).

- [ ] **Step 1: Write the failing tests**

Unit (append to `tests/unit/test_languages_base.py`):

```python
def test_adapter_quirks_attach_via_adapter_defaults_false():
    from tdb.languages.base import AdapterQuirks

    assert AdapterQuirks().attach_via_adapter is False
    assert AdapterQuirks(attach_via_adapter=True).attach_via_adapter is True
```

Unit — create `tests/unit/test_remote_attach_via_adapter.py` (AsyncMock pattern copied from `tests/unit/test_dap_attach_pathmappings.py`):

```python
"""remote_attach must spawn the adapter subprocess (client.start) for
adapters with the attach_via_adapter quirk, instead of dialing the
debuggee's DAP port directly (the debugpy path)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from tdb.languages.base import (
    AdapterQuirks,
    AdapterSpec,
    LanguageProfile,
    Presentation,
    ProfileCapabilities,
)
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController


class _MediatedAdapter(AdapterSpec):
    id = "mediated"
    quirks = AdapterQuirks(attach_via_adapter=True)

    def command(self):
        return ["true"]

    def launch_body(self, **kw):
        return {"request": "launch", "program": kw.get("program", "")}

    def attach_body(self, *, host, port, opts):
        return {"request": "attach", "host": host, "port": port}

    def pick_exception_filters(self, caps):
        return []


def _profile(quirk: bool) -> LanguageProfile:
    adapter = _MediatedAdapter()
    if not quirk:
        adapter.quirks = AdapterQuirks(attach_via_adapter=False)
    return LanguageProfile(
        id="x", display_name="X", adapter=adapter,
        presentation=Presentation(), capabilities=ProfileCapabilities(),
    )


async def _attach_with(profile: LanguageProfile):
    ctrl = DebugController(ServerEventHandler(), profile=profile)
    ctrl.client.start = AsyncMock()
    ctrl.client.connect = AsyncMock()
    ctrl.client.initialize = AsyncMock()
    ctrl.client.attach = AsyncMock(return_value=None)
    await ctrl.remote_attach(host="devbox", port=5678)
    return ctrl


async def test_quirk_true_spawns_adapter_not_tcp():
    ctrl = await _attach_with(_profile(quirk=True))
    ctrl.client.start.assert_awaited_once()
    ctrl.client.connect.assert_not_awaited()
    ctrl.client.attach.assert_awaited_once()


async def test_quirk_false_keeps_direct_tcp_connect():
    ctrl = await _attach_with(_profile(quirk=False))
    ctrl.client.connect.assert_awaited_once_with("devbox", 5678)
    ctrl.client.start.assert_not_awaited()
```

Integration — create `tests/integration/test_perl_adapter_attach.py`:

```python
import asyncio
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from .perl_adapter_harness import AdapterClient

pytestmark = pytest.mark.skipif(
    shutil.which("perl") is None
    or subprocess.run(["perl", "-e", "require v5.18"]).returncode != 0,
    reason="perl >= 5.18 required",
)

PKG_DIR = Path(__file__).resolve().parents[2] / "src/tdb/adapters/perl"

REMOTE_PROG = """\
use Devel::TdbRemote;
my $counter = 10;
open my $fh, '>', $ARGV[1] or die;
Devel::TdbRemote::listen($ARGV[0], '127.0.0.1');
print {$fh} "listening\\n"; close $fh;
Devel::TdbRemote::wait_for_client();
$counter += 1;
$counter += 20;
print "counter=$counter\\n";
"""


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]; s.close()
    return port


@pytest.fixture
def remote_debuggee(tmp_path):
    prog = tmp_path / "svc.pl"
    prog.write_text(REMOTE_PROG)
    ready = tmp_path / "ready"
    port = _free_port()
    proc = subprocess.Popen(
        ["perl", f"-I{PKG_DIR}", str(prog), str(port), str(ready)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    for _ in range(100):
        if ready.exists():
            break
        time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("debuggee never listened")
    yield proc, str(prog), port
    if proc.poll() is None:
        proc.kill()


async def test_attach_stop_step_inspect_continue(remote_debuggee, tmp_path):
    proc, prog, port = remote_debuggee
    c = AdapterClient()
    await c.start()
    try:
        await c.request("initialize", {"adapterID": "perl-tdb"})
        attach_fut = c.send("attach", {"host": "127.0.0.1", "port": port})
        await c.wait_event("initialized")
        await c.request("configurationDone")
        resp = await asyncio.wait_for(attach_fut, 30)
        assert resp["success"] is True
        stopped = await c.wait_event("stopped")
        assert stopped["body"]["reason"] == "entry"
        st = await c.request("stackTrace", {"threadId": 1})
        assert st["body"]["stackFrames"][0]["line"] == 7  # after wait_for_client()
        ev = await c.request("evaluate", {"expression": "$counter", "context": "repl"})
        assert ev["body"]["result"] == "10"
        await c.request("next")
        await c.wait_event("stopped")
        ev = await c.request("evaluate", {"expression": "$counter", "context": "repl"})
        assert ev["body"]["result"] == "11"
        pause = await c.request("pause", {"threadId": 1})
        # attach mode: gated
        assert pause["success"] in (True, False)
        await c.request("continue")
        out, _ = proc.communicate(timeout=15)
        assert "counter=31" in out
    finally:
        await c.stop()


async def test_attach_connection_refused_errors_helpfully(tmp_path):
    c = AdapterClient()
    await c.start()
    try:
        await c.request("initialize", {"adapterID": "perl-tdb"})
        fut = c.send("attach", {"host": "127.0.0.1", "port": _free_port()})
        resp = await asyncio.wait_for(fut, 30)
        assert resp["success"] is False
        assert "wait_for_client" in resp["message"]
    finally:
        await c.stop()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/integration/test_perl_adapter_attach.py tests/unit/test_languages_base.py -q`
Expected: attach tests fail with "unsupported command: attach"; quirk test fails with TypeError (unknown field).

- [ ] **Step 3: Implement**

(a) `base.py` — add to `AdapterQuirks`:

```python
    # True -> tdb spawns the adapter subprocess for attach too and sends
    # the DAP attach request through it (the adapter dials the debuggee).
    # False (debugpy) -> tdb connects straight to the remote DAP server.
    attach_via_adapter: bool = False
```

(b) `controller.py` — in `remote_attach`, replace `await self.client.connect(host, port)` with:

```python
        if self.profile.adapter.quirks.attach_via_adapter:
            await self.client.start()
        else:
            await self.client.connect(host, port)
```

(c) `server.py` — rename `self._launch_request` to `self._start_request` (both uses in Task 9's code), add:

```python
    async def _on_attach(self, request: Request) -> None:
        host = request.arguments.get("host", "127.0.0.1")
        port = request.arguments.get("port", 0)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), 10.0
            )
        except (OSError, asyncio.TimeoutError) as e:
            self.send_error(
                request,
                f"cannot connect to {host}:{port} ({e}) — has the program "
                "called Devel::TdbRemote::listen() and wait_for_client(), "
                "and is the port reachable?",
            )
            return
        self.session = PerlSession(
            on_output=self._forward_output, on_stop=self._on_unsolicited_stop
        )
        try:
            await self.session.attach_socket(reader, writer)
            loc = await self.session.helper("Devel::TdbHelper::location()")
        except PerlProtocolError as e:
            self.send_error(request, f"attach handshake failed: {e} [{e.tail}]")
            return
        if loc.get("version") != 1:
            self.send_error(
                request,
                f"protocol mismatch (debuggee helpers v{loc.get('version')}, "
                "adapter expects v1) — update Devel/TdbRemote.pm and "
                "helpers.pl on the remote host",
            )
            return
        self._stop_on_entry = True
        self._start_request = request
        self.send_event("initialized")
```

`_on_configurationDone` already sends the held response + entry stop —
it only needs the `_launch_request` → `_start_request` rename.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/integration -k perl -q && .venv/bin/pytest tests/unit -q`
Expected: all pass (launch flow unaffected by the rename).

- [ ] **Step 5: Commit**

```bash
git add src/tdb/adapters/perl/server.py src/tdb/languages/base.py src/tdb/session/controller.py tests/integration/test_perl_adapter_attach.py tests/unit/test_languages_base.py tests/unit/test_remote_attach_via_adapter.py
git commit -m "feat: perl adapter remote attach + attach_via_adapter quirk"
```

---

### Task 15: PerlProfile, detection, CLI

**Files:**
- Create: `src/tdb/languages/perl.py`
- Modify: `src/tdb/languages/registry.py`, `src/tdb/cli.py`
- Test: `tests/unit/test_perl_profile.py`, additions to `tests/unit/test_language_registry.py` and `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: `AdapterSpec`/`LanguageProfile` machinery, Task 14's quirk.
- Produces: `PerlAdapter(AdapterSpec)` (`id="perl-tdb"`, `quirks=AdapterQuirks(attach_via_adapter=True)`, `command() -> [sys.executable, "-m", "tdb.adapters.perl"]`, `launch_body` → `{"type": "perl", "request": "launch", "program", "args", "cwd", "stopOnEntry"}` + `"env"` if env + `"perl"` if interpreter override; `attach_body` → `{"type": "perl", "request": "attach", "host", "port"}`; `pick_exception_filters` → `[]`), `build_perl_profile(adapter=None, adapter_paths=None)` (rejects unknown adapter ids with LanguageNotSupportedError; `adapter_paths.get("perl")` = PERL INTERPRETER path override, passed to `PerlAdapter(perl_executable=...)`), profile: `id="perl"`, `display_name="Perl"`, `Presentation(lexer="perl")`, default `ProfileCapabilities()`. Registry: `.pl`/`.pm`/`.t` in `_EXTENSION_MAP` → `"perl"`, shebang `b"perl"` branch after the python one, `register("perl", build_perl_profile)`. CLI: `_resolve_language`'s remote-attach rejection becomes `if args.remote_attach and profile.id not in ("python", "perl"): parser.error(...)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_perl_profile.py`:

```python
import sys

import pytest

from tdb.dap.types import Capabilities
from tdb.languages import registry
from tdb.languages.base import LanguageNotSupportedError
from tdb.languages.perl import PerlAdapter, build_perl_profile


def test_profile_shape():
    p = build_perl_profile()
    assert p.id == "perl"
    assert p.adapter.id == "perl-tdb"
    assert p.presentation.lexer == "perl"
    assert p.capabilities.compute_step_units is None
    assert p.capabilities.task_inspection is False
    assert p.adapter.quirks.attach_via_adapter is True
    assert p.adapter.quirks.pre_arm_pause_on_attach is False


def test_registered_in_registry():
    assert "perl" in registry.known_languages()
    assert registry.resolve("perl").id == "perl"


def test_command_is_bundled_module():
    assert PerlAdapter().command() == [sys.executable, "-m", "tdb.adapters.perl"]


def test_launch_body_carries_perl_override():
    body = PerlAdapter(perl_executable="/opt/bin/perl").launch_body(
        program="/x/p.pl", args=["a"], cwd="/x", env={"K": "V"},
        stop_on_entry=True, console="internalConsole", opts={},
    )
    assert body == {
        "type": "perl", "request": "launch", "program": "/x/p.pl",
        "args": ["a"], "cwd": "/x", "stopOnEntry": True,
        "env": {"K": "V"}, "perl": "/opt/bin/perl",
    }


def test_launch_body_omits_optional_keys():
    body = PerlAdapter().launch_body(
        program="/x/p.pl", args=[], cwd="/x", env=None,
        stop_on_entry=False, console="internalConsole", opts={},
    )
    assert "env" not in body and "perl" not in body


def test_attach_body():
    body = PerlAdapter().attach_body(host="devbox", port=5678, opts={})
    assert body == {"type": "perl", "request": "attach",
                    "host": "devbox", "port": 5678}


def test_adapter_paths_names_the_interpreter():
    p = build_perl_profile(adapter_paths={"perl": "/opt/bin/perl"})
    body = p.adapter.launch_body(
        program="/x/p.pl", args=[], cwd="/x", env=None,
        stop_on_entry=False, console="internalConsole", opts={},
    )
    assert body["perl"] == "/opt/bin/perl"


def test_unknown_adapter_rejected():
    with pytest.raises(LanguageNotSupportedError):
        build_perl_profile(adapter="perl5db-xyz")


def test_no_exception_filters():
    assert build_perl_profile().adapter.pick_exception_filters(Capabilities()) == []
```

Append to `tests/unit/test_language_registry.py`:

```python
def test_detect_perl_extensions(tmp_path):
    from tdb.languages import registry

    for ext in (".pl", ".pm", ".t"):
        f = tmp_path / f"x{ext}"
        f.write_text("print 1;\n")
        assert registry.detect(str(f)) == "perl"


def test_detect_perl_shebang(tmp_path):
    from tdb.languages import registry

    f = tmp_path / "tool"
    f.write_text("#!/usr/bin/perl\nprint 1;\n")
    assert registry.detect(str(f)) == "perl"
```

Append to `tests/unit/test_cli.py`:

```python
def test_remote_attach_allowed_for_perl():
    args = parse_args(["--lang", "perl", "-r", "5678"])
    assert args.profile.id == "perl"
    assert args.attach_port == 5678


def test_remote_attach_still_rejected_for_cpp():
    with pytest.raises(SystemExit):
        parse_args(["--lang", "cpp", "-r", "5678"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_perl_profile.py tests/unit/test_language_registry.py tests/unit/test_cli.py -q`
Expected: import errors / detection errors / SystemExit for the perl -r case.

- [ ] **Step 3: Implement**

Create `src/tdb/languages/perl.py`:

```python
"""The Perl language profile.

The adapter is tdb's own bundled module (python -m tdb.adapters.perl)
driving stock perl5db, so AdapterNotFoundError cannot happen for the
adapter itself — a missing/old *perl interpreter* is reported by the
adapter at launch. Config twist: {"adapters": {"perl": "/path/perl"}}
names the perl interpreter to spawn, not the adapter binary.

Core-DAP capabilities only: no statement stepping, no task inspection,
no child-process tracking. Remote attach is adapter-mediated
(attach_via_adapter quirk): tdb spawns the adapter, the adapter dials
the Devel::TdbRemote listener inside the debuggee.
"""

from __future__ import annotations

import sys
from typing import Any

from tdb.languages.base import (
    AdapterQuirks,
    AdapterSpec,
    LanguageNotSupportedError,
    LanguageProfile,
    Presentation,
    ProfileCapabilities,
)


class PerlAdapter(AdapterSpec):
    id = "perl-tdb"
    quirks = AdapterQuirks(attach_via_adapter=True)

    def __init__(self, perl_executable: str | None = None) -> None:
        self._perl = perl_executable

    def command(self) -> list[str]:
        return [sys.executable, "-m", "tdb.adapters.perl"]

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
            "type": "perl",
            "request": "launch",
            "program": program,
            "args": args,
            "cwd": cwd,
            "stopOnEntry": stop_on_entry,
        }
        if env:
            body["env"] = env
        if self._perl:
            body["perl"] = self._perl
        return body

    def attach_body(
        self, *, host: str, port: int, opts: dict[str, Any]
    ) -> dict[str, Any]:
        return {"type": "perl", "request": "attach", "host": host, "port": port}

    def pick_exception_filters(self, caps) -> list[str]:
        return []


def build_perl_profile(
    adapter: str | None = None, adapter_paths: dict[str, str] | None = None
) -> LanguageProfile:
    if adapter not in (None, "perl-tdb"):
        raise LanguageNotSupportedError(
            f"unknown adapter {adapter!r} for perl (known: perl-tdb)"
        )
    return LanguageProfile(
        id="perl",
        display_name="Perl",
        adapter=PerlAdapter(perl_executable=(adapter_paths or {}).get("perl")),
        presentation=Presentation(lexer="perl"),
        capabilities=ProfileCapabilities(),
    )
```

`registry.py`: extend `_EXTENSION_MAP` with `".pl": "perl", ".pm": "perl", ".t": "perl"`; after the python-shebang branch add:

```python
    if head.startswith(b"#!") and b"perl" in head.splitlines()[0]:
        return "perl"
```

and at the bottom (import placement mirrors cpp):

```python
from tdb.languages.perl import build_perl_profile  # noqa: E402

register("perl", build_perl_profile)
```

`cli.py`: change the `_resolve_language` rejection to:

```python
        if args.remote_attach and profile.id not in ("python", "perl"):
            parser.error(
                f"--remote-attach supports Python and Perl debuggees only "
                f"(detected language: {profile.id})"
            )
```

Move that check OUT of the `if profile.id != "python":` block it currently lives in (it must run the same for every language; the python-only flag checks stay inside the block).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit -q`
Expected: all pass — including the profile-contract suite, which now parametrizes over python/cpp/perl automatically. If the contract test `test_expected_languages_are_registered` asserts a frozen set, extend it to include "perl".

- [ ] **Step 5: Commit**

```bash
git add src/tdb/languages/perl.py src/tdb/languages/registry.py src/tdb/cli.py tests/unit/test_perl_profile.py tests/unit/test_language_registry.py tests/unit/test_cli.py
git commit -m "feat: Perl language profile, detection, CLI remote-attach support"
```

---

### Task 16: packaging, docs, end-to-end controller tests

**Files:**
- Modify: `pyproject.toml`, `README.md`, `SKILL.md`
- Test: `tests/integration/test_perl_session.py` (controller-level e2e, launch + attach)

**Interfaces:**
- Consumes: everything.
- Produces: wheel data files, user docs, the final proof.

- [ ] **Step 1: Write the failing e2e test**

Create `tests/integration/test_perl_session.py`, modeled directly on `tests/integration/test_cpp_session.py` (read it first — reuse its `session` fixture shape, `_launch` helper with `profile=build_perl_profile()`, `_resume_and_wait`, WAIT=20.0):

```python
"""End-to-end: DebugController + bundled perl adapter + real perl."""
# pytestmark: skipif perl missing or < 5.18 (same expression as other perl tests)

PERL_SRC = """\
sub add {
    my ($a, $b) = @_;
    my $result = $a + $b;
    return $result;
}
my $x = 5;
my $y = add($x, 7);
print "total=$y\\n";
"""
BP_LINE = 6  # my $x = 5;

# Tests (write them fully, following test_cpp_session.py's idioms):
# 1. test_registry_detects_pl_as_perl: registry.detect(script) == "perl"
# 2. test_launch_entry_stop_breakpoint_variables:
#    _launch(stop_on_entry=True) -> phase STOPPED; add_breakpoint(BP_LINE);
#    _resume_and_wait(continue_) -> stack_frames[0].line == BP_LINE;
#    ctrl.evaluate("1 + 2") == "3"
# 3. test_step_into_and_out: breakpoint at line 7 (my $y = add...); step_in
#    -> frame name contains "add"; step_out -> back in main file scope
# 4. test_attach_via_tdbremote: spawn the Task 14 REMOTE_PROG fixture
#    (perl -I<pkg dir>), wait for ready-file, then
#    ctrl = DebugController(handler, profile=build_perl_profile());
#    await ctrl.remote_attach(host="127.0.0.1", port=port);
#    wait initialized; ctrl.do_configure(); wait stop; evaluate("$counter")
#    == "10"; continue to completion; child stdout has "counter=31".
```

The comment block above is the test list — write each as real code in
this step; the cpp file shows every idiom (fixtures, `_launch`,
`_resume_and_wait`, teardown). This test file is the gate for the
attach-pause decision recorded in the spec: if pause proved unreliable
it stays gated (Task 14 already gates it); no further action here.

- [ ] **Step 2: Run to verify current state**

Run: `.venv/bin/pytest tests/integration/test_perl_session.py -q`
Expected: tests fail only if something in Tasks 1-15 is actually broken; a clean pass on first run is acceptable here (this task's RED is the packaging check below).

- [ ] **Step 3: Packaging**

In `pyproject.toml`, extend package data:

```toml
  [tool.setuptools.package-data]
  tdb = ["README.md", "adapters/perl/helpers.pl", "adapters/perl/Devel/TdbRemote.pm"]
```

Verify: `.venv/bin/python -m build --wheel 2>/dev/null || .venv/bin/pip wheel . -w /tmp/perlwheel --no-deps` then `unzip -l` the wheel and confirm both `.pl`/`.pm` files are inside. (If neither build tool is available, `uv pip install -e .` + `python -c "from tdb.adapters.perl.session import helpers_path; print(helpers_path())"` is the fallback check.)

- [ ] **Step 4: Docs**

README.md:
- Languages table: add row `| Perl | perl-tdb (bundled) | needs perl ≥ 5.18 on PATH (or {"adapters": {"perl": ...}}) | core debugging + remote attach |`
- "Language detection and selection": add `.pl`/`.pm`/`.t` and the perl shebang to the detection list.
- New `### Perl` subsection under Multi-Language Debugging covering: launch example (`tdb script.pl`), the remote-attach walkthrough (the exact `use Devel::TdbRemote; listen(); wait_for_client();` snippet + `tdb --lang perl -r host:5678`), the copy-two-files recipe for remote hosts (`Devel/TdbRemote.pm` + `helpers.pl` on PERL5LIB), the arming caveat (use-line-first / `-d:TdbRemote` / PERL5OPT), the PadWalker note (lexicals in outer frames degrade without it), and pause being unavailable in attach mode.
- Configuration section: document `{"adapters": {"perl": "/path/to/perl"}}` = interpreter override.

SKILL.md:
- Title/intro: add Perl to the language list.
- Multi-language notes: `.pl` auto-detection; `debug_attach` works for Perl debuggees prepared with Devel::TdbRemote; `tasks`/`processes`/`wait_graph` remain Python-only.

- [ ] **Step 5: Full-suite run and commit**

Run: `.venv/bin/pytest tests/unit -q && .venv/bin/pytest tests/integration -q`
Expected: all green (perl, cpp, gdb, debugpy suites).

```bash
git add pyproject.toml README.md SKILL.md tests/integration/test_perl_session.py
git commit -m "feat: perl packaging, docs, end-to-end controller tests"
```

---

## Plan Self-Review Notes (already applied)

- Spec coverage: launch topology (T8/T9), prompt protocol (T2/T8), helper inventory (T5-T7), breakpoint snap via breakable (T10), variables/specials/rich dumping (T6/T7/T11), evaluate wrap trick (T7/T11), pause launch-mode + attach gating (T12/T14), DAP source for remote files (T12; tdb's controller already calls DAP `source` for missing files — no tdb-side change needed), TdbRemote listen/wait (T13), adapter-mediated attach + quirk (T14), profile/detection/CLI (T15), packaging + docs + e2e (T16), protocol-version handshake (T13/T14), preflight version check (T9), error surfaces with socket tail (T8).
- Deviation from spec, intentional: spec's "adapter keeps variablesReference → (frame, access-path)" is implemented as debuggee-side stash ids (`%REG`) + adapter `RefRegistry` — access-path strings break on hash keys needing quoting and on non-top frames; stash ids are strictly more robust. Spec intent (lazy one-level expansion, per-stop invalidation) is preserved.
- Deviation, intentional: spec's `wire.py` extraction is unnecessary — `tdb/dap/protocol.py` already holds the shared framing; Task 1 adds only the missing serializers.
- Attach-mode pause: implemented as gated (clear error) per the spec's fallback arm; the control-channel mechanism is future work listed in docs.

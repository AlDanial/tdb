# Ruby (rdbg) Core Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Debug Ruby programs in tdb via the debug gem's `rdbg`, at Perl's
feature tier (launch, breakpoints/conditions, stack/variables/evaluate
console with completion, fatal-error modal, remote attach, `--terminal`,
`--run`, `--record`/`--replay`), plus ungating File→Open for all languages.

**Architecture:** A thin bundled stdio↔socket DAP proxy
(`python -m tdb.adapters.ruby`) spawns `rdbg --open` and passes DAP frames
through with seq renumbering. The client's `launch` is rewritten as an rdbg
`attach` with `nonstop: !stopOnEntry` (rdbg's DAP `launch` handler ignores
stop-on-entry). The proxy pumps rdbg's stdout/stderr pipes into DAP `output`
events. Remote attach bypasses the proxy (direct TCP — rdbg IS a DAP server).

**Tech Stack:** Python 3.12+ asyncio, existing `tdb.dap` wire helpers,
Ruby's debug gem (`rdbg`) ≥ 1.9, pytest + pytest-asyncio (`asyncio_mode =
"auto"` — no `@pytest.mark.asyncio` needed on new tests).

**Spec:** `docs/superpowers/specs/2026-08-21-ruby-support-design.md`

## Global Constraints

- Branch: `add-ruby-support`. Repo root: `/home/al/projects/tdbg/work`. Only
  search/edit under this repo.
- Run tests with `uv run pytest <path> -v` (never bare `pip`; use `uv pip`
  if a package is ever needed).
- rdbg floor: debug gem ≥ 1.9, Ruby ≥ 3.1. All integration tests must skip
  cleanly when `rdbg` is absent (`rdbg_ok()` guard).
- Never use `rdbg --open=vscode` (it launches VS Code) and never pass
  `--nonstop` on the rdbg command line (the proxy controls nonstop via the
  DAP attach argument).
- AF_UNIX socket paths must stay short (< ~90 chars); fall back to TCP on
  127.0.0.1 otherwise, and always TCP on Windows. No `--cookie` (it is part
  of rdbg's own protocol greeting, not DAP; 127.0.0.1 binding is the
  boundary).
- rdbg subprocesses run in their own process group (`start_new_session=True`
  on POSIX, `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP` on Windows)
  and must be killed on every exit path (disconnect, terminate, proxy
  death) — no orphans.
- Bare `asyncio.ensure_future()` tasks are garbage-collectable mid-flight;
  always hold a strong reference (repo-wide pitfall).
- Cross-platform: any path/subprocess decision needs a Windows branch or a
  comment explaining why POSIX-only is fine.
- DAP launch body `type` key for Ruby is `"ruby"`; adapter id is `"rdbg"`;
  language id is `"ruby"`.
- Commit after every task with the message given in its final step. Append
  to every commit message:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

---

### Task 1: `parse_ruby_error`

**Files:**
- Modify: `src/tdb/languages/errors.py` (append at end of file)
- Test: `tests/unit/test_error_parsers.py` (append at end of file)

**Interfaces:**
- Consumes: `ErrorFrame`, `ParsedError` from `tdb.languages.base` (already
  imported at the top of errors.py).
- Produces: `parse_ruby_error(stderr: str, exit_code: int | None = None)
  -> ParsedError | None` — Task 2 wires it into the Ruby profile as
  `presentation.parse_error`.

Ruby (captured via pipes, i.e. non-tty) prints fatal errors bottom-up:

```
/w/boom.rb:2:in `inner': divided by 0 (ZeroDivisionError)
	from /w/boom.rb:6:in `outer'
	from /w/boom.rb:9:in `<main>'
```

Ruby ≥ 3.4 uses straight quotes and may prefix the receiver:
`/w/boom.rb:2:in 'Object#inner': ...`. Syntax errors have their own shape:
`/w/bad.rb:3: syntax error, unexpected end-of-input` (3.3) or
`/w/bad.rb:3: syntax error found (SyntaxError)` (3.4+). `ParsedError.frames`
must be OUTERMOST-first with the failing frame last (Python/Perl
convention); Ruby prints innermost-first, so the `from` frames are reversed
and the head frame appended last.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_error_parsers.py`:

```python
# ---- ruby ----

from tdb.languages.errors import parse_ruby_error  # noqa: E402

RUBY_CLASSIC = """\
/w/boom.rb:2:in `inner': divided by 0 (ZeroDivisionError)
\tfrom /w/boom.rb:6:in `outer'
\tfrom /w/boom.rb:9:in `<main>'
"""

RUBY_34_QUOTING = """\
/w/boom.rb:2:in 'Object#inner': divided by 0 (ZeroDivisionError)
\tfrom /w/boom.rb:6:in 'Object#outer'
\tfrom /w/boom.rb:9:in '<main>'
"""


def test_ruby_classic_traceback():
    parsed = parse_ruby_error(RUBY_CLASSIC, 1)
    assert parsed is not None
    assert parsed.header == "Ruby error:"
    assert parsed.message == "divided by 0 (ZeroDivisionError)"
    # OUTERMOST-first, failing frame last
    assert [(f.path, f.line, f.func) for f in parsed.frames] == [
        ("/w/boom.rb", 9, ""),  # <main> -> "" so frame_placeholder applies
        ("/w/boom.rb", 6, "outer"),
        ("/w/boom.rb", 2, "inner"),
    ]
    assert "divided by 0" in parsed.detail
    assert "from /w/boom.rb:6" in parsed.detail


def test_ruby_34_quoting_variant():
    parsed = parse_ruby_error(RUBY_34_QUOTING, 1)
    assert parsed is not None
    assert [f.func for f in parsed.frames] == ["", "Object#outer", "Object#inner"]


def test_ruby_error_amid_earlier_stderr_noise():
    parsed = parse_ruby_error("some warning\n" + RUBY_CLASSIC, 1)
    assert parsed is not None
    assert parsed.message == "divided by 0 (ZeroDivisionError)"


def test_ruby_single_frame_error():
    parsed = parse_ruby_error("/w/x.rb:3:in `<main>': boom (RuntimeError)\n", 1)
    assert parsed is not None
    assert parsed.frames == [ErrorFrame(path="/w/x.rb", line=3, func="")]


def test_ruby_syntax_error_old_shape():
    parsed = parse_ruby_error("/w/bad.rb:3: syntax error, unexpected end-of-input\n", 1)
    assert parsed is not None
    assert parsed.frames == [ErrorFrame(path="/w/bad.rb", line=3, func="")]
    assert "syntax error" in parsed.message


def test_ruby_syntax_error_34_shape():
    text = "/w/bad.rb:2: syntax error found (SyntaxError)\n  1 | x = 1\n> 2 | if\n"
    parsed = parse_ruby_error(text, 1)
    assert parsed is not None
    assert parsed.frames[0].line == 2
    assert "> 2 | if" in parsed.detail


def test_ruby_garbage_returns_none():
    assert parse_ruby_error("plain stderr chatter\n", 1) is None
    assert parse_ruby_error("", None) is None
```

Note: `ErrorFrame` is already imported at the top of the test file — check;
if not, add `from tdb.languages.base import ErrorFrame`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_error_parsers.py -v -k ruby`
Expected: FAIL — `ImportError: cannot import name 'parse_ruby_error'`

- [ ] **Step 3: Implement the parser**

Append to `src/tdb/languages/errors.py`:

```python
# First line of a fatal ruby exception (pipe/non-tty output is always
# bottom-up), e.g.
#   /w/boom.rb:2:in `inner': divided by 0 (ZeroDivisionError)
# Ruby <= 3.3 quotes the method as `inner'; >= 3.4 as 'Object#inner'.
_RUBY_HEAD_RE = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+):in [`'](?P<func>[^`']+)'"
    r": (?P<msg>.+) \((?P<cls>[A-Z]\w*(?:::\w+)*)\)\s*$"
)

# A "\tfrom FILE:LINE:in `func'" backtrace frame (innermost-caller-first).
_RUBY_FRAME_RE = re.compile(
    r"^\s*from (?P<path>.+?):(?P<line>\d+):in [`'](?P<func>[^`']+)'\s*$"
)

# Syntax errors have no exception-style head line:
#   /w/bad.rb:3: syntax error, unexpected end-of-input        (<= 3.3)
#   /w/bad.rb:2: syntax error found (SyntaxError)             (>= 3.4)
_RUBY_SYNTAX_RE = re.compile(r"^(?P<path>.+?):(?P<line>\d+): (?P<msg>syntax error.*)$")


def _ruby_func(name: str) -> str:
    # "" lets Presentation.frame_placeholder ("<main>") label the frame.
    return "" if name == "<main>" else name


def parse_ruby_error(stderr: str, exit_code: int | None = None) -> ParsedError | None:
    """Parse a fatal Ruby exception or syntax error out of raw stderr.

    ``exit_code`` is accepted for signature parity with the other
    parsers but ignored: the ``FILE:LINE:in `meth': msg (Class)`` head
    line is an unambiguous fatal-error signal on its own (Ruby prints
    it only for exceptions that terminate the process; rescued
    exceptions produce no such stderr line unless the program prints
    one itself, which is the same accepted ambiguity Python's parser
    has with `traceback.print_exc()`).
    """
    lines = stderr.splitlines()
    head = None
    head_idx = 0
    for i, ln in enumerate(lines):
        m = _RUBY_HEAD_RE.match(ln)
        if m:
            head, head_idx = m, i
            break
    if head is None:
        for i, ln in enumerate(lines):
            m = _RUBY_SYNTAX_RE.match(ln)
            if m:
                return ParsedError(
                    header="Ruby error:",
                    message=m.group("msg"),
                    frames=[
                        ErrorFrame(
                            path=m.group("path"),
                            line=int(m.group("line")),
                            func="",
                        )
                    ],
                    # keep the caret/source context lines that follow
                    detail="\n".join(lines[i:]).rstrip(),
                )
        return None

    call_frames: list[ErrorFrame] = []
    detail_lines = [lines[head_idx]]
    for ln in lines[head_idx + 1 :]:
        fm = _RUBY_FRAME_RE.match(ln)
        if not fm:
            break  # e.g. a "... N levels..." truncation marker ends frames
        call_frames.append(
            ErrorFrame(
                path=fm.group("path"),
                line=int(fm.group("line")),
                func=_ruby_func(fm.group("func")),
            )
        )
        detail_lines.append(ln)

    # Ruby prints innermost-first; ParsedError wants OUTERMOST-first
    # with the failing frame last (same reordering as perl's parser).
    frames = list(reversed(call_frames)) + [
        ErrorFrame(
            path=head.group("path"),
            line=int(head.group("line")),
            func=_ruby_func(head.group("func")),
        )
    ]
    return ParsedError(
        header="Ruby error:",
        message=f"{head.group('msg')} ({head.group('cls')})",
        frames=frames,
        detail="\n".join(detail_lines),
    )
```

Wait — `test_ruby_classic_traceback` expects the innermost frame's func to
be `"inner"`, i.e. the head frame keeps its func. Note the expected frames:
head frame is LAST and carries `func="inner"`; the `<main>` frame maps to
`""`. The code above does exactly that via `_ruby_func`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_error_parsers.py -v`
Expected: all PASS (including the pre-existing python/perl cases).

- [ ] **Step 5: Commit**

```bash
git add src/tdb/languages/errors.py tests/unit/test_error_parsers.py
git commit -m "feat: parse_ruby_error for the fatal-error modal"
```

---

### Task 2: Ruby language profile + registry

**Files:**
- Create: `src/tdb/languages/ruby.py`
- Modify: `src/tdb/languages/registry.py`
- Test: `tests/unit/test_ruby_profile.py`, `tests/unit/test_registry_ruby.py`

**Interfaces:**
- Consumes: `AdapterSpec`, `AdapterQuirks`, `LanguageProfile`,
  `Presentation`, `ProfileCapabilities`, `LanguageNotSupportedError` from
  `tdb.languages.base`; `parse_ruby_error` from Task 1.
- Produces: `RdbgAdapter(rdbg_executable: str | None = None)` with
  `id="rdbg"`, `command() == [sys.executable, "-m", "tdb.adapters.ruby"]`,
  `launch_body(...)` (keys: type/request/program/args/cwd/stopOnEntry/
  console, optional env, optional `rdbg`), `attach_body(...)` (`{"type":
  "ruby", "request": "attach"}`); `build_ruby_profile(adapter=None,
  adapter_paths=None) -> LanguageProfile` registered as `"ruby"`.
  Tasks 4–10 spawn `python -m tdb.adapters.ruby` and read the `rdbg` key
  from the launch body.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_ruby_profile.py` (model: `test_perl_profile.py`):

```python
import sys

import pytest

from tdb.dap.types import Capabilities
from tdb.languages import registry
from tdb.languages.base import LanguageNotSupportedError
from tdb.languages.ruby import RdbgAdapter, build_ruby_profile


def test_profile_shape():
    p = build_ruby_profile()
    assert p.id == "ruby"
    assert p.display_name == "Ruby"
    assert p.adapter.id == "rdbg"
    assert p.presentation.lexer == "ruby"
    assert p.presentation.frame_placeholder == "<main>"
    assert p.presentation.parse_error is not None
    assert p.capabilities.compute_step_units is None
    assert p.capabilities.task_inspection is False
    assert p.capabilities.child_process_strategy is None
    assert p.capabilities.pause_while_running is True
    # remote attach is DIRECT (rdbg is a DAP server), unlike perl
    assert p.adapter.quirks.attach_via_adapter is False
    assert p.adapter.quirks.pre_arm_pause_on_attach is False


def test_registered_in_registry():
    assert "ruby" in registry.known_languages()
    assert registry.resolve("ruby").id == "ruby"


def test_command_is_bundled_proxy():
    assert RdbgAdapter().command() == [sys.executable, "-m", "tdb.adapters.ruby"]


def test_launch_body_carries_rdbg_override():
    body = RdbgAdapter(rdbg_executable="/opt/bin/rdbg").launch_body(
        program="/x/p.rb",
        args=["a"],
        cwd="/x",
        env={"K": "V"},
        stop_on_entry=True,
        console="internalConsole",
        opts={},
    )
    assert body == {
        "type": "ruby",
        "request": "launch",
        "program": "/x/p.rb",
        "args": ["a"],
        "cwd": "/x",
        "stopOnEntry": True,
        "console": "internalConsole",
        "env": {"K": "V"},
        "rdbg": "/opt/bin/rdbg",
    }


def test_launch_body_omits_optional_keys():
    body = RdbgAdapter().launch_body(
        program="/x/p.rb",
        args=[],
        cwd="/x",
        env=None,
        stop_on_entry=False,
        console="internalConsole",
        opts={},
    )
    assert "env" not in body and "rdbg" not in body
    assert body["stopOnEntry"] is False


def test_attach_body_minimal():
    body = RdbgAdapter().attach_body(host="devbox", port=5678, opts={})
    assert body == {"type": "ruby", "request": "attach"}


def test_attach_body_rejects_path_mappings():
    with pytest.raises(LanguageNotSupportedError):
        RdbgAdapter().attach_body(
            host="devbox",
            port=5678,
            opts={"path_mappings": [("/local", "/remote")]},
        )


def test_adapter_paths_names_rdbg():
    p = build_ruby_profile(adapter_paths={"rdbg": "/opt/bin/rdbg"})
    body = p.adapter.launch_body(
        program="/x/p.rb",
        args=[],
        cwd="/x",
        env=None,
        stop_on_entry=False,
        console="internalConsole",
        opts={},
    )
    assert body["rdbg"] == "/opt/bin/rdbg"


def test_unknown_adapter_rejected():
    with pytest.raises(LanguageNotSupportedError):
        build_ruby_profile(adapter="byebug")


def test_no_exception_filters():
    assert build_ruby_profile().adapter.pick_exception_filters(Capabilities()) == []
```

`tests/unit/test_registry_ruby.py` (model: `test_registry_bash.py`):

```python
from tdb.languages import registry


def test_rb_extension_detects_ruby(tmp_path):
    p = tmp_path / "x.rb"
    p.write_text("puts 1\n")
    assert registry.detect(str(p)) == "ruby"


def test_ruby_shebang_detects_ruby(tmp_path):
    p = tmp_path / "script"
    p.write_text("#!/usr/bin/env ruby\nputs 1\n")
    assert registry.detect(str(p)) == "ruby"


def test_resolve_default_adapter():
    profile = registry.resolve("ruby")
    assert profile.adapter.id == "rdbg"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_ruby_profile.py tests/unit/test_registry_ruby.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tdb.languages.ruby'`

- [ ] **Step 3: Implement `src/tdb/languages/ruby.py`**

```python
"""The Ruby language profile.

The adapter is tdb's bundled stdio<->socket DAP proxy
(python -m tdb.adapters.ruby) in front of the debug gem's `rdbg`,
which speaks DAP natively but only over a socket. AdapterNotFoundError
cannot happen for the adapter itself — a missing/too-old *rdbg* is
reported by the proxy at launch. Config twist (same shape as perl):
{"adapters": {"rdbg": "/path/to/rdbg"}} names the rdbg executable the
proxy should spawn.

Core-DAP capabilities only: no statement stepping, no task inspection,
no child-process tracking (fork support is a follow-on project).
Remote attach is DIRECT (attach_via_adapter=False): rdbg is a DAP
server, so tdb TCP-connects to it exactly like debugpy.
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
from tdb.languages.errors import parse_ruby_error


class RdbgAdapter(AdapterSpec):
    id = "rdbg"
    quirks = AdapterQuirks()

    def __init__(self, rdbg_executable: str | None = None) -> None:
        self._rdbg = rdbg_executable

    def command(self) -> list[str]:
        return [sys.executable, "-m", "tdb.adapters.ruby"]

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
            "type": "ruby",
            "request": "launch",
            "program": program,
            "args": args,
            "cwd": cwd,
            "stopOnEntry": stop_on_entry,
            "console": console,
        }
        if env:
            body["env"] = env
        if self._rdbg:
            body["rdbg"] = self._rdbg
        return body

    def attach_body(
        self, *, host: str, port: int, opts: dict[str, Any]
    ) -> dict[str, Any]:
        # Direct TCP attach: the host/port were already used to open the
        # socket (dap/client.py connect); rdbg's attach request needs no
        # address. Path mapping would use rdbg's localfsMap argument,
        # whose format is unverified — refuse rather than misbehave.
        if opts.get("path_mappings"):
            raise LanguageNotSupportedError(
                "--local-root/--remote-root path mappings are not "
                "supported for ruby remote attach yet"
            )
        return {"type": "ruby", "request": "attach"}

    def pick_exception_filters(self, caps) -> list[str]:
        # rdbg's filters ("any", "RuntimeError") trigger on *rescued*
        # exceptions too — far too noisy as defaults.
        return []


def build_ruby_profile(
    adapter: str | None = None, adapter_paths: dict[str, str] | None = None
) -> LanguageProfile:
    if adapter not in (None, "rdbg"):
        raise LanguageNotSupportedError(
            f"unknown adapter {adapter!r} for ruby (known: rdbg)"
        )
    return LanguageProfile(
        id="ruby",
        display_name="Ruby",
        adapter=RdbgAdapter(rdbg_executable=(adapter_paths or {}).get("rdbg")),
        presentation=Presentation(
            lexer="ruby",
            parse_error=parse_ruby_error,
            frame_placeholder="<main>",
        ),
        capabilities=ProfileCapabilities(pause_while_running=True),
    )
```

- [ ] **Step 4: Wire the registry**

In `src/tdb/languages/registry.py`:

1. Add to `_EXTENSION_MAP`: `".rb": "ruby",` (keep the dict's grouping —
   put it after the `.t` perl entry).
2. In `detect()`, add after the perl shebang branch:

```python
    if head.startswith(b"#!") and b"ruby" in head.splitlines()[0]:
        return "ruby"
```

3. At the bottom, after the tcsh registration:

```python
from tdb.languages.ruby import build_ruby_profile  # noqa: E402

register("ruby", build_ruby_profile)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_ruby_profile.py tests/unit/test_registry_ruby.py tests/unit/test_profile_contract.py tests/unit/test_language_registry.py -v`
Expected: all PASS. `test_profile_contract.py` is parametrized over
`registry.known_languages()` and now covers ruby automatically. If
`test_language_registry.py` asserts an exact language list, add `"ruby"`
to it.

- [ ] **Step 6: Commit**

```bash
git add src/tdb/languages/ruby.py src/tdb/languages/registry.py tests/unit/test_ruby_profile.py tests/unit/test_registry_ruby.py
git commit -m "feat: ruby language profile, .rb/shebang detection, rdbg adapter spec"
```

---

### Task 3: CLI remote-attach allowlist

**Files:**
- Modify: `src/tdb/cli.py:423-427`
- Test: `tests/unit/test_cli.py` (append)

**Interfaces:**
- Consumes: `build_ruby_profile` registration from Task 2 (parse_args
  resolves `--lang ruby`).
- Produces: `parse_args(["-r", "5678", "--lang", "ruby"])` succeeds with
  `args.profile.id == "ruby"`.

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_cli.py`)

```python
def test_remote_attach_allows_ruby():
    args = parse_args(["-r", "5678", "--lang", "ruby"])
    assert args.profile.id == "ruby"
    assert args.profile.adapter.id == "rdbg"


def test_remote_attach_still_rejects_bash():
    with pytest.raises(SystemExit):
        parse_args(["-r", "5678", "--lang", "bash"])
```

Check first whether a bash/tcsh remote-attach rejection test already
exists in the file; if so, skip the second test. Ensure `pytest` is
imported at the top (it is).

- [ ] **Step 2: Run tests to verify the ruby one fails**

Run: `uv run pytest tests/unit/test_cli.py -v -k remote_attach`
Expected: `test_remote_attach_allows_ruby` FAILS with `SystemExit`
(current gate rejects ruby).

- [ ] **Step 3: Widen the gate**

In `src/tdb/cli.py`, replace:

```python
    if args.remote_attach and profile.id not in ("python", "perl"):
        parser.error(
            f"--remote-attach supports Python and Perl debuggees only "
            f"(detected language: {profile.id})"
        )
```

with:

```python
    if args.remote_attach and profile.id not in ("python", "perl", "ruby"):
        parser.error(
            f"--remote-attach supports Python, Perl, and Ruby debuggees "
            f"only (detected language: {profile.id})"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_cli.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/cli.py tests/unit/test_cli.py
git commit -m "feat: allow --remote-attach for ruby debuggees"
```

---

### Task 4: Proxy scaffolding — package, seq translation, filters

**Files:**
- Create: `src/tdb/adapters/ruby/__init__.py`
- Create: `src/tdb/adapters/ruby/__main__.py`
- Create: `src/tdb/adapters/ruby/server.py` (translator + constants +
  transport helpers only in this task; `RubyDapServer` comes in Task 5)
- Test: `tests/unit/test_ruby_proxy_units.py`

**Interfaces:**
- Consumes: `tdb.dap.protocol.encode_message/read_message` (existing).
- Produces (Task 5 builds on these exact names in the same module):
  `SeqTranslator` with methods `next_client_seq() -> int`,
  `next_rdbg_seq() -> int`, `client_request_to_rdbg(dict) -> dict`,
  `rdbg_response_to_client(dict) -> dict | None`,
  `rdbg_event_to_client(dict) -> dict`, `rdbg_request_to_client(dict) ->
  dict`, `client_response_to_rdbg(dict) -> dict | None`;
  `pick_transport() -> _Transport` (fields `rdbg_args: list[str]`,
  `connect`, `cleanup`); `_free_port() -> int`;
  `async _rdbg_version(rdbg: str) -> tuple[int, int]`; constants
  `CAPABILITIES`, `MIN_DEBUG_GEM = (1, 9)`, `RDBG_HINT`,
  `_BANNER_PREFIX = "DEBUGGER: "`, `_REPL_NOTICE = "Ruby REPL:"`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_ruby_proxy_units.py`:

```python
"""Pure-logic tests for the ruby proxy: seq translation and transport
selection. No Ruby/rdbg required."""

import os

import pytest

from tdb.adapters.ruby.server import (
    CAPABILITIES,
    MIN_DEBUG_GEM,
    SeqTranslator,
    _free_port,
    pick_transport,
)


def test_client_request_roundtrip():
    t = SeqTranslator()
    fwd = t.client_request_to_rdbg({"seq": 41, "type": "request", "command": "next"})
    assert fwd["seq"] == 1 and fwd["command"] == "next"
    resp = t.rdbg_response_to_client(
        {
            "seq": 9,
            "type": "response",
            "request_seq": fwd["seq"],
            "command": "next",
            "success": True,
        }
    )
    assert resp["request_seq"] == 41
    assert resp["seq"] == 1  # first message the proxy sends to the client


def test_proxy_originated_response_is_swallowed():
    t = SeqTranslator()
    # a response to a request the proxy sent itself (no client mapping)
    assert (
        t.rdbg_response_to_client(
            {
                "seq": 1,
                "type": "response",
                "request_seq": 999,
                "command": "initialize",
                "success": True,
            }
        )
        is None
    )


def test_events_are_resequenced_monotonically():
    t = SeqTranslator()
    e1 = t.rdbg_event_to_client({"seq": 50, "type": "event", "event": "output"})
    e2 = t.rdbg_event_to_client({"seq": 51, "type": "event", "event": "stopped"})
    assert (e1["seq"], e2["seq"]) == (1, 2)


def test_reverse_request_roundtrip():
    t = SeqTranslator()
    fwd = t.rdbg_request_to_client(
        {"seq": 7, "type": "request", "command": "runInTerminal"}
    )
    back = t.client_response_to_rdbg(
        {
            "seq": 3,
            "type": "response",
            "request_seq": fwd["seq"],
            "command": "runInTerminal",
            "success": True,
        }
    )
    assert back["request_seq"] == 7


def test_client_response_without_mapping_is_swallowed():
    t = SeqTranslator()
    assert (
        t.client_response_to_rdbg(
            {
                "seq": 3,
                "type": "response",
                "request_seq": 123,
                "command": "x",
                "success": True,
            }
        )
        is None
    )


def test_capabilities_omit_step_back():
    # rdbg advertises supportsStepBack; tdb has no step-back UI, so the
    # proxy's static capability dict must not re-advertise it.
    assert "supportsStepBack" not in CAPABILITIES
    assert CAPABILITIES["supportsConfigurationDoneRequest"] is True
    assert CAPABILITIES["supportsConditionalBreakpoints"] is True
    assert CAPABILITIES["supportsCompletionsRequest"] is True


def test_free_port_is_bindable():
    import socket

    port = _free_port()
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))  # racy in theory; fine as a smoke test


@pytest.mark.skipif(os.name == "nt", reason="unix-socket branch")
def test_pick_transport_prefers_unix_socket():
    tr = pick_transport()
    try:
        assert tr.rdbg_args[0] in ("--sock-path", "--port")
        if tr.rdbg_args[0] == "--sock-path":
            assert len(tr.rdbg_args[1]) < 90
    finally:
        tr.cleanup()


def test_min_debug_gem():
    assert MIN_DEBUG_GEM == (1, 9)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_ruby_proxy_units.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tdb.adapters.ruby'`

- [ ] **Step 3: Create the package**

`src/tdb/adapters/ruby/__init__.py`:

```python
"""tdb's Ruby adapter: a DAP proxy in front of the debug gem's rdbg."""
```

`src/tdb/adapters/ruby/__main__.py` (same wiring as perl/bash):

```python
"""python -m tdb.adapters.ruby — run the Ruby DAP proxy on stdio."""

import asyncio
import sys

from tdb.adapters.ruby.server import RubyDapServer


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
    await RubyDapServer(reader, writer).run()


if __name__ == "__main__":
    asyncio.run(main())
```

(`RubyDapServer` does not exist until Task 5; `__main__` importing it is
fine — nothing imports `__main__` in unit tests.)

- [ ] **Step 4: Create `server.py` with the Task-4 pieces**

```python
"""DAP proxy between tdb (stdio) and Ruby's rdbg (socket).

rdbg — the debug gem's CLI (>= 1.9) — speaks DAP natively, but only
over a UNIX/TCP socket, and its DAP `launch` handler hardcodes
nonstop mode (server_dap.rb: `@nonstop = true`). tdb expects a stdio
adapter it can spawn. This module bridges the two:

  tdb  --stdio-->  RubyDapServer  --socket-->  rdbg --open -- prog.rb

It is a store-and-forward pipe, not a debugger: every request without
a local handler is forwarded to rdbg with its seq renumbered, and
rdbg's events/responses flow back the same way. Locally handled:

  initialize — answered from static CAPABILITIES (rdbg isn't running yet)
  launch     — spawns rdbg, connects, then forwards the request AS an
               rdbg `attach` with nonstop=(not stopOnEntry): rdbg's
               DAP `attach` honors nonstop and emits stopped("pause")
               after configurationDone, which `launch` never does.
  disconnect / terminate — kill the rdbg process group (no orphans).

rdbg does NOT forward debuggee stdout/stderr as DAP output events; the
proxy pumps the child's pipes into `output` events itself, filtering
rdbg's own "DEBUGGER:" banner lines.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from tdb.dap.messages import parse_message
from tdb.dap.protocol import encode_message, read_message
from tdb.dap.reverse import ReverseRequester, ReverseRequestError

log = logging.getLogger(__name__)

# Mirrors what rdbg (debug gem 1.11) actually advertises, minus
# supportsStepBack: rdbg supports it but tdb has no step-back UI, so
# re-advertising it would be a lie to tdb's capability checks.
CAPABILITIES = {
    "supportsConfigurationDoneRequest": True,
    "supportsConditionalBreakpoints": True,
    "supportsCompletionsRequest": True,
    "supportsEvaluateForHovers": True,
    "supportsFunctionBreakpoints": True,
    "supportsExceptionFilterOptions": True,
    "supportsTerminateRequest": True,
    "supportTerminateDebuggee": True,
    "exceptionBreakpointFilters": [
        {
            "filter": "any",
            "label": "rescue any exception",
            "supportsCondition": True,
        },
        {
            "filter": "RuntimeError",
            "label": "rescue RuntimeError",
            "supportsCondition": True,
        },
    ],
}

MIN_DEBUG_GEM = (1, 9)

RDBG_HINT = (
    "rdbg not found on PATH — install Ruby's debug gem "
    '(`gem install debug`), or set {"adapters": {"rdbg": '
    '"/path/to/rdbg"}} in tdb\'s config.json'
)

# rdbg's own stderr chatter ("Debugger can attach via ...",
# "Connected.") — adapter noise, not program output.
_BANNER_PREFIX = "DEBUGGER: "

# rdbg greets DAP clients with a "Ruby REPL: ..." console output event.
_REPL_NOTICE = "Ruby REPL:"


class SeqTranslator:
    """Renumber seq/request_seq between the two sides of the proxy.

    Each side sees a gapless seq space owned by the proxy. A forwarded
    request remembers the originator's seq so the answering side's
    response can be restamped with it; responses to requests the proxy
    itself originated (its own initialize/terminate to rdbg) have no
    mapping and translate to None — exactly what the proxy wants, since
    it must swallow those.
    """

    def __init__(self) -> None:
        self._client_seq = 0  # last seq sent TO the client
        self._rdbg_seq = 0  # last seq sent TO rdbg
        self._from_client: dict[int, int] = {}  # rdbg-side seq -> client seq
        self._from_rdbg: dict[int, int] = {}  # client-side seq -> rdbg seq

    def next_client_seq(self) -> int:
        self._client_seq += 1
        return self._client_seq

    def next_rdbg_seq(self) -> int:
        self._rdbg_seq += 1
        return self._rdbg_seq

    def client_request_to_rdbg(self, msg: dict) -> dict:
        out = dict(msg)
        out["seq"] = self.next_rdbg_seq()
        self._from_client[out["seq"]] = msg["seq"]
        return out

    def rdbg_response_to_client(self, msg: dict) -> dict | None:
        orig = self._from_client.pop(msg.get("request_seq", -1), None)
        if orig is None:
            return None
        out = dict(msg)
        out["seq"] = self.next_client_seq()
        out["request_seq"] = orig
        return out

    def rdbg_event_to_client(self, msg: dict) -> dict:
        out = dict(msg)
        out["seq"] = self.next_client_seq()
        return out

    def rdbg_request_to_client(self, msg: dict) -> dict:
        out = dict(msg)
        out["seq"] = self.next_client_seq()
        self._from_rdbg[out["seq"]] = msg["seq"]
        return out

    def client_response_to_rdbg(self, msg: dict) -> dict | None:
        orig = self._from_rdbg.pop(msg.get("request_seq", -1), None)
        if orig is None:
            return None
        out = dict(msg)
        out["seq"] = self.next_rdbg_seq()
        out["request_seq"] = orig
        return out


@dataclass
class _Transport:
    rdbg_args: list[str]
    connect: Callable[[], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]
    cleanup: Callable[[], None] = lambda: None


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pick_transport() -> _Transport:
    """UNIX socket where possible; TCP on 127.0.0.1 otherwise.

    AF_UNIX paths are limited to ~107 bytes — a long TMPDIR silently
    breaks connect(), so fall back to TCP past a 90-char margin. No
    --cookie: rdbg's cookie check lives in its own protocol greeting,
    not DAP; binding to 127.0.0.1 is the actual boundary.
    """
    if os.name != "nt":
        sock_dir = tempfile.mkdtemp(prefix="tdb-rdbg-")
        sock_path = os.path.join(sock_dir, "s")
        if len(sock_path) < 90:

            def cleanup() -> None:
                shutil.rmtree(sock_dir, ignore_errors=True)

            return _Transport(
                ["--sock-path", sock_path],
                lambda: asyncio.open_unix_connection(sock_path),
                cleanup,
            )
        shutil.rmtree(sock_dir, ignore_errors=True)
    port = _free_port()
    return _Transport(
        ["--port", str(port), "--host", "127.0.0.1"],
        lambda: asyncio.open_connection("127.0.0.1", port),
    )


async def _rdbg_version(rdbg: str) -> tuple[int, int]:
    """Parse `rdbg --version` ("rdbg 1.11.1") into (major, minor)."""
    proc = await asyncio.create_subprocess_exec(
        rdbg,
        "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    m = re.search(rb"(\d+)\.(\d+)\.\d+", out)
    if not m:
        raise RuntimeError(f"could not parse `rdbg --version` output: {out!r}")
    return int(m.group(1)), int(m.group(2))
```

(The unused imports — `parse_message`, `ReverseRequester`, `signal`,
`subprocess`, `Any` — are consumed by Task 5's `RubyDapServer` in this
same file; if the linter complains at this intermediate step, that is
expected and resolves in Task 5.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_ruby_proxy_units.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/tdb/adapters/ruby/
git add tests/unit/test_ruby_proxy_units.py
git commit -m "feat: ruby proxy scaffolding — seq translation, transport pick, capabilities"
```

---

### Task 5: Proxy server core — launch, passthrough, output pump

**Files:**
- Modify: `src/tdb/adapters/ruby/server.py` (append `RubyDapServer`)
- Create: `tests/integration/ruby_adapter_harness.py`
- Create: `tests/integration/fixtures/ruby_hello.rb`
- Create: `tests/integration/fixtures/ruby_vars.rb`
- Test: `tests/integration/test_ruby_adapter_launch.py`

**Interfaces:**
- Consumes: everything Task 4 defined in `server.py`; `AdapterClient`
  from `tests/integration/perl_adapter_harness.py`.
- Produces: `RubyDapServer(reader, writer)` with `async run()` (used by
  `__main__.py`); harness helpers `rdbg_ok() -> bool`,
  `async start_ruby_adapter() -> AdapterClient`,
  `async launch_stopped(client, program, breakpoints=None,
  stop_on_entry=True)`, `FIXTURES` (Path). Tasks 6–10 reuse the harness.

- [ ] **Step 1: Write fixtures and harness**

`tests/integration/fixtures/ruby_hello.rb`:

```ruby
x = 1
y = 2
puts "hello from ruby #{x + y}"
exit 7
```

`tests/integration/fixtures/ruby_vars.rb`:

```ruby
def inner(n)
  m = n * 2
  m + 1
end

def outer(k)
  inner(k) + inner(k + 1)
end

total = 0
[1, 2, 3].each do |i|
  total += outer(i)
end
puts total
```

`tests/integration/ruby_adapter_harness.py`:

```python
"""Scripted DAP client for the ruby proxy + shared launch helper."""

import shutil
import subprocess
from pathlib import Path

from tests.integration.perl_adapter_harness import AdapterClient

FIXTURES = Path(__file__).parent / "fixtures"


def rdbg_ok() -> bool:
    """rdbg present and debug gem >= 1.9."""
    rdbg = shutil.which("rdbg")
    if not rdbg:
        return False
    try:
        cp = subprocess.run(
            [rdbg, "--version"], capture_output=True, text=True, check=True
        )
        # "rdbg 1.11.1"
        parts = cp.stdout.split()[-1].split(".")
        major, minor = int(parts[0]), int(parts[1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return False
    return (major, minor) >= (1, 9)


async def start_ruby_adapter() -> AdapterClient:
    client = AdapterClient()
    await client.start(module="tdb.adapters.ruby")
    await client.request(
        "initialize",
        {"adapterID": "rdbg", "linesStartAt1": True, "columnsStartAt1": True},
    )
    return client


async def launch_stopped(
    client: AdapterClient, program: str, breakpoints=None, stop_on_entry=True
):
    """Standard DAP dance: launch -> initialized -> [setBreakpoints] ->
    configurationDone; returns after both responses arrive."""
    launch_fut = client.send(
        "launch",
        {
            "type": "ruby",
            "request": "launch",
            "program": program,
            "args": [],
            "cwd": str(Path(program).parent),
            "stopOnEntry": stop_on_entry,
        },
    )
    await client.wait_event("initialized")
    if breakpoints:
        await client.request(
            "setBreakpoints",
            {"source": {"path": program}, "breakpoints": breakpoints},
        )
    await client.request("configurationDone")
    await launch_fut
```

- [ ] **Step 2: Write the failing launch tests**

`tests/integration/test_ruby_adapter_launch.py`:

```python
"""DAP-level: launch, entry stop, nonstop, output pump, exit code."""

import pytest

from tests.integration.ruby_adapter_harness import (
    FIXTURES,
    launch_stopped,
    rdbg_ok,
    start_ruby_adapter,
)

pytestmark = pytest.mark.skipif(not rdbg_ok(), reason="needs rdbg (debug gem >= 1.9)")


async def test_stop_on_entry_reports_entry_at_first_line():
    client = await start_ruby_adapter()
    try:
        program = str(FIXTURES / "ruby_hello.rb")
        await launch_stopped(client, program)
        ev = await client.wait_event("stopped")
        # rdbg reports the entry stop as "pause"; the proxy rewrites the
        # first stop to "entry" for debugpy parity.
        assert ev["body"]["reason"] == "entry"
        st = await client.request(
            "stackTrace", {"threadId": ev["body"].get("threadId", 1)}
        )
        top = st["body"]["stackFrames"][0]
        assert top["source"]["path"] == program
        await client.request("continue", {"threadId": 1})
        exited = await client.wait_event("exited")
        assert exited["body"]["exitCode"] == 7
        await client.wait_event("terminated")
    finally:
        await client.stop()


async def test_nonstop_runs_to_completion_with_output():
    client = await start_ruby_adapter()
    try:
        await launch_stopped(
            client, str(FIXTURES / "ruby_hello.rb"), stop_on_entry=False
        )
        exited = await client.wait_event("exited")
        assert exited["body"]["exitCode"] == 7
        await client.wait_event("terminated")
        text = "".join(
            e["body"].get("output", "")
            for e in list(client.events)
            if e["event"] == "output"
        )
        assert "hello from ruby 3" in text
        assert "DEBUGGER" not in text  # banner lines filtered
        assert "Ruby REPL" not in text  # greeting notice filtered
    finally:
        await client.stop()


async def test_launch_missing_program_fails():
    client = await start_ruby_adapter()
    try:
        resp = await client.send(
            "launch",
            {
                "type": "ruby",
                "program": "/nonexistent/x.rb",
                "args": [],
                "cwd": "/tmp",
                "stopOnEntry": True,
            },
        )
        assert resp["success"] is False
        assert "not found" in resp["message"]
    finally:
        await client.stop()


async def test_launch_bad_rdbg_path_names_the_hint():
    client = await start_ruby_adapter()
    try:
        resp = await client.send(
            "launch",
            {
                "type": "ruby",
                "program": str(FIXTURES / "ruby_hello.rb"),
                "args": [],
                "cwd": "/tmp",
                "stopOnEntry": True,
                "rdbg": "/nonexistent/rdbg",
            },
        )
        assert resp["success"] is False
    finally:
        await client.stop()
```

Note `client.send(...)` returns a future; `await` it directly for the
failure cases (the proxy answers immediately, no configurationDone
needed).

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_ruby_adapter_launch.py -v`
Expected: FAIL — `ImportError`/`AttributeError` (no `RubyDapServer`), or
"unsupported command" errors.

- [ ] **Step 4: Implement `RubyDapServer`** (append to `server.py`)

```python
class RubyDapServer:
    """Store-and-forward proxy; see module docstring."""

    def __init__(self, reader: asyncio.StreamReader, writer: Any) -> None:
        self._reader = reader
        self._writer = writer
        self._seqs = SeqTranslator()
        self._done = asyncio.Event()
        self._proc: asyncio.subprocess.Process | None = None
        self._rdbg_writer: asyncio.StreamWriter | None = None
        self._transport: _Transport | None = None
        self._client_init_args: dict = {}
        self._client_supports_run_in_terminal = False
        self._stop_on_entry = True
        self._entry_stop_pending = False
        self._start_client_seq: int | None = None
        self._launched = False
        self._sent_exited = False
        self._sent_terminated = False
        self._reverse = ReverseRequester(self._write_client, self._seqs.next_client_seq)
        # Strong refs: asyncio only weakly references bare tasks (repo
        # pitfall) — a GC'd pump silently loses program output.
        self._tasks: set[asyncio.Future] = set()
        self._pump_tasks: list[asyncio.Future] = []
        self._launch_task: asyncio.Future | None = None
        self.handlers: dict[str, Callable[[dict], Awaitable[None]]] = {}
        for name in dir(self):
            if name.startswith("_on_"):
                self.handlers[name[4:]] = getattr(self, name)

    # ---- plumbing ----
    def _write_client(self, msg: dict) -> None:
        self._writer.write(encode_message(msg))

    def _write_rdbg(self, msg: dict) -> None:
        if self._rdbg_writer is not None:
            self._rdbg_writer.write(encode_message(msg))

    def _spawn_task(self, coro) -> asyncio.Future:
        t = asyncio.ensure_future(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    def send_response(self, request: dict, body: dict | None = None) -> None:
        msg: dict = {
            "seq": self._seqs.next_client_seq(),
            "type": "response",
            "request_seq": request["seq"],
            "command": request["command"],
            "success": True,
        }
        if body:
            msg["body"] = body
        self._write_client(msg)

    def send_error(self, request: dict, message: str) -> None:
        self._write_client(
            {
                "seq": self._seqs.next_client_seq(),
                "type": "response",
                "request_seq": request["seq"],
                "command": request["command"],
                "success": False,
                "message": message,
            }
        )

    def send_event(self, event: str, body: dict | None = None) -> None:
        msg: dict = {
            "seq": self._seqs.next_client_seq(),
            "type": "event",
            "event": event,
        }
        if body:
            msg["body"] = body
        self._write_client(msg)

    # ---- main loop (client stdio side) ----
    async def run(self) -> None:
        try:
            while not self._done.is_set():
                try:
                    msg = await read_message(self._reader)
                except (ConnectionError, asyncio.IncompleteReadError, EOFError):
                    break
                await self._dispatch_client_message(msg)
                await self._writer.drain()
        finally:
            await self._cancel_launch_task()
            await self._ensure_rdbg_dead()
            if self._transport is not None:
                self._transport.cleanup()
            await self._writer.drain()

    async def _dispatch_client_message(self, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == "response":
            if self._reverse.route(parse_message(msg)):
                return
            fwd = self._seqs.client_response_to_rdbg(msg)
            if fwd is not None:
                self._write_rdbg(fwd)
            return
        if mtype != "request":
            return
        handler = self.handlers.get(msg["command"])
        if handler is not None:
            try:
                await handler(msg)
            except Exception as e:
                log.exception("handler %s failed", msg["command"])
                self.send_error(msg, str(e))
            return
        if self._rdbg_writer is None:
            self.send_error(msg, "no debug session")
            return
        self._write_rdbg(self._seqs.client_request_to_rdbg(msg))

    # ---- rdbg socket side ----
    async def _pump_rdbg(self, reader: asyncio.StreamReader) -> None:
        while True:
            try:
                msg = await read_message(reader)
            except (ConnectionError, asyncio.IncompleteReadError, EOFError):
                return
            mtype = msg.get("type")
            if mtype == "event":
                if self._note_and_filter_event(msg):
                    continue
                self._write_client(self._seqs.rdbg_event_to_client(msg))
            elif mtype == "response":
                out = self._seqs.rdbg_response_to_client(msg)
                if out is None:
                    continue  # reply to a proxy-originated request
                if out["request_seq"] == self._start_client_seq:
                    # the client sent `launch`; rdbg answered the
                    # translated `attach` — restamp the command so the
                    # client's launch future matches.
                    out["command"] = "launch"
                self._write_client(out)
            elif mtype == "request":
                self._write_client(self._seqs.rdbg_request_to_client(msg))
            await self._writer.drain()

    def _note_and_filter_event(self, msg: dict) -> bool:
        """Track exit/stop state; True -> swallow the event."""
        event = msg.get("event")
        body = msg.get("body") or {}
        if event == "output":
            if body.get("category") == "console" and str(
                body.get("output", "")
            ).startswith(_REPL_NOTICE):
                return True
        elif event == "stopped":
            if self._entry_stop_pending:
                # rdbg reports the post-configurationDone entry stop as
                # "pause"; tdb (like debugpy) expects "entry".
                self._entry_stop_pending = False
                body["reason"] = "entry"
                msg["body"] = body
        elif event == "exited":
            self._sent_exited = True
        elif event == "terminated":
            self._sent_terminated = True
        return False

    async def _pump_output(self, stream: asyncio.StreamReader, category: str) -> None:
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace")
            if category == "stderr" and text.startswith(_BANNER_PREFIX):
                continue
            self.send_event("output", {"category": category, "output": text})
            await self._writer.drain()

    async def _watch_exit(self) -> None:
        assert self._proc is not None
        code = await self._proc.wait()
        # let the pipe pumps drain the output tail before exit events
        if self._pump_tasks:
            await asyncio.wait(self._pump_tasks, timeout=2.0)
        if not self._launched:
            return  # pre-handshake death is reported via the launch error
        if not self._sent_exited:
            self.send_event("exited", {"exitCode": code})
        if not self._sent_terminated:
            self.send_event("terminated")
        await self._writer.drain()

    # ---- lifecycle handlers ----
    async def _on_initialize(self, request: dict) -> None:
        self._client_init_args = dict(request.get("arguments") or {})
        self._client_supports_run_in_terminal = bool(
            self._client_init_args.get("supportsRunInTerminalRequest")
        )
        self.send_response(request, CAPABILITIES)

    async def _on_launch(self, request: dict) -> None:
        args = request.get("arguments") or {}
        program = args.get("program", "")
        if not os.path.isfile(program):
            self.send_error(request, f"program not found: {program}")
            return
        rdbg = args.get("rdbg") or shutil.which("rdbg")
        if rdbg is None:
            self.send_error(request, RDBG_HINT)
            return
        try:
            version = await _rdbg_version(rdbg)
        except (OSError, RuntimeError) as e:
            self.send_error(request, f"cannot run {rdbg!r}: {e} — {RDBG_HINT}")
            return
        if version < MIN_DEBUG_GEM:
            self.send_error(
                request,
                f"debug gem {version[0]}.{version[1]} is too old — tdb "
                f"needs >= {MIN_DEBUG_GEM[0]}.{MIN_DEBUG_GEM[1]} "
                f"(`gem install debug`)",
            )
            return
        self._stop_on_entry = bool(args.get("stopOnEntry", True))
        self._transport = pick_transport()
        cmd = [
            rdbg,
            "--open",
            *self._transport.rdbg_args,
            "--",
            program,
            *[str(a) for a in (args.get("args") or [])],
        ]
        if args.get("console") == "externalTerminal":
            if not self._client_supports_run_in_terminal:
                self.send_error(
                    request,
                    "externalTerminal launch requires a client that "
                    "supports the runInTerminal reverse request",
                )
                return
            # session-launch awaits the runInTerminal reply, which only
            # run()'s read loop can route — but run() is what's calling
            # this handler and it awaits handlers inline. Awaiting here
            # would deadlock; run the rest as a background task (strong
            # ref, per the repo's task-GC pitfall) so run() goes back to
            # reading. Same shape as the bash server's _on_launch.
            self._launch_task = asyncio.ensure_future(
                self._finish_launch(request, cmd, args, terminal=True)
            )
            return
        await self._finish_launch(request, cmd, args, terminal=False)

    async def _finish_launch(
        self, request: dict, cmd: list[str], args: dict, *, terminal: bool
    ) -> None:
        cwd = args.get("cwd") or os.getcwd()
        env = {**os.environ, **(args.get("env") or {})}
        try:
            if terminal:
                await self._reverse.request(
                    "runInTerminal",
                    {
                        "kind": "external",
                        "title": "tdb ruby debuggee",
                        "cwd": cwd,
                        "args": cmd,
                        "env": args.get("env") or {},
                    },
                )
            else:
                popen_kwargs: dict[str, Any] = {}
                if os.name == "nt":
                    # Ctrl-C isolation, same as the perl/bash spawn path
                    popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    popen_kwargs["start_new_session"] = True
                self._proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=cwd,
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **popen_kwargs,
                )
            reader, writer = await self._connect_with_retry()
        except asyncio.CancelledError:
            raise  # disconnect/terminate cancelling us — they clean up
        except Exception as e:
            detail = await self._collect_early_stderr()
            await self._ensure_rdbg_dead()
            if not isinstance(
                e, (OSError, TimeoutError, RuntimeError, ReverseRequestError)
            ):
                log.exception("ruby launch failed unexpectedly")
            self.send_error(request, f"{e}\n{detail}".strip())
            await self._writer.drain()
            return
        self._rdbg_writer = writer
        self._launched = True
        self._entry_stop_pending = self._stop_on_entry
        self._start_client_seq = request["seq"]
        if self._proc is not None:
            self._pump_tasks = [
                self._spawn_task(self._pump_output(self._proc.stdout, "stdout")),
                self._spawn_task(self._pump_output(self._proc.stderr, "stderr")),
            ]
            self._spawn_task(self._watch_exit())
        self._spawn_task(self._pump_rdbg(reader))
        # rdbg needs its own initialize first. Proxy-originated (no
        # client mapping) -> its response is swallowed by the translator;
        # rdbg's `initialized` event passes through to the client and
        # triggers its setBreakpoints/configurationDone sequence.
        self._write_rdbg(
            {
                "seq": self._seqs.next_rdbg_seq(),
                "type": "request",
                "command": "initialize",
                "arguments": dict(self._client_init_args),
            }
        )
        # Forward the client's launch AS an rdbg `attach` (see module
        # docstring): nonstop honors stopOnEntry, localfs=true because
        # rdbg runs on this same machine.
        fwd = self._seqs.client_request_to_rdbg(request)
        fwd["command"] = "attach"
        fwd["arguments"] = {
            "localfs": True,
            "nonstop": not self._stop_on_entry,
        }
        self._write_rdbg(fwd)
        await self._writer.drain()

    async def _connect_with_retry(
        self, timeout: float = 30.0
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        assert self._transport is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            try:
                return await self._transport.connect()
            except (ConnectionError, FileNotFoundError, OSError):
                if self._proc is not None and self._proc.returncode is not None:
                    raise RuntimeError(
                        f"rdbg exited with code {self._proc.returncode} "
                        f"before accepting a connection"
                    )
                if loop.time() > deadline:
                    raise TimeoutError("timed out waiting for rdbg's DAP socket")
                await asyncio.sleep(0.1)

    async def _collect_early_stderr(self) -> str:
        """Salvage rdbg's stderr for a failed-launch message (pumps have
        not started yet on this path)."""
        if self._proc is None or self._proc.stderr is None:
            return ""
        try:
            data = await asyncio.wait_for(self._proc.stderr.read(4096), 0.5)
        except (asyncio.TimeoutError, OSError):
            return ""
        lines = [
            ln
            for ln in data.decode("utf-8", "replace").splitlines()
            if not ln.startswith(_BANNER_PREFIX)
        ]
        return "\n".join(lines)

    # ---- teardown ----
    async def _cancel_launch_task(self) -> None:
        """Cancel an in-flight externalTerminal launch continuation before
        teardown (same rationale as the bash server's method of the same
        name: the continuation could assign session state after our
        checks already ran)."""
        task, self._launch_task = self._launch_task, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("launch task raised during cancellation")

    def _kill_rdbg_group(self, sig_kill: bool = False) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        try:
            if os.name == "nt":
                proc.kill() if sig_kill else proc.terminate()
            else:
                os.killpg(proc.pid, signal.SIGKILL if sig_kill else signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass

    async def _ensure_rdbg_dead(self, grace: float = 2.0) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        self._kill_rdbg_group()
        try:
            await asyncio.wait_for(proc.wait(), grace)
        except asyncio.TimeoutError:
            self._kill_rdbg_group(sig_kill=True)
            await proc.wait()

    async def _on_disconnect(self, request: dict) -> None:
        await self._cancel_launch_task()
        if self._rdbg_writer is not None:
            # graceful first (kills a terminal-mode debuggee the proxy
            # has no process handle for); proxy-originated -> swallowed
            self._write_rdbg(
                {
                    "seq": self._seqs.next_rdbg_seq(),
                    "type": "request",
                    "command": "terminate",
                    "arguments": {},
                }
            )
        await self._ensure_rdbg_dead()
        self.send_response(request)
        self._done.set()

    async def _on_terminate(self, request: dict) -> None:
        await self._cancel_launch_task()
        if self._rdbg_writer is not None:
            self._write_rdbg(
                {
                    "seq": self._seqs.next_rdbg_seq(),
                    "type": "request",
                    "command": "terminate",
                    "arguments": {},
                }
            )
        await self._ensure_rdbg_dead()
        self.send_response(request)
```

- [ ] **Step 5: Run the launch tests**

Run: `uv run pytest tests/integration/test_ruby_adapter_launch.py -v`
Expected: all PASS. Debugging notes if not:
- Entry stop missing → confirm the launch was forwarded as `attach` with
  `nonstop: false` and rdbg received `configurationDone`.
- No output events → pumps not started or GC'd (check strong refs).
- `exited` exitCode wrong → `_watch_exit` raced the socket's `terminated`;
  the `_sent_*` flags must be set in `_note_and_filter_event` BEFORE the
  event is forwarded.

- [ ] **Step 6: Run the unit suite to catch regressions**

Run: `uv run pytest tests/unit -q`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/tdb/adapters/ruby/server.py tests/integration/ruby_adapter_harness.py tests/integration/fixtures/ruby_hello.rb tests/integration/fixtures/ruby_vars.rb tests/integration/test_ruby_adapter_launch.py
git commit -m "feat: ruby proxy server — launch-as-attach, passthrough, output pump"
```

---

### Task 6: Integration — breakpoints, stepping, inspection, evaluate

**Files:**
- Test: `tests/integration/test_ruby_adapter_breakpoints.py`
- Test: `tests/integration/test_ruby_adapter_inspection.py`

**Interfaces:**
- Consumes: harness from Task 5 (`start_ruby_adapter`, `launch_stopped`,
  `rdbg_ok`, `FIXTURES`); fixture `ruby_vars.rb` (breakpointable line 3 =
  `m + 1` inside `inner`, line 12 = `total += outer(i)`).
- Produces: nothing new — these tests prove the passthrough carries the
  whole core-DAP surface.

These are pure passthrough exercises: if any fails, the bug is almost
certainly in seq translation or event filtering, not in rdbg.

- [ ] **Step 1: Write the tests**

`tests/integration/test_ruby_adapter_breakpoints.py`:

```python
"""Breakpoints (plain + conditional) and stepping through the proxy."""

import pytest

from tests.integration.ruby_adapter_harness import (
    FIXTURES,
    launch_stopped,
    rdbg_ok,
    start_ruby_adapter,
)

pytestmark = pytest.mark.skipif(not rdbg_ok(), reason="needs rdbg (debug gem >= 1.9)")

VARS = str(FIXTURES / "ruby_vars.rb")


async def test_breakpoint_hit_and_continue_to_exit():
    client = await start_ruby_adapter()
    try:
        await launch_stopped(
            client, VARS, breakpoints=[{"line": 12}], stop_on_entry=False
        )
        ev = await client.wait_event("stopped")
        assert ev["body"]["reason"] == "breakpoint"
        st = await client.request("stackTrace", {"threadId": 1})
        assert st["body"]["stackFrames"][0]["line"] == 12
        await client.request("continue", {"threadId": 1})
        await client.wait_event("stopped")  # second loop iteration
        await client.request(
            "setBreakpoints", {"source": {"path": VARS}, "breakpoints": []}
        )
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
    finally:
        await client.stop()


async def test_conditional_breakpoint():
    client = await start_ruby_adapter()
    try:
        await launch_stopped(
            client,
            VARS,
            breakpoints=[{"line": 12, "condition": "i == 3"}],
            stop_on_entry=False,
        )
        await client.wait_event("stopped")
        resp = await client.request("evaluate", {"expression": "i", "context": "repl"})
        assert resp["body"]["result"].strip() == "3"
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
    finally:
        await client.stop()


async def test_step_in_and_out():
    client = await start_ruby_adapter()
    try:
        await launch_stopped(
            client, VARS, breakpoints=[{"line": 12}], stop_on_entry=False
        )
        await client.wait_event("stopped")
        await client.request("stepIn", {"threadId": 1})
        ev = await client.wait_event("stopped")
        assert ev["body"]["reason"] in ("step", "pause")
        st = await client.request("stackTrace", {"threadId": 1})
        names = [f["name"] for f in st["body"]["stackFrames"]]
        assert any("outer" in n for n in names)
        await client.request("stepOut", {"threadId": 1})
        await client.wait_event("stopped")
        await client.request("continue", {"threadId": 1})
        # remaining breakpoint hits: clear and run out
        await client.wait_event("stopped")
        await client.request(
            "setBreakpoints", {"source": {"path": VARS}, "breakpoints": []}
        )
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
    finally:
        await client.stop()
```

`tests/integration/test_ruby_adapter_inspection.py`:

```python
"""Stack/scopes/variables/evaluate/completions through the proxy."""

import pytest

from tests.integration.ruby_adapter_harness import (
    FIXTURES,
    launch_stopped,
    rdbg_ok,
    start_ruby_adapter,
)

pytestmark = pytest.mark.skipif(not rdbg_ok(), reason="needs rdbg (debug gem >= 1.9)")

VARS = str(FIXTURES / "ruby_vars.rb")


async def _stop_at(client, line):
    await launch_stopped(
        client, VARS, breakpoints=[{"line": line}], stop_on_entry=False
    )
    return await client.wait_event("stopped")


async def test_scopes_and_variables():
    client = await start_ruby_adapter()
    try:
        await _stop_at(client, 3)  # inside inner(); m is defined
        st = await client.request("stackTrace", {"threadId": 1})
        frame_id = st["body"]["stackFrames"][0]["id"]
        scopes = await client.request("scopes", {"frameId": frame_id})
        assert scopes["success"]
        ref = scopes["body"]["scopes"][0]["variablesReference"]
        vs = await client.request("variables", {"variablesReference": ref})
        names = {v["name"] for v in vs["body"]["variables"]}
        assert "m" in names or "%self" in names  # rdbg lists %self too
        await client.request(
            "setBreakpoints", {"source": {"path": VARS}, "breakpoints": []}
        )
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
    finally:
        await client.stop()


async def test_evaluate_in_frame():
    client = await start_ruby_adapter()
    try:
        await _stop_at(client, 3)
        st = await client.request("stackTrace", {"threadId": 1})
        frame_id = st["body"]["stackFrames"][0]["id"]
        resp = await client.request(
            "evaluate",
            {"expression": "m + 40", "frameId": frame_id, "context": "repl"},
        )
        assert resp["success"]
        assert resp["body"]["result"].strip() == "42"  # m == 2 at first hit
        await client.request(
            "setBreakpoints", {"source": {"path": VARS}, "breakpoints": []}
        )
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
    finally:
        await client.stop()


async def test_completions():
    client = await start_ruby_adapter()
    try:
        await _stop_at(client, 3)
        st = await client.request("stackTrace", {"threadId": 1})
        frame_id = st["body"]["stackFrames"][0]["id"]
        resp = await client.request(
            "completions",
            {"text": "tot", "column": 4, "frameId": frame_id},
        )
        assert resp["success"]
        await client.request(
            "setBreakpoints", {"source": {"path": VARS}, "breakpoints": []}
        )
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
    finally:
        await client.stop()
```

- [ ] **Step 2: Run the tests**

Run: `uv run pytest tests/integration/test_ruby_adapter_breakpoints.py tests/integration/test_ruby_adapter_inspection.py -v`
Expected: PASS (this is verification of Task 5's passthrough — fix the
proxy, not the tests, on failure; loosen only assertions that prove to
depend on rdbg version formatting, e.g. `m + 40` result quoting).

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_ruby_adapter_breakpoints.py tests/integration/test_ruby_adapter_inspection.py
git commit -m "test: ruby proxy passthrough — breakpoints, stepping, inspection, completions"
```

---

### Task 7: Teardown — disconnect kills rdbg, no orphans

**Files:**
- Test: `tests/integration/test_ruby_adapter_teardown.py`
- Modify (only if a test exposes a gap): `src/tdb/adapters/ruby/server.py`

**Interfaces:**
- Consumes: Task 5 harness; `ruby_sleep.rb` fixture created here.
- Produces: `tests/integration/fixtures/ruby_sleep.rb` (used again by
  Task 10's run-mode test).

- [ ] **Step 1: Create the long-running fixture**

`tests/integration/fixtures/ruby_sleep.rb`:

```ruby
i = 0
loop do
  i += 1
  sleep 0.05
end
```

- [ ] **Step 2: Write the tests**

`tests/integration/test_ruby_adapter_teardown.py`:

```python
"""Disconnect/terminate must kill the rdbg process tree — no orphans."""

import asyncio
import os
import signal

import pytest

from tests.integration.ruby_adapter_harness import (
    FIXTURES,
    launch_stopped,
    rdbg_ok,
    start_ruby_adapter,
)

pytestmark = pytest.mark.skipif(not rdbg_ok(), reason="needs rdbg (debug gem >= 1.9)")

SLEEPER = str(FIXTURES / "ruby_sleep.rb")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


async def _rdbg_pids_of(proxy_pid: int) -> list[int]:
    """Direct children of the proxy (the rdbg process).

    /proc first (works on BusyBox/Alpine CI, where `ps` lacks --ppid),
    procps `ps --ppid` as the fallback for /proc-less platforms.
    """
    try:
        text = "".join(
            open(f"/proc/{proxy_pid}/task/{t}/children").read()
            for t in os.listdir(f"/proc/{proxy_pid}/task")
        )
        pids = [int(p) for p in text.split()]
        if pids:
            return pids
    except OSError:
        pass
    proc = await asyncio.create_subprocess_exec(
        "ps",
        "-o",
        "pid=",
        "--ppid",
        str(proxy_pid),
        stdout=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    return [int(p) for p in out.split()]


@pytest.mark.skipif(os.name == "nt", reason="ps/kill-based checks")
async def test_disconnect_kills_rdbg():
    client = await start_ruby_adapter()
    try:
        await launch_stopped(client, SLEEPER, stop_on_entry=False)
        await asyncio.sleep(0.3)
        children = await _rdbg_pids_of(client.proc.pid)
        assert children, "expected a live rdbg child"
        resp = await client.request("disconnect", {})
        assert resp["success"]
        await asyncio.sleep(0.5)
        for pid in children:
            assert not _alive(pid), f"rdbg {pid} survived disconnect"
    finally:
        await client.stop()


@pytest.mark.skipif(os.name == "nt", reason="ps/kill-based checks")
async def test_terminate_kills_rdbg_and_reports_termination():
    client = await start_ruby_adapter()
    try:
        await launch_stopped(client, SLEEPER, stop_on_entry=False)
        await asyncio.sleep(0.3)
        children = await _rdbg_pids_of(client.proc.pid)
        resp = await client.request("terminate", {})
        assert resp["success"]
        await client.wait_event("terminated")
        await asyncio.sleep(0.5)
        for pid in children:
            assert not _alive(pid), f"rdbg {pid} survived terminate"
    finally:
        await client.stop()


@pytest.mark.skipif(os.name == "nt", reason="ps/kill-based checks")
async def test_proxy_death_kills_rdbg():
    """The run() finally-block must reap rdbg when tdb kills the proxy."""
    client = await start_ruby_adapter()
    await launch_stopped(client, SLEEPER, stop_on_entry=False)
    await asyncio.sleep(0.3)
    children = await _rdbg_pids_of(client.proc.pid)
    assert children
    client.proc.stdin.close()  # EOF -> run() exits -> finally kills group
    await asyncio.sleep(1.5)
    for pid in children:
        assert not _alive(pid), f"rdbg {pid} survived proxy stdin EOF"
    await client.stop()
```

Note: reading `/proc/<pid>/task/*/children` requires
`CONFIG_PROC_CHILDREN` (present on all mainstream kernels); the
`ps --ppid` fallback covers the rest. If a same-purpose Alpine-safe
process-snapshot helper already exists in `tests/` (from the Alpine CI
fixes), reuse it instead of duplicating.

- [ ] **Step 3: Run the tests**

Run: `uv run pytest tests/integration/test_ruby_adapter_teardown.py -v`
Expected: PASS with Task 5's implementation. If `test_proxy_death_kills_rdbg`
fails: the `run()` finally-block runs `_ensure_rdbg_dead()` — verify EOF on
stdin actually breaks the read loop (it raises `IncompleteReadError`).

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_ruby_adapter_teardown.py tests/integration/fixtures/ruby_sleep.rb
git commit -m "test: ruby proxy teardown — disconnect/terminate/EOF all reap rdbg"
```

---

### Task 8: `--terminal` mode (runInTerminal)

**Files:**
- Test: `tests/integration/test_ruby_terminal.py`
- Modify (only if a test exposes a gap): `src/tdb/adapters/ruby/server.py`

**Interfaces:**
- Consumes: Task 5 harness; `AdapterClient.on_reverse_request` hook (the
  harness answers reverse requests via that callback).
- Produces: nothing new.

The proxy side is already implemented (Task 5's `terminal=True` path).
This task proves the handshake: the test's reverse-request handler plays
the terminal's role by spawning the rdbg argv itself.

- [ ] **Step 1: Write the test**

`tests/integration/test_ruby_terminal.py`:

```python
"""externalTerminal launch: proxy sends runInTerminal with the full rdbg
argv; the 'terminal' (this test) spawns it; session proceeds normally."""

import asyncio

import pytest

from tests.integration.ruby_adapter_harness import (
    FIXTURES,
    rdbg_ok,
)
from tests.integration.perl_adapter_harness import AdapterClient

pytestmark = pytest.mark.skipif(not rdbg_ok(), reason="needs rdbg (debug gem >= 1.9)")


async def test_external_terminal_handshake():
    client = AdapterClient()
    spawned: list[asyncio.subprocess.Process] = []

    async def fake_terminal(req):
        args = req["arguments"]
        assert args["kind"] == "external"
        assert "rdbg" in args["args"][0]
        assert "--open" in args["args"]
        proc = await asyncio.create_subprocess_exec(*args["args"], cwd=args["cwd"])
        spawned.append(proc)
        return {}

    client.on_reverse_request = fake_terminal
    await client.start(module="tdb.adapters.ruby")
    try:
        await client.request(
            "initialize",
            {"adapterID": "rdbg", "supportsRunInTerminalRequest": True},
        )
        program = str(FIXTURES / "ruby_hello.rb")
        launch_fut = client.send(
            "launch",
            {
                "type": "ruby",
                "request": "launch",
                "program": program,
                "args": [],
                "cwd": str(FIXTURES),
                "stopOnEntry": True,
                "console": "externalTerminal",
            },
        )
        await client.wait_event("initialized")
        await client.request("configurationDone")
        await launch_fut
        ev = await client.wait_event("stopped")
        assert ev["body"]["reason"] == "entry"
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
    finally:
        for proc in spawned:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        await client.stop()


async def test_external_terminal_requires_client_support():
    client = AdapterClient()
    await client.start(module="tdb.adapters.ruby")
    try:
        await client.request(
            "initialize",
            {"adapterID": "rdbg", "supportsRunInTerminalRequest": False},
        )
        resp = await client.send(
            "launch",
            {
                "type": "ruby",
                "program": str(FIXTURES / "ruby_hello.rb"),
                "args": [],
                "cwd": str(FIXTURES),
                "stopOnEntry": True,
                "console": "externalTerminal",
            },
        )
        assert resp["success"] is False
        assert "runInTerminal" in resp["message"]
    finally:
        await client.stop()
```

- [ ] **Step 2: Run the tests**

Run: `uv run pytest tests/integration/test_ruby_terminal.py -v`
Expected: PASS. Known trap if the first test hangs: the proxy must NOT
await the runInTerminal reply inside `_on_launch` inline (deadlock — see
the background-task comment in `_on_launch`).

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_ruby_terminal.py
git commit -m "test: ruby --terminal runInTerminal handshake"
```

---

### Task 9: Remote attach (direct TCP)

**Files:**
- Test: `tests/integration/test_ruby_remote_attach.py`

**Interfaces:**
- Consumes: `RdbgAdapter.attach_body` (Task 2), `rdbg_ok`/`FIXTURES`
  (Task 5), `ruby_sleep.rb` (Task 7).
- Produces: nothing new — proves the direct-TCP assumption end to end.

- [ ] **Step 1: Write the test**

`tests/integration/test_ruby_remote_attach.py`:

```python
"""Remote attach is DIRECT: tdb TCP-connects to a user-started
`rdbg --open --port N`; no proxy involved. This test plays tdb's part
with a raw DAP-over-TCP client using the exact attach body
RdbgAdapter.attach_body produces."""

import asyncio
import json

import pytest

from tdb.languages.ruby import RdbgAdapter
from tests.integration.ruby_adapter_harness import FIXTURES, rdbg_ok
from tdb.adapters.ruby.server import _free_port

pytestmark = pytest.mark.skipif(not rdbg_ok(), reason="needs rdbg (debug gem >= 1.9)")


class TcpDap:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.seq = 0
        self.events = []

    def send(self, command, arguments=None):
        self.seq += 1
        body = json.dumps(
            {
                "seq": self.seq,
                "type": "request",
                "command": command,
                "arguments": arguments or {},
            }
        ).encode()
        self.writer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)

    async def recv(self):
        header = b""
        while not header.endswith(b"\r\n\r\n"):
            header += await self.reader.readexactly(1)
        length = int(header.split(b":")[1])
        return json.loads(await self.reader.readexactly(length))

    async def wait(self, *, event=None, command=None, timeout=15.0):
        async def _loop():
            while True:
                m = await self.recv()
                if event and m.get("type") == "event" and m["event"] == event:
                    return m
                if command and m.get("type") == "response" and m["command"] == command:
                    return m

        return await asyncio.wait_for(_loop(), timeout)


async def test_direct_tcp_attach_stop_inspect_continue(tmp_path):
    port = _free_port()
    rdbg = await asyncio.create_subprocess_exec(
        "rdbg",
        "--open",
        "--port",
        str(port),
        "--host",
        "127.0.0.1",
        str(FIXTURES / "ruby_sleep.rb"),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        for _ in range(100):  # rdbg needs a moment to listen
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                break
            except OSError:
                await asyncio.sleep(0.1)
        else:
            pytest.fail("could not connect to rdbg")
        dap = TcpDap(reader, writer)
        dap.send("initialize", {"adapterID": "rdbg"})
        await dap.wait(command="initialize")
        dap.send(
            "attach", RdbgAdapter().attach_body(host="127.0.0.1", port=port, opts={})
        )
        await dap.wait(command="attach")
        dap.send("configurationDone")
        # non-nonstop attach: rdbg stops the waiting debuggee right after
        # configurationDone (stopped reason "pause")
        stopped = await dap.wait(event="stopped")
        assert stopped["body"]["reason"] == "pause"
        dap.send("stackTrace", {"threadId": stopped["body"].get("threadId", 1)})
        st = await dap.wait(command="stackTrace")
        assert st["body"]["stackFrames"], "expected a live stack"
        dap.send("continue", {"threadId": 1})
        await dap.wait(command="continue")
        writer.close()
    finally:
        if rdbg.returncode is None:
            rdbg.kill()
            await rdbg.wait()
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/integration/test_ruby_remote_attach.py -v`
Expected: PASS. If the `stopped` never arrives, check that the attach
arguments dict really reached rdbg (its DAP `attach` sets nonstop=false
when the argument is absent — `attach_body` intentionally omits it).

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_ruby_remote_attach.py
git commit -m "test: ruby remote attach — direct TCP DAP against live rdbg"
```

---

### Task 10: `--run` mode and record/replay

**Files:**
- Modify: `tests/integration/test_run_mode.py` (append)
- Create: `tests/integration/test_replay_ruby.py`

**Interfaces:**
- Consumes: `run_mode.run(program, config, profile, tui_episode)` (see the
  perl case in the same file), `build_ruby_profile` (Task 2),
  `load_recording`/`run_replay` from `tdb.replay`, `rdbg_ok` (Task 5).
- Produces: nothing new. Two spec follow-throughs, both verified during
  planning: (a) `replay.py`'s `_LAUNCH_REQUIRED`/`_ATTACH_REQUIRED` are
  required-header-KEY tuples, not per-language lists — **no replay.py
  change is needed**; (b) the spec's `test_ruby_session.py`
  (controller-level coverage) is realized by these run-mode and replay
  tests, which drive the full controller → proxy → rdbg stack — no
  separate file is added.

- [ ] **Step 1: Write the run-mode tests** (append to
  `tests/integration/test_run_mode.py`)

```python
from tests.integration.ruby_adapter_harness import (
    rdbg_ok,
)  # add to the file's import block

ruby_available = pytest.mark.skipif(
    not rdbg_ok(), reason="needs rdbg (debug gem >= 1.9)"
)


@ruby_available
async def test_ruby_runs_headless_without_tui_episode(tmp_path, capfd):
    from tdb.languages.ruby import build_ruby_profile

    p = tmp_path / "hello.rb"
    p.write_text('puts "rhello"\n')
    episodes = []

    async def fake_episode(controller, handler, console, config, program):
        episodes.append(controller.state.phase)
        return False

    code = await asyncio.wait_for(
        run_mode.run(
            program=str(p),
            config=TdbConfig(),
            profile=build_ruby_profile(),
            tui_episode=fake_episode,
        ),
        timeout=60.0,
    )
    assert episodes == [], "spurious TUI episode during headless ruby run"
    assert code == 0
    assert "rhello" in capfd.readouterr().out


@ruby_available
async def test_ruby_exit_code_passthrough(tmp_path, capfd):
    from tdb.languages.ruby import build_ruby_profile

    p = tmp_path / "exit7.rb"
    p.write_text('puts "rbye"\n$stdout.flush\nexit 7\n')
    code = await asyncio.wait_for(
        run_mode.run(program=str(p), config=TdbConfig(), profile=build_ruby_profile()),
        timeout=60.0,
    )
    assert code == 7
    assert "rbye" in capfd.readouterr().out
```

Put the `rdbg_ok` import in the file's top import block (matching how the
file structures `perl_available`); place `ruby_available` next to it.

- [ ] **Step 2: Write the replay test**

`tests/integration/test_replay_ruby.py` (model: `test_replay_perl.py`):

```python
"""Replay is language-agnostic: a ruby recording replays through the
ruby proxy adapter."""

import json

import pytest

from tdb.replay import load_recording, run_replay
from tests.integration.ruby_adapter_harness import rdbg_ok

pytestmark = pytest.mark.skipif(not rdbg_ok(), reason="needs rdbg (debug gem >= 1.9)")

TOY = """\
x = 1
y = 2
z = x + y
puts "z=#{z}"
"""


async def test_ruby_recording_replays(tmp_path):
    prog = tmp_path / "toy.rb"
    prog.write_text(TOY)
    header = {
        "tdb_recording": 1,
        "created": "2026-08-21T00:00:00",
        "mode": "launch",
        "language": "ruby",
        "program": str(prog),
        "args": [],
        "cwd": str(tmp_path),
        "python": None,
        "adapter": None,
        "step_mode": "line",
        "no_just_my_code": False,
    }
    records = [
        {"t": 0.1, "action": "set_breakpoint", "params": [f"{prog}:3"]},
        {"t": 0.2, "action": "continue", "params": []},
        {"t": 0.3, "action": "evaluate", "params": ["x + y"]},
        {"t": 0.4, "action": "quit", "params": []},
    ]
    path = tmp_path / "ruby.jsonl"
    path.write_text(
        "\n".join([json.dumps(header)] + [json.dumps(r) for r in records]) + "\n"
    )
    out: list[str] = []
    errors = await run_replay(load_recording(str(path)), echo=out.append)
    text = "\n".join(out)
    assert errors == 0
    assert "ok: 3" in text  # x + y evaluated through rdbg
```

- [ ] **Step 3: Run the tests**

Run: `uv run pytest tests/integration/test_run_mode.py -v -k ruby` then
`uv run pytest tests/integration/test_replay_ruby.py -v`
Expected: PASS. Notes:
- run-mode uses `pause_while_running` + stop events only — all
  passthrough; a hang usually means the launch never reached nonstop mode
  (check `stopOnEntry=False` → `attach nonstop: true`).
- replay evaluates through the RPC layer; `"ok: 3"` matches the
  `_print_command` echo format in replay.py.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_run_mode.py tests/integration/test_replay_ruby.py
git commit -m "test: ruby --run mode and record/replay round-trip"
```

---

### Task 11: File→Open for all languages

**Files:**
- Modify: `src/tdb/languages/registry.py` (two helpers)
- Modify: `src/tdb/widgets/modals.py` (`_PyFileTree` → `_SourceFileTree`;
  `_OpenFileModal` gains `suffixes`)
- Modify: `src/tdb/app.py` (compose label gate ~line 301-309;
  `action_open_file` ~line 1388)
- Test: `tests/unit/test_open_file_all_languages.py`

**Interfaces:**
- Consumes: `_EXTENSION_MAP`, `detect`, `LanguageNotSupportedError`
  (registry).
- Produces: `registry.extensions_for(lang_id: str) -> tuple[str, ...]`;
  `registry.matches_language(path: str, lang_id: str) -> bool`;
  `_OpenFileModal(initial_path, suffixes: tuple[str, ...])`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_open_file_all_languages.py`:

```python
"""File->Open ungating: extension filters + same-language validation."""

from pathlib import Path

from tdb.languages import registry
from tdb.widgets.modals import _SourceFileTree


def test_extensions_for_each_language():
    assert registry.extensions_for("python") == (".py", ".pyw")
    assert registry.extensions_for("ruby") == (".rb",)
    assert registry.extensions_for("perl") == (".pl", ".pm", ".t")
    assert registry.extensions_for("bash") == (".bash", ".sh")
    assert registry.extensions_for("cpp") == ()  # magic bytes, no suffix


def test_matches_language(tmp_path):
    rb = tmp_path / "x.rb"
    rb.write_text("puts 1\n")
    py = tmp_path / "x.py"
    py.write_text("print(1)\n")
    assert registry.matches_language(str(rb), "ruby") is True
    assert registry.matches_language(str(py), "ruby") is False
    assert registry.matches_language(str(py), "python") is True
    # unknown extension -> False, never an exception
    junk = tmp_path / "x.xyz"
    junk.write_text("?")
    assert registry.matches_language(str(junk), "python") is False


def test_source_tree_filters_by_suffix(tmp_path):
    (tmp_path / "a.rb").write_text("")
    (tmp_path / "b.py").write_text("")
    (tmp_path / "sub").mkdir()
    tree = _SourceFileTree(str(tmp_path), suffixes=(".rb",))
    kept = {p.name for p in tree.filter_paths(tmp_path.iterdir())}
    assert kept == {"a.rb", "sub"}


def test_source_tree_empty_suffixes_shows_everything(tmp_path):
    (tmp_path / "a.rb").write_text("")
    (tmp_path / "binary").write_text("")
    tree = _SourceFileTree(str(tmp_path), suffixes=())
    kept = {p.name for p in tree.filter_paths(tmp_path.iterdir())}
    assert kept == {"a.rb", "binary"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_open_file_all_languages.py -v`
Expected: FAIL — `extensions_for`/`_SourceFileTree` don't exist.

- [ ] **Step 3: Add the registry helpers** (append to `registry.py`,
  before the `register(...)` block)

```python
def extensions_for(lang_id: str) -> tuple[str, ...]:
    """Extensions mapped to `lang_id`, for UI file filters (File > Open).

    Empty for languages detected by other means (cpp: binary magic
    bytes) — callers treat empty as "show all files".
    """
    return tuple(sorted(ext for ext, lang in _EXTENSION_MAP.items() if lang == lang_id))


def matches_language(path: str, lang_id: str) -> bool:
    """True when `path` detects as `lang_id` (File > Open's
    same-language guard). Detection failure counts as a mismatch."""
    try:
        return detect(path) == lang_id
    except LanguageNotSupportedError:
        return False
```

- [ ] **Step 4: Generalize the modal** (`src/tdb/widgets/modals.py`)

Replace `_PyFileTree` (line ~232) with:

```python
class _SourceFileTree(DirectoryTree):
    """DirectoryTree showing directories plus files matching `suffixes`
    (all files when `suffixes` is empty — cpp binaries have none)."""

    def __init__(self, path: str, suffixes: tuple[str, ...] = (), **kwargs) -> None:
        self._suffixes = suffixes
        super().__init__(path, **kwargs)

    def filter_paths(self, paths):
        if not self._suffixes:
            return list(paths)
        return [p for p in paths if p.is_dir() or p.suffix.lower() in self._suffixes]
```

Then in `_OpenFileModal`:
- Docstring: "Modal file picker for selecting a source file to debug."
- Rename every `_PyFileTree` reference (CSS selectors keep working —
  they target `DirectoryTree`; grep the file to be sure no `_PyFileTree`
  string remains).
- `__init__(self, initial_path: str, suffixes: tuple[str, ...] = ()) ->
  None` — store `self._suffixes = suffixes`.
- Header Static becomes:

```python
            kinds = ", ".join(self._suffixes) if self._suffixes else "all"
            yield Static(
                f"[bold]Open file to debug[/bold]  ({kinds} files)",
                id="open-header",
                markup=True,
            )
```

- `compose` yields `_SourceFileTree(str(self._current_path),
  suffixes=self._suffixes, id="file-tree")`.
- `on_mount` / `action_go_up` query `_SourceFileTree` instead of
  `_PyFileTree`.
- `on_directory_tree_file_selected` becomes:

```python
        path = Path(event.path)
        if not self._suffixes or path.suffix.lower() in self._suffixes:
            self.dismiss(str(path))
```

- [ ] **Step 5: Ungate app.py**

1. In `compose()` (~app.py:301-309): delete the python-only comment and
   the `if self.controller.profile.id == "python":` guard; always set
   `leading_action_labels["open-file-label"] = "File"`. Keep a one-line
   comment: `# File > Open is language-aware: the picker filters to the
   profile's extensions and validates the pick (action_open_file).`
2. In `action_open_file()` (~app.py:1388): replace the python-only guard
   with a remote-attach guard mirroring `_restart_session`'s own, and add
   validation in the dismiss callback. Preserve the existing
   `self._restart_session(...)` invocation exactly as-is:

```python
    def action_open_file(self) -> None:
        if not self.controller.supports_restart:
            # File > Open relaunches via _restart_session, which has
            # nothing to relaunch in remote-attach / tdb.breakpoint()
            # sessions (mirrors _restart_session's own R-key guard).
            self.notify(
                "File > Open is not available in remote-attach mode.",
                severity="warning",
            )
            return
        from tdb.languages import registry

        profile = self.controller.profile
        initial = self._cwd or (
            str(Path(self._program).parent) if self._program else str(Path.cwd())
        )

        def on_dismiss(path: str | None) -> None:
            if not path:
                return
            if not registry.matches_language(path, profile.id):
                self.notify(
                    f"{Path(path).name} is not a {profile.display_name} "
                    f"program — this session debugs {profile.display_name}.",
                    severity="warning",
                )
                return
            self._restart_session(new_program=path, start_immediately=False)

        self.push_screen(
            _OpenFileModal(initial, suffixes=registry.extensions_for(profile.id)),
            callback=on_dismiss,
        )
```

(If the current code wraps `_restart_session` differently — e.g. via
`run_worker` — keep that exact call shape; only the guard and validation
change.) Also audit the two entry paths per the project rule: menu click
(`on_menu_bar_action_label_clicked` → `action_open_file`) and Alt+F
(`action_menu_file` → `action_open_file`) both converge here — no other
path exists (verify with `grep -n "action_open_file" src/tdb/app.py`).

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/unit/test_open_file_all_languages.py tests/unit -q`
Expected: new tests PASS; full unit suite PASS (an existing unit test may
reference `_PyFileTree` or assert the python-only File label — update any
such test to the new names/behavior, they are part of this task's scope).

- [ ] **Step 7: Commit**

```bash
git add src/tdb/languages/registry.py src/tdb/widgets/modals.py src/tdb/app.py tests/unit/test_open_file_all_languages.py
git commit -m "feat: File > Open for all languages with same-language validation"
```

---

### Task 12: Dockerfile, docs, full-suite verification

**Files:**
- Modify: `Dockerfile:18`
- Modify: `README.md`
- Test: full suite

**Interfaces:**
- Consumes: everything above.
- Produces: CI coverage for ruby; user-facing docs.

- [ ] **Step 1: Dockerfile**

Replace line 18 with:

```dockerfile
# perl/bash/tcsh power their DAP adapters; ruby + the debug gem power
# the rdbg proxy (the gem has a C extension, hence the build deps).
RUN apk add --no-cache perl bash tcsh ruby ruby-dev make gcc musl-dev \
 && gem install debug --no-document
```

If Alpine's `ruby` package already bundles a debug gem ≥ 1.9 with the
`rdbg` binstub on PATH (`docker run --rm <img> rdbg --version`), the
`gem install` + build deps may be dropped — verify, don't assume.

- [ ] **Step 2: Verify the image (if docker is available)**

Run: `docker build --target base -t tdb-ruby-check . && docker run --rm tdb-ruby-check rdbg --version`
Expected: `rdbg 1.x.y` with x ≥ 9 (or 2+). Then run the ruby integration
tests inside:
`docker run --rm --cpus=2 --memory=7g --init tdb-ruby-check uv run pytest tests/integration/test_ruby_adapter_launch.py -x -vv -p no:cacheprovider --no-cov`
If docker is unavailable in this environment, note it in the commit
message body (`Dockerfile change unverified locally; CI will verify`)
and continue.

- [ ] **Step 3: README**

Read `README.md` and update every place that enumerates supported
languages (search for "Perl" and "bash" to find them all): the language
support table/list, the `--terminal`/`--run`/remote-attach/record-replay
sections' language mentions, and installation requirements. Add a
"Debugging Ruby" subsection alongside the Perl one with this content
(adapt formatting to the file's existing style):

```markdown
### Ruby

tdb debugs Ruby via the [debug gem](https://github.com/ruby/debug)'s
`rdbg` (Ruby >= 3.1 ships it; otherwise `gem install debug`; tdb needs
debug >= 1.9). `rdbg` must be on PATH, or point tdb at it with
`{"adapters": {"rdbg": "/path/to/rdbg"}}` in config.json.

    tdb script.rb                # launch, stop at first line
    tdb --run script.rb          # run immediately, debug on demand
    tdb --terminal script.rb     # program I/O in its own terminal

Remote attach: start the program with
`rdbg --open --port 5678 --host 0.0.0.0 script.rb` (add `--nonstop` to
let it run before you attach), then `tdb -r HOST:5678 --lang ruby`.
Note: rdbg's `--cookie` authentication is not part of DAP and is not
supported — bind to localhost and tunnel over SSH instead.
`--local-root`/`--remote-root` path mappings are not supported for Ruby
yet. Bundler projects work when your environment resolves `rdbg`
(`gem install debug` into the project's Ruby); there is no `bundle
exec` integration yet.
```

Also mention in the File→Open documentation (if the README documents the
menu) that it now works for every language and requires the picked file
to match the session's language.

- [ ] **Step 4: Full suite**

Run: `uv run pytest`
Expected: everything passes (ruby integration tests run for real on this
machine — rdbg 1.11.1 is installed). Fix anything that broke before
committing.

- [ ] **Step 5: Commit**

```bash
git add Dockerfile README.md
git commit -m "docs+ci: Ruby language support — README section, Alpine ruby/debug gem"
```

---

## Final verification (after all tasks)

- [ ] `uv run pytest` — full suite green.
- [ ] Manual smoke: `uv run tdb tests/integration/fixtures/ruby_vars.rb` —
  set a breakpoint by clicking line 13, `c` to it, inspect `total` in the
  Variable View, evaluate `outer(5)` in the console, tab-complete `tot`,
  `q` to quit. Then `uv run tdb --run tests/integration/fixtures/ruby_sleep.rb`,
  Ctrl-C/pause into the TUI, `q`.
- [ ] `ps aux | grep rdbg` — no orphaned rdbg processes after the smoke
  tests.
- [ ] Use superpowers:requesting-code-review before merging.

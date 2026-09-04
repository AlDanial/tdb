# PowerShell Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Debug PowerShell 7 scripts in tdb (launch, breakpoints, stepping, variables, evaluate, console output, fatal-error modal, `--run`) through PowerShell Editor Services (PSES).

**Architecture:** A bundled Python DAP proxy (`tdb.adapters.powershell`, the Ruby shape) speaks stdio to tdb, spawns `pwsh` running PSES's `Start-EditorServices.ps1` in debug-only named-pipe mode, connects to the UNIX socket named in PSES's session file, and forwards DAP both ways while normalizing PSES quirks (dead `stopOnEntry`/`env`, `pause` reported as `step`, `repl` evaluate printing to stdout, no `terminate`, no `exited`, unquoted args). pwsh's stdout is pumped into DAP `output` events. A bundled launcher script `tdb_launch.ps1` runs the user script with the call operator and prints an exit-code sentinel. The language profile in `tdb.languages.powershell` stays thin; nothing PSES-specific touches the DAP client, controller, or widgets.

**Tech Stack:** Python 3.11+ asyncio, existing `tdb.dap.protocol` framing, pytest (`asyncio_mode = "auto"`), pwsh 7.x + PSES v4.7.0 for integration tests, Alpine Docker CI.

**Spec:** `docs/superpowers/specs/2026-09-03-powershell-support-design.md` (read the Addendum section too — it overrides the body where they conflict).

## Global Constraints

- Branch `add-powershell-support`; all commits go there.
- `pwsh` >= 7.0 required (proxy refuses lower); `pwsh` >= 7.2 documented; PSES pinned to `v4.7.0` in README/Dockerfile.
- Config keys: `{"adapters": {"pwsh": "<exe>"}}` (interpreter) and `{"adapters": {"pses": "<dir>"}}` (PSES module dir). Env override for the module: `TDB_PSES_PATH`. Precedence: config > env > VS Code extension dir.
- Profile id `"powershell"`, display name `"PowerShell"`, adapter id `"pses"`, lexer `"powershell"`, `frame_placeholder="<ScriptBlock>"`.
- Extensions `.ps1`, `.psm1`; shebang first line containing `pwsh`.
- `--terminal`, `-r`, `-a` rejected for PowerShell. `--run` allowed (`pause_while_running=True`).
- The proxy sets `NO_COLOR=1` and `TERM=dumb` on the pwsh process.
- Every user arg and the user script path are PowerShell single-quoted (`'` -> `''`) before forwarding to PSES.
- Exit code: sentinel `"\x1etdb-exit:<n>"` on stdout -> n; PSES `terminated` with no sentinel -> 1; pwsh died without `terminated` -> pwsh's return code.
- Windows: named-pipe branch written, unit-tested only by platform dispatch; not verified.
- Never commit `examples/powershell_7.6.5-1.deb_amd64.deb`.
- Use `uv run pytest ...` to run tests (repo convention; `uv pip install` for packages).
- Nothing PSES-specific in `src/tdb/dap/`, `src/tdb/session/`, or `src/tdb/widgets/`.

---

## File Structure

Create:
- `src/tdb/languages/powershell.py` — `PsesAdapter(AdapterSpec)`, `build_powershell_profile`.
- `src/tdb/adapters/seqs.py` — generic `SeqTranslator` (client <-> upstream seq renumbering). The ruby proxy keeps its own copy; migrating it is out of scope.
- `src/tdb/adapters/powershell/__init__.py` — hints/constants docstring.
- `src/tdb/adapters/powershell/__main__.py` — stdio entry point.
- `src/tdb/adapters/powershell/locate.py` — `find_pwsh`, `find_pses`.
- `src/tdb/adapters/powershell/output.py` — `quote_ps_arg`, `parse_exit_sentinel`, `OutputClassifier`.
- `src/tdb/adapters/powershell/server.py` — `PowerShellDapServer`, `build_pwsh_command`, `connect_debug_service`.
- `src/tdb/adapters/powershell/tdb_launch.ps1` — launcher wrapper.
- `tests/unit/test_powershell_profile.py`, `tests/unit/test_registry_powershell.py`, `tests/unit/test_powershell_errors.py`, `tests/unit/test_pses_locate.py`, `tests/unit/test_powershell_output.py`, `tests/unit/test_seqs.py`, `tests/unit/fake_pses.py`, `tests/unit/test_powershell_proxy.py`.
- `tests/integration/powershell_adapter_harness.py`, `tests/integration/fixtures/powershell/{simple,functions,loop,throws,writes_error,exit7}.ps1`, `tests/integration/test_powershell_adapter_launch.py`, `test_powershell_adapter_breakpoints.py`, `test_powershell_adapter_stepping.py`, `test_powershell_adapter_inspection.py`, `test_powershell_session.py`, `test_powershell_run_mode.py`, `test_replay_powershell.py`.
- `examples/hello_powershell.ps1`.

Modify:
- `src/tdb/languages/registry.py` — extension map, shebang, registration.
- `src/tdb/languages/errors.py` — `parse_powershell_error`.
- `src/tdb/cli.py` — `--terminal` guard for adapter id `pses`.
- `pyproject.toml` — package data for `tdb_launch.ps1` (check whether `*.ps1` under `src/tdb` is already included; add if not).
- `Dockerfile`, `.github/workflows/test.yml`, `README.md`.
- `tests/unit/test_open_file_all_languages.py` — extensions assertion.

---

### Task 1: Language profile, detection, CLI guard

**Files:**
- Create: `src/tdb/languages/powershell.py`
- Modify: `src/tdb/languages/registry.py` (`_EXTENSION_MAP`, shebang chain in `detect`, registration block at the bottom)
- Modify: `src/tdb/cli.py` (the `--terminal` guard block after the `dlv` guard, ~line 561)
- Modify: `tests/unit/test_open_file_all_languages.py`
- Test: `tests/unit/test_powershell_profile.py`, `tests/unit/test_registry_powershell.py`

**Interfaces:**
- Produces: `tdb.languages.powershell.PsesAdapter(pwsh_executable: str | None = None, pses_dir: str | None = None)` with `id == "pses"`, `command() -> [sys.executable, "-m", "tdb.adapters.powershell"]`, `launch_body(...)` returning the dict shown below (keys `"pwsh"` / `"pses"` only when overrides are set).
- Produces: `tdb.languages.powershell.build_powershell_profile(adapter=None, adapter_paths=None, program=None) -> LanguageProfile`.
- Consumes: `tdb.languages.errors.parse_powershell_error` (Task 2) — import it lazily? No: Task 2 must land before this profile references it. **Order: do Task 2 first, or in Task 1 set `parse_error=None` and switch it in Task 2.** This plan does the latter (Task 2 flips it).

- [ ] **Step 1: Write the failing profile tests**

`tests/unit/test_powershell_profile.py`:

```python
import sys

import pytest

from tdb.dap.types import Capabilities
from tdb.languages import registry
from tdb.languages.base import LanguageNotSupportedError
from tdb.languages.powershell import PsesAdapter, build_powershell_profile


def test_profile_shape():
    p = build_powershell_profile()
    assert p.id == "powershell"
    assert p.display_name == "PowerShell"
    assert p.adapter.id == "pses"
    assert p.presentation.lexer == "powershell"
    assert p.presentation.frame_placeholder == "<ScriptBlock>"
    assert p.capabilities.compute_step_units is None
    assert p.capabilities.task_inspection is False
    assert p.capabilities.child_process_strategy is None
    assert p.capabilities.pause_while_running is True
    assert p.adapter.quirks.attach_via_adapter is False
    assert p.adapter.quirks.pre_arm_pause_on_attach is False


def test_registered_in_registry():
    assert "powershell" in registry.known_languages()
    assert registry.resolve("powershell").id == "powershell"


def test_unknown_adapter_rejected():
    with pytest.raises(LanguageNotSupportedError, match="known: pses"):
        build_powershell_profile(adapter="bogus")


def test_command_is_bundled_proxy():
    assert PsesAdapter().command() == [
        sys.executable,
        "-m",
        "tdb.adapters.powershell",
    ]


def test_launch_body_carries_overrides():
    body = PsesAdapter(pwsh_executable="/opt/pwsh", pses_dir="/opt/pses").launch_body(
        program="/x/p.ps1",
        args=["a"],
        cwd="/x",
        env={"K": "V"},
        stop_on_entry=True,
        console="internalConsole",
        opts={},
    )
    assert body == {
        "type": "powershell",
        "request": "launch",
        "program": "/x/p.ps1",
        "args": ["a"],
        "cwd": "/x",
        "stopOnEntry": True,
        "console": "internalConsole",
        "env": {"K": "V"},
        "pwsh": "/opt/pwsh",
        "pses": "/opt/pses",
    }


def test_launch_body_omits_optional_keys():
    body = PsesAdapter().launch_body(
        program="/x/p.ps1",
        args=[],
        cwd="/x",
        env=None,
        stop_on_entry=False,
        console="internalConsole",
        opts={},
    )
    assert "env" not in body and "pwsh" not in body and "pses" not in body
    assert body["stopOnEntry"] is False


def test_overrides_come_from_adapter_paths():
    p = build_powershell_profile(adapter_paths={"pwsh": "/p", "pses": "/s"})
    body = p.adapter.launch_body(
        program="/x/p.ps1",
        args=[],
        cwd="/x",
        env=None,
        stop_on_entry=True,
        console="internalConsole",
        opts={},
    )
    assert body["pwsh"] == "/p" and body["pses"] == "/s"


def test_external_terminal_rejected():
    with pytest.raises(LanguageNotSupportedError, match="--terminal is not supported"):
        PsesAdapter().launch_body(
            program="/x/p.ps1",
            args=[],
            cwd="/x",
            env=None,
            stop_on_entry=True,
            console="externalTerminal",
            opts={},
        )


def test_attach_rejected():
    with pytest.raises(LanguageNotSupportedError):
        PsesAdapter().attach_body(host="h", port=1, opts={})


def test_no_exception_filters():
    caps = Capabilities(exception_breakpoint_filters=[{"filter": "x", "default": True}])
    assert PsesAdapter().pick_exception_filters(caps) == []
```

`tests/unit/test_registry_powershell.py`:

```python
from tdb.languages import registry


def test_ps1_extension_detects_powershell(tmp_path):
    p = tmp_path / "x.ps1"
    p.write_text("Write-Host 1\n")
    assert registry.detect(str(p)) == "powershell"


def test_psm1_extension_detects_powershell(tmp_path):
    p = tmp_path / "m.psm1"
    p.write_text("function F {}\n")
    assert registry.detect(str(p)) == "powershell"


def test_pwsh_shebang_detects_powershell(tmp_path):
    p = tmp_path / "script"
    p.write_text("#!/usr/bin/env pwsh\nWrite-Host 1\n")
    assert registry.detect(str(p)) == "powershell"


def test_extensions_for_powershell():
    assert registry.extensions_for("powershell") == (".ps1", ".psm1")


def test_resolve_default_adapter():
    assert registry.resolve("powershell").adapter.id == "pses"
```

Check `tests/unit/test_open_file_all_languages.py` for a test enumerating every language's extensions; if it has an exhaustive list, add `assert registry.extensions_for("powershell") == (".ps1", ".psm1")` next to the ruby line.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_powershell_profile.py tests/unit/test_registry_powershell.py -q --no-cov`
Expected: FAIL with `ModuleNotFoundError: No module named 'tdb.languages.powershell'` and registry assertions failing.

- [ ] **Step 3: Write the profile module**

`src/tdb/languages/powershell.py`:

```python
"""The PowerShell language profile.

The adapter is tdb's bundled proxy (python -m tdb.adapters.powershell)
in front of PowerShell Editor Services (PSES), the DAP server behind the
VS Code PowerShell extension. Config twist (same shape as perl/ruby):
{"adapters": {"pwsh": "/path/to/pwsh"}} names the interpreter and
{"adapters": {"pses": "/path/to/PowerShellEditorServices"}} names the
PSES module directory; neither selects an adapter binary. A missing
pwsh/PSES is reported by the proxy at launch, not here.

Core-DAP capabilities plus --run. No --terminal, no attach in v1 (see
docs/superpowers/specs/2026-09-03-powershell-support-design.md).
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


class PsesAdapter(AdapterSpec):
    id = "pses"
    quirks = AdapterQuirks()

    def __init__(
        self, pwsh_executable: str | None = None, pses_dir: str | None = None
    ) -> None:
        self._pwsh = pwsh_executable
        self._pses = pses_dir

    def command(self) -> list[str]:
        return [sys.executable, "-m", "tdb.adapters.powershell"]

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
                "--terminal is not supported for PowerShell yet (PSES has "
                "no terminal integration in tdb's debug-only mode)"
            )
        body: dict[str, Any] = {
            "type": "powershell",
            "request": "launch",
            "program": program,
            "args": args,
            "cwd": cwd,
            "stopOnEntry": stop_on_entry,
            "console": console,
        }
        if env:
            body["env"] = env
        if self._pwsh:
            body["pwsh"] = self._pwsh
        if self._pses:
            body["pses"] = self._pses
        return body

    def attach_body(
        self, *, host: str, port: int, opts: dict[str, Any]
    ) -> dict[str, Any]:
        raise LanguageNotSupportedError("PowerShell does not support remote attach")

    def pick_exception_filters(self, caps) -> list[str]:
        return []


def build_powershell_profile(
    adapter: str | None = None,
    adapter_paths: dict[str, str] | None = None,
    program: str | None = None,
) -> LanguageProfile:
    if adapter not in (None, "pses"):
        raise LanguageNotSupportedError(
            f"unknown adapter {adapter!r} for powershell (known: pses)"
        )
    paths = adapter_paths or {}
    return LanguageProfile(
        id="powershell",
        display_name="PowerShell",
        adapter=PsesAdapter(
            pwsh_executable=paths.get("pwsh"), pses_dir=paths.get("pses")
        ),
        presentation=Presentation(
            lexer="powershell",
            parse_error=None,  # Task 2 wires parse_powershell_error
            frame_placeholder="<ScriptBlock>",
        ),
        capabilities=ProfileCapabilities(pause_while_running=True),
    )
```

- [ ] **Step 4: Register in the registry**

In `src/tdb/languages/registry.py`:

Add to `_EXTENSION_MAP` after the `.tcsh` line:

```python
    ".ps1": "powershell",
    ".psm1": "powershell",
```

In `detect`, after the `csh` shebang check and before the final `raise`:

```python
    if head.startswith(b"#!") and b"pwsh" in head.splitlines()[0]:
        return "powershell"
```

At the bottom, after the go registration:

```python
from tdb.languages.powershell import build_powershell_profile  # noqa: E402

register("powershell", build_powershell_profile)
```

Update the module docstring's detection list to mention `.ps1`/`.psm1` -> powershell and the `pwsh` shebang.

- [ ] **Step 5: Add the CLI `--terminal` guard**

In `src/tdb/cli.py`, directly after the `dlv` guard block (the one raising "--terminal is not supported for Go yet"):

```python
    # PSES in tdb's debug-only mode has no terminal integration (see
    # PsesAdapter.launch_body, which raises the same error as a backstop).
    if args.terminal and profile.adapter.id == "pses":
        parser.error(
            "--terminal is not supported for PowerShell yet (PSES has no "
            "terminal integration in tdb's debug-only mode)"
        )
```

Find the existing CLI test for the dlv guard (`grep -n "not supported for Go" tests/unit/test_cli_go.py`) and add the analogous test in `tests/unit/test_powershell_profile.py`:

```python
def test_cli_rejects_terminal_for_powershell(tmp_path, monkeypatch):
    from tdb import cli

    script = tmp_path / "s.ps1"
    script.write_text("Write-Host 1\n")
    with pytest.raises(SystemExit):
        cli.parse_args(["--terminal", "xterm", str(script)])
```

If `cli.parse_args` requires the terminal executable to exist on PATH first (`_validate_terminal_choice`), monkeypatch `shutil.which` to return `"/usr/bin/xterm"` inside the test, mirroring how `test_cli_go.py` does it.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_powershell_profile.py tests/unit/test_registry_powershell.py tests/unit/test_open_file_all_languages.py tests/unit/test_language_registry.py tests/unit/test_profile_contract.py -q --no-cov`
Expected: all PASS. If `test_profile_contract.py` enumerates languages and fails on the new one, fix the contract (it likely asserts every registered profile has certain attributes — the profile above satisfies them).

- [ ] **Step 7: Commit**

```bash
git add src/tdb/languages/powershell.py src/tdb/languages/registry.py src/tdb/cli.py tests/unit/test_powershell_profile.py tests/unit/test_registry_powershell.py tests/unit/test_open_file_all_languages.py
git commit -m "PowerShell: language profile, detection, --terminal guard"
```

---

### Task 2: Fatal-error parser

**Files:**
- Modify: `src/tdb/languages/errors.py` (append after `parse_go_error`)
- Modify: `src/tdb/languages/powershell.py` (wire `parse_error`)
- Test: `tests/unit/test_powershell_errors.py`

**Interfaces:**
- Produces: `parse_powershell_error(stderr: str, exit_code: int | None = None) -> ParsedError | None`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_powershell_errors.py` (samples captured from pwsh 7.6.5 with `NO_COLOR=1`):

```python
from tdb.languages.errors import parse_powershell_error
from tdb.languages.powershell import build_powershell_profile

THROW = """\
before
Exception: /tmp/w/e1.ps1:2
Line |
   2 |  function Inner { throw "kaboom" }
     |                   ~~~~~~~~~~~~~~
     | kaboom
"""

DOTNET = """\
x
MethodInvocationException: /tmp/w/e2.ps1:2
Line |
   2 |  $n = [int]::Parse("abc")
     |  ~~~~~~~~~~~~~~~~~~~~~~~~
     | Exception calling "Parse" with "1" argument(s): "The input string 'abc'
     | was not in a correct format."
"""

CMDLET = """\
Get-Item: /tmp/w/e4.ps1:1
Line |
   1 |  Get-Item /nonexistent/zzz
     |  ~~~~~~~~~~~~~~~~~~~~~~~~~
     | Cannot find path '/nonexistent/zzz' because it does not exist.
continues
"""

WRITE_ERROR = "Write-Error: not fatal\nstill here\n"

ANSI_THROW = (
    "\x1b[31;1mException: \x1b[0m/tmp/w/e1.ps1:2\x1b[0m\n"
    "\x1b[31;1m\x1b[0m\x1b[36;1mLine |\x1b[0m\n"
    "     | \x1b[31;1mkaboom\x1b[0m\n"
)


def test_throw_parses_to_one_frame():
    err = parse_powershell_error(THROW, exit_code=1)
    assert err is not None
    assert err.header == "Exception: /tmp/w/e1.ps1:2"
    assert err.message == "kaboom"
    assert [(f.path, f.line, f.func) for f in err.frames] == [("/tmp/w/e1.ps1", 2, "")]
    assert err.detail.startswith("Exception: /tmp/w/e1.ps1:2")
    assert "kaboom" in err.detail


def test_dotnet_exception_joins_multiline_message():
    err = parse_powershell_error(DOTNET, exit_code=1)
    assert err is not None
    assert err.header == "MethodInvocationException: /tmp/w/e2.ps1:2"
    assert err.message == (
        'Exception calling "Parse" with "1" argument(s): '
        "\"The input string 'abc' was not in a correct format.\""
    )


def test_cmdlet_error_kind():
    err = parse_powershell_error(CMDLET, exit_code=1)
    assert err is not None
    assert err.frames[0].line == 1
    assert err.message.startswith("Cannot find path")


def test_exit_code_zero_is_not_fatal():
    assert parse_powershell_error(CMDLET, exit_code=0) is None
    assert parse_powershell_error(THROW, exit_code=None) is None


def test_write_error_is_not_fatal():
    assert parse_powershell_error(WRITE_ERROR, exit_code=1) is None


def test_ansi_is_stripped():
    err = parse_powershell_error(ANSI_THROW, exit_code=1)
    assert err is not None
    assert err.header == "Exception: /tmp/w/e1.ps1:2"
    assert err.message == "kaboom"


def test_profile_wires_parser():
    assert build_powershell_profile().presentation.parse_error is parse_powershell_error
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_powershell_errors.py -q --no-cov`
Expected: FAIL with `ImportError: cannot import name 'parse_powershell_error'`.

- [ ] **Step 3: Implement the parser**

Append to `src/tdb/languages/errors.py`:

```python
# --- PowerShell ------------------------------------------------------------
# pwsh 7's default "ConciseView" error rendering:
#
#   <Kind>: <path>:<line>          Kind = Exception | FooException | Cmdlet-Name
#   Line |
#      2 |  $n = [int]::Parse("abc")
#        |  ~~~~~~~~~~~~~~~~~~~~~~~~
#        | <message, possibly continued on more "| " lines>
#
# The identical block is printed for NON-terminating errors (script keeps
# running, exit 0) and for the terminating error that ends the script
# (exit 1), so `exit_code` is the only fatal-vs-not signal.
_PS_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_PS_HEAD_RE = re.compile(r"^(?P<kind>[A-Za-z][\w.-]*): (?P<path>.+?):(?P<line>\d+)\s*$")
_PS_CONT_RE = re.compile(r"^\s*(?:Line \||\d+ \||\|)(?P<rest>.*)$")
_PS_MSG_RE = re.compile(r"^\s*\|\s?(?P<msg>.*)$")


def parse_powershell_error(
    stderr: str, exit_code: int | None = None
) -> ParsedError | None:
    """Parse pwsh's ConciseView error block into a ParsedError.

    Returns None unless ``exit_code`` is a non-zero int: PowerShell prints
    the same block for non-terminating errors, after which the script
    continues and exits 0. The LAST block in the text is the fatal one
    (earlier ones were non-terminating).
    """
    if not exit_code:
        return None
    lines = [_PS_ANSI_RE.sub("", ln) for ln in stderr.splitlines()]
    head_idx = None
    for i, ln in enumerate(lines):
        if _PS_HEAD_RE.match(ln):
            head_idx = i
    if head_idx is None:
        return None
    head = _PS_HEAD_RE.match(lines[head_idx])
    assert head is not None
    block = [lines[head_idx]]
    msg_parts: list[str] = []
    for ln in lines[head_idx + 1 :]:
        if not _PS_CONT_RE.match(ln):
            break
        block.append(ln)
        m = _PS_MSG_RE.match(ln)
        if m is None:
            continue  # "Line |" or "   2 | source" rows
        text = m.group("msg").strip()
        if not text or set(text) <= {"~", " "}:
            continue  # the squiggle row
        msg_parts.append(text)
    return ParsedError(
        header=lines[head_idx].strip(),
        message=" ".join(msg_parts),
        frames=[
            ErrorFrame(path=head.group("path"), line=int(head.group("line")), func="")
        ],
        detail="\n".join(block).rstrip(),
    )
```

Note on `_PS_MSG_RE`: a source row like `   2 |  $n = ...` also matches `^\s*\|`? No — it starts with digits, so `_PS_MSG_RE` (which requires `|` as the first non-space char) does not match it; only the squiggle row and message rows do, and the squiggle row is skipped by the `~` check.

- [ ] **Step 4: Wire the parser into the profile**

In `src/tdb/languages/powershell.py`, add `from tdb.languages.errors import parse_powershell_error` and replace `parse_error=None,  # Task 2 wires parse_powershell_error` with `parse_error=parse_powershell_error,`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_powershell_errors.py tests/unit/test_powershell_profile.py -q --no-cov`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/tdb/languages/errors.py src/tdb/languages/powershell.py tests/unit/test_powershell_errors.py
git commit -m "PowerShell: ConciseView fatal-error parser"
```

---

### Task 3: pwsh and PSES locator

**Files:**
- Create: `src/tdb/adapters/powershell/__init__.py`, `src/tdb/adapters/powershell/locate.py`
- Test: `tests/unit/test_pses_locate.py`

**Interfaces:**
- Produces: `find_pwsh(override: str | None) -> str` (raises `FileNotFoundError(PWSH_HINT)`), `find_pses(override: str | None, env: Mapping[str, str] | None = None, home: Path | None = None) -> Path` (returns the directory containing `Start-EditorServices.ps1`; raises `FileNotFoundError` whose message names the missing thing plus `PSES_HINT`), constants `PSES_ENV_VAR = "TDB_PSES_PATH"`, `PSES_RELEASE = "v4.7.0"`, `START_SCRIPT = "Start-EditorServices.ps1"`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_pses_locate.py`:

```python
from pathlib import Path

import pytest

from tdb.adapters.powershell.locate import (
    PSES_ENV_VAR,
    PSES_RELEASE,
    START_SCRIPT,
    find_pses,
    find_pwsh,
)


def _make_pses(root: Path) -> Path:
    d = root / "PowerShellEditorServices"
    d.mkdir(parents=True)
    (d / START_SCRIPT).write_text("# stub\n")
    return d


def test_override_dir_wins(tmp_path):
    d = _make_pses(tmp_path / "cfg")
    _make_pses(tmp_path / "envdir")
    assert (
        find_pses(str(d), env={PSES_ENV_VAR: str(tmp_path / "envdir")}, home=tmp_path)
        == d
    )


def test_override_accepts_unzip_root(tmp_path):
    d = _make_pses(tmp_path / "unzipped")
    assert find_pses(str(tmp_path / "unzipped"), env={}, home=tmp_path) == d


def test_env_var_used_when_no_override(tmp_path):
    d = _make_pses(tmp_path / "envdir")
    assert (
        find_pses(None, env={PSES_ENV_VAR: str(tmp_path / "envdir")}, home=tmp_path)
        == d
    )


def test_vscode_extension_newest_version_wins(tmp_path):
    ext = tmp_path / ".vscode" / "extensions"
    old = _make_pses(ext / "ms-vscode.powershell-2024.2.1" / "modules")
    new = _make_pses(ext / "ms-vscode.powershell-2025.10.0" / "modules")
    assert old != new
    assert find_pses(None, env={}, home=tmp_path) == new


def test_vscode_insiders_and_server_dirs_are_searched(tmp_path):
    d = _make_pses(
        tmp_path
        / ".vscode-server"
        / "extensions"
        / "ms-vscode.powershell-2025.1.0"
        / "modules"
    )
    assert find_pses(None, env={}, home=tmp_path) == d


def test_not_found_message_is_actionable(tmp_path):
    with pytest.raises(FileNotFoundError) as ei:
        find_pses(None, env={}, home=tmp_path)
    msg = str(ei.value)
    assert PSES_RELEASE in msg
    assert "PowerShellEditorServices.zip" in msg
    assert '"pses"' in msg and PSES_ENV_VAR in msg


def test_override_without_start_script_names_the_path(tmp_path):
    bogus = tmp_path / "nope"
    bogus.mkdir()
    with pytest.raises(FileNotFoundError, match=str(bogus)):
        find_pses(str(bogus), env={}, home=tmp_path)


def test_find_pwsh_override(tmp_path):
    exe = tmp_path / "pwsh"
    exe.write_text("")
    exe.chmod(0o755)
    assert find_pwsh(str(exe)) == str(exe)


def test_find_pwsh_missing_override_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="pwsh"):
        find_pwsh(str(tmp_path / "missing"))


def test_find_pwsh_uses_path(monkeypatch):
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/pwsh" if name == "pwsh" else None
    )
    assert find_pwsh(None) == "/usr/bin/pwsh"


def test_find_pwsh_not_on_path_hint(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(FileNotFoundError, match="aka.ms/powershell"):
        find_pwsh(None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_pses_locate.py -q --no-cov`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the locator**

`src/tdb/adapters/powershell/__init__.py`:

```python
"""tdb's PowerShell adapter: a DAP proxy in front of PowerShell Editor
Services (PSES). See server.py for the protocol translation and
locate.py for how pwsh and the PSES module are found."""
```

`src/tdb/adapters/powershell/locate.py`:

```python
"""Find the pwsh interpreter and the PSES module directory.

PSES precedence (spec "Language profile, registry, CLI"):
  1. {"adapters": {"pses": DIR}} from tdb's config (the `override` arg)
  2. $TDB_PSES_PATH
  3. the newest VS Code PowerShell extension's bundled copy
DIR may be the module directory itself (contains Start-EditorServices.ps1)
or the unzip root one level above it.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Mapping

PSES_ENV_VAR = "TDB_PSES_PATH"
PSES_RELEASE = "v4.7.0"
START_SCRIPT = "Start-EditorServices.ps1"
_MODULE_DIR = "PowerShellEditorServices"

PWSH_HINT = (
    "pwsh (PowerShell 7) not found on PATH — install it from "
    "https://aka.ms/powershell, or set "
    '{"adapters": {"pwsh": "/path/to/pwsh"}} in tdb\'s config.json'
)

PSES_HINT = (
    "PowerShell Editor Services (PSES) not found. Download "
    f"PowerShellEditorServices.zip from https://github.com/PowerShell/"
    f"PowerShellEditorServices/releases/tag/{PSES_RELEASE}, unzip it, and "
    "point tdb at the PowerShellEditorServices directory with "
    '{"adapters": {"pses": "/path/to/PowerShellEditorServices"}} in '
    f"config.json or the {PSES_ENV_VAR} environment variable. tdb also "
    "finds the copy bundled with the VS Code PowerShell extension "
    "(~/.vscode/extensions/ms-vscode.powershell-*/modules)"
)

_VSCODE_DIRS = (".vscode", ".vscode-insiders", ".vscode-server")
_EXT_RE = re.compile(r"^ms-vscode\.powershell-(?P<ver>[\d.]+)")


def find_pwsh(override: str | None) -> str:
    if override:
        if os.path.isfile(override):
            return override
        raise FileNotFoundError(f"pwsh not found at {override!r} — {PWSH_HINT}")
    found = shutil.which("pwsh")
    if found is None:
        raise FileNotFoundError(PWSH_HINT)
    return found


def _module_dir(candidate: Path) -> Path | None:
    """`candidate` or its PowerShellEditorServices child, if it holds the
    start script."""
    if (candidate / START_SCRIPT).is_file():
        return candidate
    nested = candidate / _MODULE_DIR
    if (nested / START_SCRIPT).is_file():
        return nested
    return None


def _version_key(name: str) -> tuple[int, ...]:
    m = _EXT_RE.match(name)
    if m is None:
        return ()
    return tuple(int(p) for p in m.group("ver").split(".") if p.isdigit())


def _vscode_candidates(home: Path) -> list[Path]:
    """Extension module dirs, newest extension version first."""
    found: list[tuple[tuple[int, ...], Path]] = []
    for vs in _VSCODE_DIRS:
        ext_root = home / vs / "extensions"
        if not ext_root.is_dir():
            continue
        for entry in ext_root.iterdir():
            key = _version_key(entry.name)
            if key:
                found.append((key, entry / "modules" / _MODULE_DIR))
    found.sort(key=lambda kv: kv[0], reverse=True)
    return [p for _, p in found]


def find_pses(
    override: str | None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    if override:
        d = _module_dir(Path(override))
        if d is None:
            raise FileNotFoundError(
                f"{START_SCRIPT} not found under {override!r} — {PSES_HINT}"
            )
        return d
    env_dir = env.get(PSES_ENV_VAR)
    if env_dir:
        d = _module_dir(Path(env_dir))
        if d is None:
            raise FileNotFoundError(
                f"{START_SCRIPT} not found under {PSES_ENV_VAR}={env_dir!r} — {PSES_HINT}"
            )
        return d
    for candidate in _vscode_candidates(home):
        d = _module_dir(candidate)
        if d is not None:
            return d
    raise FileNotFoundError(PSES_HINT)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_pses_locate.py -q --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/adapters/powershell/__init__.py src/tdb/adapters/powershell/locate.py tests/unit/test_pses_locate.py
git commit -m "PowerShell: locate pwsh and the PSES module"
```

---

### Task 4: Launcher script, arg quoting, stdout classifier

**Files:**
- Create: `src/tdb/adapters/powershell/tdb_launch.ps1`, `src/tdb/adapters/powershell/output.py`
- Modify: `pyproject.toml` (package data, only if `.ps1` files under `src/tdb` are not already shipped — check with `grep -n "package-data\|include" pyproject.toml`; the perl/bash adapters ship non-Python files, follow whatever they did)
- Test: `tests/unit/test_powershell_output.py`

**Interfaces:**
- Produces: `quote_ps_arg(s: str) -> str`; `EXIT_SENTINEL_PREFIX = "\x1etdb-exit:"`; `parse_exit_sentinel(line: str) -> int | None`; `class OutputClassifier` with `classify(self, line: str) -> str | None` returning `"stdout"`, `"stderr"`, or `None` (drop); `LAUNCHER: Path` constant in `output.py` pointing at `tdb_launch.ps1`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_powershell_output.py`:

```python
import shutil
import subprocess

import pytest

from tdb.adapters.powershell.output import (
    EXIT_SENTINEL_PREFIX,
    LAUNCHER,
    OutputClassifier,
    parse_exit_sentinel,
    quote_ps_arg,
)


def test_quote_plain():
    assert quote_ps_arg("abc") == "'abc'"


def test_quote_space_and_apostrophe():
    assert quote_ps_arg("it's here") == "'it''s here'"


def test_quote_empty():
    assert quote_ps_arg("") == "''"


def test_sentinel_parses():
    assert parse_exit_sentinel(f"{EXIT_SENTINEL_PREFIX}7\n") == 7
    assert parse_exit_sentinel(f"{EXIT_SENTINEL_PREFIX}0") == 0


def test_sentinel_rejects_other_lines():
    assert parse_exit_sentinel("tdb-exit:7") is None  # no \x1e
    assert parse_exit_sentinel("hello") is None
    assert parse_exit_sentinel(f"{EXIT_SENTINEL_PREFIX}x") is None


def test_classifier_drops_first_prompt_echo_only():
    c = OutputClassifier()
    assert c.classify("PS /tmp/w> . '/x/tdb_launch.ps1' '/x/s.ps1' 'a'\n") is None
    assert c.classify("PS /tmp/w> . '/x/tdb_launch.ps1' '/x/s.ps1'\n") == "stdout"


def test_classifier_plain_lines_are_stdout():
    c = OutputClassifier()
    assert c.classify("hello\n") == "stdout"
    assert c.classify("Write-Error: not fatal\n") == "stdout"


def test_classifier_tags_error_block_as_stderr_until_it_ends():
    c = OutputClassifier()
    assert c.classify("before\n") == "stdout"
    assert c.classify("Exception: /tmp/w/e1.ps1:2\n") == "stderr"
    assert c.classify("Line |\n") == "stderr"
    assert c.classify('   2 |  function Inner { throw "kaboom" }\n') == "stderr"
    assert c.classify("     |                   ~~~~~~~~~~~~~~\n") == "stderr"
    assert c.classify("     | kaboom\n") == "stderr"
    assert c.classify("after\n") == "stdout"


def test_classifier_cmdlet_header():
    c = OutputClassifier()
    assert c.classify("Get-Item: /tmp/w/e4.ps1:1\n") == "stderr"


def test_classifier_ansi_header_still_detected():
    c = OutputClassifier()
    assert (
        c.classify("\x1b[31;1mException: \x1b[0m/tmp/w/e1.ps1:2\x1b[0m\n") == "stderr"
    )


def test_launcher_exists():
    assert LAUNCHER.is_file()
    assert LAUNCHER.name == "tdb_launch.ps1"


pwsh = shutil.which("pwsh")


@pytest.mark.skipif(pwsh is None, reason="pwsh not installed")
def test_launcher_reports_exit_code_and_args(tmp_path):
    s = tmp_path / "s.ps1"
    s.write_text('Write-Host ($args -join "|")\nexit 7\n')
    cp = subprocess.run(
        [pwsh, "-NoProfile", "-File", str(LAUNCHER), str(s), "one two", "it's"],
        capture_output=True,
        text=True,
        env={"NO_COLOR": "1", "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    assert "one two|it's" in cp.stdout
    assert f"{EXIT_SENTINEL_PREFIX}7" in cp.stdout


@pytest.mark.skipif(pwsh is None, reason="pwsh not installed")
def test_launcher_no_sentinel_on_throw(tmp_path):
    s = tmp_path / "t.ps1"
    s.write_text('throw "kaboom"\n')
    cp = subprocess.run(
        [pwsh, "-NoProfile", "-File", str(LAUNCHER), str(s)],
        capture_output=True,
        text=True,
        env={"NO_COLOR": "1", "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    assert EXIT_SENTINEL_PREFIX not in cp.stdout
    assert f"Exception: {s}:1" in cp.stdout + cp.stderr
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_powershell_output.py -q --no-cov`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the launcher and output helpers**

`src/tdb/adapters/powershell/tdb_launch.ps1`:

```powershell
# tdb's PowerShell launcher. PSES dot-sources THIS file; it runs the
# user's script with the call operator so `exit N` inside the script
# returns here with $LASTEXITCODE = N, then prints an exit sentinel the
# proxy turns into a DAP `exited` event (PSES never sends one). An
# uncaught terminating error propagates through `&` and skips the
# sentinel: the proxy reports exit code 1 in that case.
param(
    [Parameter(Mandatory, Position = 0)][string]$Script,
    [Parameter(ValueFromRemainingArguments)][string[]]$ScriptArgs = @()
)
$global:LASTEXITCODE = 0
& $Script @ScriptArgs
$code = if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE }
Write-Host "`u{1E}tdb-exit:$code"
```

`src/tdb/adapters/powershell/output.py`:

```python
"""Pure helpers for the PowerShell proxy: arg quoting, the exit-code
sentinel printed by tdb_launch.ps1, and classification of pwsh's stdout
lines (prompt echo to drop, ConciseView error blocks to tag as stderr
so tdb's fatal-error modal can see them)."""

from __future__ import annotations

import re
from pathlib import Path

LAUNCHER = Path(__file__).with_name("tdb_launch.ps1")

# \x1e (record separator) keeps the sentinel from colliding with any
# plausible script output. Must match tdb_launch.ps1's Write-Host.
EXIT_SENTINEL_PREFIX = "\x1etdb-exit:"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# The fake prompt PSES's temporary console echoes when the script starts:
#   PS /cwd> . '/path/tdb_launch.ps1' '/path/script.ps1' 'arg'
_PROMPT_ECHO_RE = re.compile(r"^PS .*> \. '")
# ConciseView header: "Exception: /p/s.ps1:2", "Get-Item: /p/s.ps1:1"
_ERROR_HEAD_RE = re.compile(r"^[A-Za-z][\w.-]*: .+?:\d+\s*$")
# Rows that belong to the block that follows a header.
_ERROR_CONT_RE = re.compile(r"^\s*(?:Line \||\d+ \||\|)")


def quote_ps_arg(s: str) -> str:
    """PowerShell single-quoted literal (the only escape is '' for ')."""
    return "'" + s.replace("'", "''") + "'"


def parse_exit_sentinel(line: str) -> int | None:
    text = line.rstrip("\r\n")
    if not text.startswith(EXIT_SENTINEL_PREFIX):
        return None
    tail = text[len(EXIT_SENTINEL_PREFIX) :]
    try:
        return int(tail)
    except ValueError:
        return None


class OutputClassifier:
    """Stateful per-line classifier for pwsh's stdout.

    classify() returns the DAP output category for the line, or None to
    drop it. State: the first prompt echo is dropped once; a ConciseView
    header opens an error block that stays "stderr" while continuation
    rows keep coming.
    """

    def __init__(self) -> None:
        self._prompt_seen = False
        self._in_error = False

    def classify(self, line: str) -> str | None:
        text = _ANSI_RE.sub("", line).rstrip("\r\n")
        if not self._prompt_seen and _PROMPT_ECHO_RE.match(text):
            self._prompt_seen = True
            return None
        if _ERROR_HEAD_RE.match(text):
            self._in_error = True
            return "stderr"
        if self._in_error:
            if _ERROR_CONT_RE.match(text):
                return "stderr"
            self._in_error = False
        return "stdout"
```

Packaging: run `uv run python -c "import tdb.adapters.powershell.output as o; print(o.LAUNCHER.is_file())"` — True in the dev checkout. Then check `pyproject.toml`: if there is a `[tool.setuptools.package-data]` (or hatch `include`) entry listing e.g. `*.pl`/`*.sh` for the perl/bash adapters, add `"*.ps1"` beside it; if the build backend includes all files under `src/tdb` by default, no change. Verify with `uv build && unzip -l dist/*.whl | grep tdb_launch` and delete `dist/` afterwards (it is not tracked).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_powershell_output.py -q --no-cov`
Expected: PASS (the two pwsh-backed tests run only when pwsh is installed; on this machine it is).

- [ ] **Step 5: Commit**

```bash
git add src/tdb/adapters/powershell/tdb_launch.ps1 src/tdb/adapters/powershell/output.py tests/unit/test_powershell_output.py pyproject.toml
git commit -m "PowerShell: launcher script, arg quoting, stdout classifier"
```

---

### Task 5: Generic seq translator

**Files:**
- Create: `src/tdb/adapters/seqs.py`
- Test: `tests/unit/test_seqs.py`

**Interfaces:**
- Produces: `SeqTranslator` with `next_client_seq() -> int`, `next_upstream_seq() -> int`, `client_request_to_upstream(msg) -> dict`, `upstream_response_to_client(msg) -> dict | None`, `upstream_event_to_client(msg) -> dict`, `upstream_request_to_client(msg) -> dict`, `client_response_to_upstream(msg) -> dict | None`. Same semantics as `tdb.adapters.ruby.server.SeqTranslator` with "rdbg" renamed "upstream".

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_seqs.py`:

```python
from tdb.adapters.seqs import SeqTranslator


def test_client_request_roundtrip():
    t = SeqTranslator()
    fwd = t.client_request_to_upstream(
        {"seq": 41, "type": "request", "command": "next"}
    )
    assert fwd["seq"] == 1 and fwd["command"] == "next"
    resp = t.upstream_response_to_client(
        {
            "seq": 9,
            "type": "response",
            "request_seq": 1,
            "command": "next",
            "success": True,
        }
    )
    assert resp["request_seq"] == 41 and resp["seq"] == 1


def test_proxy_originated_response_is_swallowed():
    t = SeqTranslator()
    assert (
        t.upstream_response_to_client(
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
    e1 = t.upstream_event_to_client({"seq": 50, "type": "event", "event": "output"})
    e2 = t.upstream_event_to_client({"seq": 51, "type": "event", "event": "stopped"})
    assert (e1["seq"], e2["seq"]) == (1, 2)


def test_reverse_request_roundtrip():
    t = SeqTranslator()
    fwd = t.upstream_request_to_client(
        {"seq": 7, "type": "request", "command": "runInTerminal"}
    )
    back = t.client_response_to_upstream(
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
        t.client_response_to_upstream(
            {
                "seq": 3,
                "type": "response",
                "request_seq": 12,
                "command": "x",
                "success": True,
            }
        )
        is None
    )


def test_inputs_are_not_mutated():
    t = SeqTranslator()
    msg = {"seq": 5, "type": "request", "command": "threads"}
    t.client_request_to_upstream(msg)
    assert msg["seq"] == 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_seqs.py -q --no-cov`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`src/tdb/adapters/seqs.py`:

```python
"""Seq renumbering between the two sides of a DAP proxy.

Each side sees a gapless seq space owned by the proxy. A forwarded
request remembers the originator's seq so the answering side's response
can be restamped with it; responses to requests the proxy itself
originated have no mapping and translate to None (the proxy swallows or
routes those itself). Generic twin of the ruby proxy's SeqTranslator
("rdbg" -> "upstream"); ruby keeps its own copy for now.
"""

from __future__ import annotations


class SeqTranslator:
    def __init__(self) -> None:
        self._client_seq = 0
        self._upstream_seq = 0
        self._from_client: dict[int, int] = {}  # upstream seq -> client seq
        self._from_upstream: dict[int, int] = {}  # client seq -> upstream seq

    def next_client_seq(self) -> int:
        self._client_seq += 1
        return self._client_seq

    def next_upstream_seq(self) -> int:
        self._upstream_seq += 1
        return self._upstream_seq

    def client_request_to_upstream(self, msg: dict) -> dict:
        out = dict(msg)
        out["seq"] = self.next_upstream_seq()
        self._from_client[out["seq"]] = msg["seq"]
        return out

    def upstream_response_to_client(self, msg: dict) -> dict | None:
        orig = self._from_client.pop(msg.get("request_seq", -1), None)
        if orig is None:
            return None
        out = dict(msg)
        out["seq"] = self.next_client_seq()
        out["request_seq"] = orig
        return out

    def upstream_event_to_client(self, msg: dict) -> dict:
        out = dict(msg)
        out["seq"] = self.next_client_seq()
        return out

    def upstream_request_to_client(self, msg: dict) -> dict:
        out = dict(msg)
        out["seq"] = self.next_client_seq()
        self._from_upstream[out["seq"]] = msg["seq"]
        return out

    def client_response_to_upstream(self, msg: dict) -> dict | None:
        orig = self._from_upstream.pop(msg.get("request_seq", -1), None)
        if orig is None:
            return None
        out = dict(msg)
        out["seq"] = self.next_upstream_seq()
        out["request_seq"] = orig
        return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_seqs.py -q --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/adapters/seqs.py tests/unit/test_seqs.py
git commit -m "adapters: generic SeqTranslator for DAP proxies"
```

---

### Task 6: Fake PSES for proxy unit tests

**Files:**
- Create: `tests/unit/fake_pses.py`
- Test: (used by Task 7/8 tests; this task only proves the fake runs)

**Interfaces:**
- Produces: `python tests/unit/fake_pses.py <pwsh-style argv>` — parses `-SessionDetailsPath P` and `-DebugServicePipeName N` from argv, serves DAP on AF_UNIX socket `<dir of P>/sock`, writes `P` as `{"status":"started","debugServiceTransport":"NamedPipe","debugServicePipeName":"<dir of P>/sock","powerShellVersion":"7.6.5"}`. Behaviour keyed by env `FAKE_PSES_MODE`: `""` (normal), `"throw"`, `"no-session-file"`, `"die"`, `"old-version"`. Prints scripted stdout. Also records every request it received as JSON lines to `$FAKE_PSES_LOG` when set.
- Produces (fixture helper): `make_fake_pwsh(tmp_path: Path) -> tuple[str, Path]` in the same module: writes an `sh` shim named `pwsh` that execs `sys.executable fake_pses.py "$@"`, plus a fake PSES dir with an empty `Start-EditorServices.ps1`; returns `(shim_path, pses_dir)`.

- [ ] **Step 1: Write the fake**

`tests/unit/fake_pses.py`:

```python
"""A stand-in for `pwsh Start-EditorServices.ps1 ... -DebugServiceOnly
-DebugServicePipeName N`, for unit-testing the PowerShell proxy without
pwsh or PSES installed (POSIX only: the proxy spawns it as `pwsh`).

Speaks just enough DAP to exercise the proxy's rewrites:
  initialize        -> PSES's real capability list
  launch            -> success, then `initialized`; records arguments
  setBreakpoints    -> echoes each breakpoint as verified; records lists
  configurationDone -> prints the prompt echo + "hello from fake" on
                       stdout, then stops (reason "breakpoint") if any
                       breakpoint was ever set, else finishes the script
  continue          -> prints "after continue", exit sentinel 3, `terminated`
  next / stepIn / stepOut -> `stopped` reason "step"
  pause             -> `stopped` reason "step" (PSES really does this)
  evaluate          -> result "ctx=<context>:<expression>"
  stackTrace        -> one <Breakpoint> label frame (+ one real frame
                       when `levels` > 1)
  threads / scopes / variables -> minimal canned bodies
  disconnect        -> success; the process exits 0 shortly after
  terminate         -> "Method not found - terminate" (as PSES)

FAKE_PSES_MODE:
  "throw"           -> configurationDone prints a ConciseView block and
                       sends `terminated` without a sentinel
  "no-session-file" -> never writes the session file (proxy must time out)
  "die"             -> prints "boom: bad module" and exits 3 at once
  "old-version"     -> session file says powerShellVersion "5.1.0"
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

CAPS = {
    "supportsConfigurationDoneRequest": True,
    "supportsFunctionBreakpoints": True,
    "supportsConditionalBreakpoints": True,
    "supportsHitConditionalBreakpoints": True,
    "supportsSetVariable": True,
    "supportsDelayedStackTraceLoading": True,
    "supportsLogPoints": True,
    "supportsCancelRequest": True,
}

PROMPT = "PS /tmp/fake> . '/x/tdb_launch.ps1' '/x/s.ps1'"
SENTINEL = "\x1etdb-exit:3"
ERROR_BLOCK = [
    "Exception: /x/s.ps1:2",
    "Line |",
    '   2 |  throw "kaboom"',
    "     |  ~~~~~~~~~~~~~~",
    "     | kaboom",
]


def _arg(argv: list[str], flag: str) -> str | None:
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
    return None


def _out(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


class Fake:
    def __init__(self, log_path: str | None) -> None:
        self.seq = 0
        self.bps_seen = False
        self.log = open(log_path, "a") if log_path else None
        self.script_path = "/x/s.ps1"

    def record(self, msg: dict) -> None:
        if self.log:
            self.log.write(json.dumps(msg) + "\n")
            self.log.flush()

    def _send(self, w: asyncio.StreamWriter, msg: dict) -> None:
        self.seq += 1
        msg["seq"] = self.seq
        body = json.dumps(msg).encode()
        w.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)

    def resp(self, w, req, body=None, ok=True, message=None):
        m = {
            "type": "response",
            "request_seq": req["seq"],
            "command": req["command"],
            "success": ok,
        }
        if body is not None:
            m["body"] = body
        if message:
            m["message"] = message
        self._send(w, m)

    def event(self, w, name, body=None):
        m = {"type": "event", "event": name}
        if body is not None:
            m["body"] = body
        self._send(w, m)

    def stopped(self, w, reason, line):
        self.line = line
        self.event(
            w, "stopped", {"reason": reason, "threadId": 1, "allThreadsStopped": True}
        )

    async def handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        mode = os.environ.get("FAKE_PSES_MODE", "")
        self.line = 0
        while True:
            header = b""
            while not header.endswith(b"\r\n\r\n"):
                chunk = await r.read(1)
                if not chunk:
                    return
                header += chunk
            n = int(header.split(b":")[1])
            req = json.loads(await r.readexactly(n))
            self.record(req)
            if req.get("type") != "request":
                continue
            cmd = req["command"]
            args = req.get("arguments") or {}
            if cmd == "initialize":
                self.resp(w, req, CAPS)
            elif cmd == "launch":
                self.resp(w, req, {})
                self.event(w, "initialized", {})
            elif cmd == "setBreakpoints":
                bps = args.get("breakpoints") or []
                self.bps_seen = self.bps_seen or bool(bps)
                self.resp(
                    w,
                    req,
                    {
                        "breakpoints": [
                            {
                                "id": i,
                                "verified": True,
                                "line": b["line"],
                                "source": args.get("source"),
                            }
                            for i, b in enumerate(bps)
                        ]
                    },
                )
            elif cmd == "configurationDone":
                self.resp(w, req, {})
                await w.drain()
                _out(PROMPT)
                _out("hello from fake")
                if mode == "throw":
                    for ln in ERROR_BLOCK:
                        _out(ln)
                    self.event(w, "terminated")
                elif self.bps_seen:
                    self.stopped(w, "breakpoint", 6)
                else:
                    _out(SENTINEL)
                    self.event(w, "terminated")
            elif cmd == "continue":
                self.resp(w, req, {})
                await w.drain()
                _out("after continue")
                _out(SENTINEL)
                self.event(w, "terminated")
            elif cmd in ("next", "stepIn", "stepOut", "pause"):
                self.resp(w, req, {})
                self.stopped(w, "step", self.line + 1)
            elif cmd == "evaluate":
                self.resp(
                    w,
                    req,
                    {
                        "result": f"ctx={args.get('context')}:{args.get('expression')}",
                        "variablesReference": 0,
                    },
                )
            elif cmd == "stackTrace":
                frames = [
                    {
                        "id": 0,
                        "name": "<Breakpoint>",
                        "presentationHint": "label",
                        "source": {"path": self.script_path},
                        "line": self.line,
                        "column": 1,
                    }
                ]
                if int(args.get("levels") or 1) > 1:
                    frames.append(
                        {
                            "id": 1,
                            "name": "<ScriptBlock>",
                            "source": {"path": self.script_path},
                            "line": self.line,
                            "column": 0,
                        }
                    )
                self.resp(w, req, {"stackFrames": frames, "totalFrames": len(frames)})
            elif cmd == "threads":
                self.resp(
                    w,
                    req,
                    {"threads": [{"id": 1, "name": "PowerShell Pipeline Thread"}]},
                )
            elif cmd == "scopes":
                self.resp(
                    w,
                    req,
                    {
                        "scopes": [
                            {
                                "name": "Local",
                                "variablesReference": 75,
                                "expensive": False,
                            }
                        ]
                    },
                )
            elif cmd == "variables":
                self.resp(
                    w,
                    req,
                    {
                        "variables": [
                            {"name": "$x", "value": "1", "variablesReference": 0}
                        ]
                    },
                )
            elif cmd == "disconnect":
                self.resp(w, req, {})
                await w.drain()
                await asyncio.sleep(0.1)
                os._exit(0)
            elif cmd == "terminate":
                self.resp(
                    w, req, None, ok=False, message="Method not found - terminate"
                )
            else:
                self.resp(w, req, {})
            await w.drain()


async def main(argv: list[str]) -> int:
    mode = os.environ.get("FAKE_PSES_MODE", "")
    if mode == "die":
        _out("boom: bad module")
        return 3
    session = _arg(argv, "-SessionDetailsPath")
    assert session, "fake_pses: -SessionDetailsPath missing"
    sock = str(Path(session).parent / "sock")
    fake = Fake(os.environ.get("FAKE_PSES_LOG"))
    server = await asyncio.start_unix_server(fake.handle, path=sock)
    if mode != "no-session-file":
        version = "5.1.0" if mode == "old-version" else "7.6.5"
        Path(session).write_text(
            json.dumps(
                {
                    "status": "started",
                    "debugServiceTransport": "NamedPipe",
                    "debugServicePipeName": sock,
                    "powerShellVersion": version,
                }
            )
        )
    async with server:
        await server.serve_forever()
    return 0


def make_fake_pwsh(tmp_path: Path) -> tuple[str, Path]:
    """Write a `pwsh` sh-shim that runs this module, plus a stub PSES dir."""
    shim = tmp_path / "pwsh"
    shim.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{Path(__file__).resolve()}" "$@"\n'
    )
    shim.chmod(0o755)
    pses = tmp_path / "PowerShellEditorServices"
    pses.mkdir()
    (pses / "Start-EditorServices.ps1").write_text("# stub\n")
    return str(shim), pses


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
```

- [ ] **Step 2: Smoke-test the fake by hand**

Run:

```bash
cd /tmp && mkdir -p fk && uv run --project /home/al/projects/tdbg/work python /home/al/projects/tdbg/work/tests/unit/fake_pses.py -SessionDetailsPath /tmp/fk/session.json -DebugServicePipeName x &
sleep 1; cat /tmp/fk/session.json; kill %1; rm -rf /tmp/fk
```

Expected: the session JSON prints with `"status": "started"` and a `sock` path.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/fake_pses.py
git commit -m "PowerShell: fake PSES for proxy unit tests"
```

---

### Task 7: Proxy core — spawn, connect, forward, pump, teardown

**Files:**
- Create: `src/tdb/adapters/powershell/server.py`, `src/tdb/adapters/powershell/__main__.py`
- Test: `tests/unit/test_powershell_proxy.py`

**Interfaces:**
- Consumes: `find_pwsh`, `find_pses` (Task 3); `quote_ps_arg`, `parse_exit_sentinel`, `OutputClassifier`, `LAUNCHER` (Task 4); `SeqTranslator` (Task 5); `tdb.dap.protocol.read_message/encode_message`; `tdb._timeouts.ADAPTER_LISTEN`.
- Produces: `CAPABILITIES: dict`, `MIN_PWSH = (7, 0)`, `build_pwsh_command(pwsh: str, pses_dir: Path, session_file: Path, log_dir: Path, pipe_name: str) -> list[str]`, `async connect_debug_service(details: dict) -> tuple[StreamReader, StreamWriter]`, `class PowerShellDapServer(reader, writer)` with `async run()`. Task 8 adds the rewrite hooks onto this class: `_entry_pending`, `_pause_pending`, `_on_setBreakpoints`, `_on_evaluate`, `_on_pause`, `_on_configurationDone`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_powershell_proxy.py` (first half; Task 8 appends more):

```python
"""Drive PowerShellDapServer end-to-end over real pipes against the
fake PSES (tests/unit/fake_pses.py). POSIX only."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from tdb.adapters.powershell.server import (
    CAPABILITIES,
    build_pwsh_command,
    connect_debug_service,
)
from tests.unit.fake_pses import make_fake_pwsh
from tests.integration.perl_adapter_harness import AdapterClient

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh shim")

FAKE_CAPS = (
    json.loads(
        Path(__file__)
        .with_name("fake_pses.py")
        .read_text()
        .split("CAPS = ")[1]
        .split("\n}\n")[0]
        + "\n}"
    )
    if False
    else None
)  # (not used; kept simple below)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    shim, pses = make_fake_pwsh(tmp_path)
    log = tmp_path / "requests.jsonl"
    monkeypatch.setenv("FAKE_PSES_LOG", str(log))
    monkeypatch.delenv("FAKE_PSES_MODE", raising=False)
    script = tmp_path / "s.ps1"
    script.write_text("Write-Host 1\n")
    return {
        "pwsh": shim,
        "pses": str(pses),
        "log": log,
        "script": str(script),
        "tmp": tmp_path,
    }


def requests_seen(log: Path, command: str) -> list[dict]:
    if not log.exists():
        return []
    return [
        json.loads(l)
        for l in log.read_text().splitlines()
        if json.loads(l).get("command") == command
    ]


async def start_proxy() -> AdapterClient:
    client = AdapterClient()
    await client.start(module="tdb.adapters.powershell")
    return client


def launch_args(fake, **extra) -> dict:
    return {
        "type": "powershell",
        "request": "launch",
        "program": fake["script"],
        "args": [],
        "cwd": str(fake["tmp"]),
        "stopOnEntry": False,
        "console": "internalConsole",
        "pwsh": fake["pwsh"],
        "pses": fake["pses"],
        **extra,
    }


async def test_initialize_is_answered_statically():
    client = await start_proxy()
    try:
        resp = await client.request("initialize", {"adapterID": "pses"})
        assert resp["success"] and resp["body"] == CAPABILITIES
        assert resp["body"]["supportsTerminateRequest"] is True
    finally:
        await client.stop()


async def test_launch_forwards_launcher_and_quoted_args(fake):
    client = await start_proxy()
    try:
        await client.request("initialize", {"adapterID": "pses"})
        fut = client.send("launch", launch_args(fake, args=["one two", "it's"]))
        await client.wait_event("initialized")
        await client.request("configurationDone")
        assert (await fut)["success"]
        exited = await client.wait_event("exited")
        assert exited["body"]["exitCode"] == 3  # from the fake's sentinel
        await client.wait_event("terminated")
        [launch] = requests_seen(fake["log"], "launch")
        a = launch["arguments"]
        assert a["script"].endswith("tdb_launch.ps1")
        assert a["args"] == [f"'{fake['script']}'", "'one two'", "'it''s'"]
        assert a["cwd"] == str(fake["tmp"])
        assert "stopOnEntry" not in a and "env" not in a
        [init] = requests_seen(fake["log"], "initialize")
        assert init["arguments"]["adapterID"] == "pses"
    finally:
        await client.stop()


async def test_stdout_becomes_output_events_without_prompt_or_sentinel(fake):
    client = await start_proxy()
    try:
        await client.request("initialize", {"adapterID": "pses"})
        fut = client.send("launch", launch_args(fake))
        await client.wait_event("initialized")
        await client.request("configurationDone")
        await fut
        await client.wait_event("terminated")
        outs = [e["body"] for e in client.events if e["event"] == "output"]
        text = "".join(o["output"] for o in outs)
        assert "hello from fake\n" in text
        assert "PS /tmp/fake>" not in text
        assert "tdb-exit" not in text
        assert all(o["category"] == "stdout" for o in outs)
    finally:
        await client.stop()


async def test_exited_precedes_terminated(fake):
    client = await start_proxy()
    try:
        await client.request("initialize", {"adapterID": "pses"})
        fut = client.send("launch", launch_args(fake))
        await client.wait_event("initialized")
        await client.request("configurationDone")
        await fut
        await client.wait_event("terminated")
        names = [e["event"] for e in client.events] + ["terminated"]
        # wait_event removed "terminated"; "exited" must have come earlier
        assert "exited" in names
    finally:
        await client.stop()


async def test_missing_pwsh_fails_launch_with_hint(fake):
    client = await start_proxy()
    try:
        await client.request("initialize", {"adapterID": "pses"})
        resp = await client.send("launch", launch_args(fake, pwsh="/nonexistent/pwsh"))
        assert resp["success"] is False
        assert "pwsh" in resp["message"]
    finally:
        await client.stop()


async def test_missing_pses_fails_launch_with_hint(fake):
    client = await start_proxy()
    try:
        await client.request("initialize", {"adapterID": "pses"})
        resp = await client.send("launch", launch_args(fake, pses="/nonexistent/pses"))
        assert resp["success"] is False
        assert "PowerShellEditorServices.zip" in resp["message"]
    finally:
        await client.stop()


async def test_missing_program_fails_launch(fake):
    client = await start_proxy()
    try:
        await client.request("initialize", {"adapterID": "pses"})
        resp = await client.send(
            "launch", launch_args(fake, program="/nonexistent/x.ps1")
        )
        assert resp["success"] is False and "not found" in resp["message"]
    finally:
        await client.stop()


async def test_pwsh_dying_early_surfaces_its_output(fake, monkeypatch):
    monkeypatch.setenv("FAKE_PSES_MODE", "die")
    client = await start_proxy()
    try:
        await client.request("initialize", {"adapterID": "pses"})
        resp = await client.send("launch", launch_args(fake))
        assert resp["success"] is False
        assert "boom: bad module" in resp["message"]
    finally:
        await client.stop()


async def test_session_file_timeout(fake, monkeypatch):
    monkeypatch.setenv("FAKE_PSES_MODE", "no-session-file")
    monkeypatch.setenv("TDB_PSES_SESSION_TIMEOUT", "1.0")
    client = await start_proxy()
    try:
        await client.request("initialize", {"adapterID": "pses"})
        resp = await client.send("launch", launch_args(fake))
        assert resp["success"] is False
        assert "session file" in resp["message"]
    finally:
        await client.stop()


async def test_old_powershell_is_refused(fake, monkeypatch):
    monkeypatch.setenv("FAKE_PSES_MODE", "old-version")
    client = await start_proxy()
    try:
        await client.request("initialize", {"adapterID": "pses"})
        resp = await client.send("launch", launch_args(fake))
        assert resp["success"] is False
        assert "5.1" in resp["message"] and "7" in resp["message"]
    finally:
        await client.stop()


async def test_disconnect_kills_pwsh(fake):
    client = await start_proxy()
    try:
        await client.request("initialize", {"adapterID": "pses"})
        fut = client.send("launch", launch_args(fake))
        await client.wait_event("initialized")
        await client.request(
            "setBreakpoints",
            {"source": {"path": fake["script"]}, "breakpoints": [{"line": 6}]},
        )
        await client.request("configurationDone")
        await fut
        await client.wait_event("stopped")
        resp = await client.request("disconnect")
        assert resp["success"]
        await client.proc.wait()  # proxy exits after disconnect
        # the fake pwsh must be gone: its pid was logged by the proxy on stderr? Simpler:
        # the sh shim exec'd python; assert no process still holds the session dir open.
        import subprocess, time

        for _ in range(30):
            out = subprocess.run(
                ["pgrep", "-f", "fake_pses.py"], capture_output=True, text=True
            ).stdout
            if not out.strip():
                break
            time.sleep(0.1)
        assert not out.strip(), f"fake pwsh survived disconnect: {out}"
    finally:
        await client.stop()


async def test_terminate_is_answered_locally_and_ends_session(fake):
    client = await start_proxy()
    try:
        await client.request("initialize", {"adapterID": "pses"})
        fut = client.send("launch", launch_args(fake))
        await client.wait_event("initialized")
        await client.request(
            "setBreakpoints",
            {"source": {"path": fake["script"]}, "breakpoints": [{"line": 6}]},
        )
        await client.request("configurationDone")
        await fut
        await client.wait_event("stopped")
        resp = await client.request("terminate")
        assert resp["success"]
        await client.wait_event("exited")
        await client.wait_event("terminated")
        assert not requests_seen(fake["log"], "terminate"), (
            "terminate must not reach PSES"
        )
    finally:
        await client.stop()


def test_build_pwsh_command(tmp_path):
    cmd = build_pwsh_command(
        "/bin/pwsh",
        tmp_path / "PSES",
        tmp_path / "s.json",
        tmp_path / "log",
        "tdb-pses-1",
    )
    assert cmd[:6] == [
        "/bin/pwsh",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(tmp_path / "PSES" / "Start-EditorServices.ps1"),
    ]
    assert "-DebugServiceOnly" in cmd
    assert cmd[cmd.index("-DebugServicePipeName") + 1] == "tdb-pses-1"
    assert cmd[cmd.index("-BundledModulesPath") + 1] == str(tmp_path)
    assert cmd[cmd.index("-LogLevel") + 1] == "None"
    assert cmd[cmd.index("-SessionDetailsPath") + 1] == str(tmp_path / "s.json")
    assert "-Stdio" not in cmd


async def test_connect_debug_service_posix(tmp_path):
    sock = str(tmp_path / "s")

    async def echo(r, w):
        w.write(await r.read(5))
        await w.drain()
        w.close()

    server = await asyncio.start_unix_server(echo, path=sock)
    async with server:
        r, w = await connect_debug_service({"debugServicePipeName": sock})
        w.write(b"hello")
        await w.drain()
        assert await r.read(5) == b"hello"
        w.close()


async def test_connect_debug_service_windows_branch_is_selected(monkeypatch):
    calls = []
    monkeypatch.setattr("tdb.adapters.powershell.server.sys.platform", "win32")

    async def fake_pipe(name):
        calls.append(name)
        return ("r", "w")

    monkeypatch.setattr(
        "tdb.adapters.powershell.server._connect_windows_pipe", fake_pipe
    )
    assert await connect_debug_service({"debugServicePipeName": r"\\.\pipe\tdb-x"}) == (
        "r",
        "w",
    )
    assert calls == [r"\\.\pipe\tdb-x"]
```

Delete the stray `FAKE_CAPS = ...` line before running (it is a leftover; the tests compare against `CAPABILITIES` from the server module).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_powershell_proxy.py -q --no-cov -x`
Expected: FAIL with `ModuleNotFoundError: tdb.adapters.powershell.server`.

- [ ] **Step 3: Write the proxy**

`src/tdb/adapters/powershell/__main__.py`:

```python
"""python -m tdb.adapters.powershell — run the PowerShell DAP proxy on stdio."""

import asyncio
import sys

from tdb.adapters.powershell.server import PowerShellDapServer


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
    await PowerShellDapServer(reader, writer).run()


if __name__ == "__main__":
    asyncio.run(main())
```

`src/tdb/adapters/powershell/server.py`:

```python
"""DAP proxy between tdb (stdio) and PowerShell Editor Services (PSES).

PSES — the DAP server inside the VS Code PowerShell extension — runs
in-process in pwsh and, in debug-only named-pipe mode, serves DAP on a
UNIX socket (Windows: a named pipe) whose path it announces in a
session-details JSON file. tdb expects a stdio adapter it can spawn.
This module bridges the two:

  tdb --stdio--> PowerShellDapServer --socket--> pwsh Start-EditorServices.ps1
                                     <--stdout-- (script output)

Store-and-forward pipe with seq renumbering (tdb.adapters.seqs). PSES
quirks handled here (all probe-verified, see the design spec):

  initialize        answered from static CAPABILITIES (PSES isn't up yet)
  launch            spawns pwsh, waits for the session file, connects,
                    then forwards a launch of tdb_launch.ps1 (which runs
                    the user's script with `&` and prints an exit-code
                    sentinel — PSES never sends `exited`); every user arg
                    is single-quoted because PSES joins args unquoted
  env               set on the pwsh process (PSES ignores the launch field)
  stopOnEntry       PSES ignores it: emulated with a line-1 breakpoint
                    on the user script (Task 8)
  pause             PSES reports the stop as "step": rewritten to "pause"
  evaluate          "repl" prints to stdout with an empty result: context
                    rewritten to "watch"
  terminate         unsupported by PSES: answered here by killing pwsh
  terminated        PSES sends it but pwsh lives on: the proxy kills pwsh,
                    then emits exited(code) + terminated in that order
  stdout            pumped into `output` events; the echoed prompt line
                    and the exit sentinel are dropped; ConciseView error
                    blocks are tagged "stderr" for the fatal-error modal
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable

from tdb import _timeouts
from tdb.adapters.powershell.locate import find_pses, find_pwsh
from tdb.adapters.powershell.output import (
    LAUNCHER,
    OutputClassifier,
    parse_exit_sentinel,
    quote_ps_arg,
)
from tdb.adapters.seqs import SeqTranslator
from tdb.dap.protocol import encode_message, read_message

log = logging.getLogger(__name__)

# What PSES 4.7 advertises, plus supportsTerminateRequest (the proxy
# implements terminate itself).
CAPABILITIES = {
    "supportsConfigurationDoneRequest": True,
    "supportsFunctionBreakpoints": True,
    "supportsConditionalBreakpoints": True,
    "supportsHitConditionalBreakpoints": True,
    "supportsSetVariable": True,
    "supportsDelayedStackTraceLoading": True,
    "supportsLogPoints": True,
    "supportsCancelRequest": True,
    "supportsTerminateRequest": True,
}

MIN_PWSH = (7, 0)
_SESSION_TIMEOUT_ENV = "TDB_PSES_SESSION_TIMEOUT"  # tests shorten the wait
_RESUME_COMMANDS = {"continue", "next", "stepIn", "stepOut"}


def build_pwsh_command(
    pwsh: str, pses_dir: Path, session_file: Path, log_dir: Path, pipe_name: str
) -> list[str]:
    from tdb import __version__

    return [
        pwsh,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(pses_dir / "Start-EditorServices.ps1"),
        "-HostName",
        "tdb",
        "-HostProfileId",
        "tdb",
        "-HostVersion",
        __version__,
        "-BundledModulesPath",
        str(pses_dir.parent),
        "-LogPath",
        str(log_dir),
        "-LogLevel",
        "None",
        "-SessionDetailsPath",
        str(session_file),
        "-DebugServiceOnly",
        "-DebugServicePipeName",
        pipe_name,
    ]


async def _connect_windows_pipe(
    name: str,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Windows named-pipe client. UNVERIFIED (no Windows CI yet)."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await loop.create_pipe_connection(lambda: protocol, name)  # type: ignore[attr-defined]
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    return reader, writer


async def connect_debug_service(
    details: dict,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """The one platform seam: UNIX socket on POSIX, named pipe on Windows."""
    name = details["debugServicePipeName"]
    if sys.platform == "win32":
        return await _connect_windows_pipe(name)
    return await asyncio.open_unix_connection(name)


def _parse_version(text: str | None) -> tuple[int, int]:
    try:
        major, minor = (text or "").split(".")[:2]
        return int(major), int(minor)
    except ValueError:
        return (0, 0)


class PowerShellDapServer:
    """Store-and-forward proxy; see module docstring."""

    def __init__(self, reader: asyncio.StreamReader, writer: Any) -> None:
        self._reader = reader
        self._writer = writer
        self._seqs = SeqTranslator()
        self._done = asyncio.Event()
        self._proc: asyncio.subprocess.Process | None = None
        self._up_writer: asyncio.StreamWriter | None = None
        self._workdir: str | None = None
        self._client_init_args: dict = {}
        self._launched = False
        self._sent_exited = False
        self._sent_terminated = False
        self._terminated_seen = False  # PSES said the script ended
        self._exit_code: int | None = None  # from the launcher's sentinel
        self._classifier = OutputClassifier()
        # Task 8 state
        self._stop_on_entry = False
        self._program = ""
        self._entry_pending = False
        self._entry_synthetic = False
        self._user_bps: list[dict] = []
        self._main_bps_sent = False
        self._pause_pending = False
        # proxy-originated upstream requests awaiting a reply
        self._proxy_requests: dict[int, asyncio.Future] = {}
        # strong refs (asyncio keeps only weak refs to bare tasks)
        self._tasks: set[asyncio.Future] = set()
        self._pump_tasks: list[asyncio.Future] = []
        self._watch_exit_task: asyncio.Future | None = None
        self.handlers: dict[str, Callable[[dict], Awaitable[None]]] = {}
        for name in dir(self):
            if name.startswith("_on_"):
                self.handlers[name[4:]] = getattr(self, name)

    # ---- plumbing ----
    def _write_client(self, msg: dict) -> None:
        self._writer.write(encode_message(msg))

    def _write_up(self, msg: dict) -> None:
        if self._up_writer is not None:
            self._up_writer.write(encode_message(msg))

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

    async def _up_request(
        self, command: str, arguments: dict, timeout: float = 5.0
    ) -> dict | None:
        """Proxy-originated request to PSES; awaits its reply."""
        if self._up_writer is None:
            return None
        seq = self._seqs.next_upstream_seq()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._proxy_requests[seq] = fut
        try:
            self._write_up(
                {
                    "seq": seq,
                    "type": "request",
                    "command": command,
                    "arguments": arguments,
                }
            )
            await self._up_writer.drain()
            return await asyncio.wait_for(fut, timeout)
        except (asyncio.TimeoutError, ConnectionError, OSError):
            return None
        finally:
            self._proxy_requests.pop(seq, None)

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
            await self._ensure_pwsh_dead()
            await self._await_watch_exit()
            self._cleanup_workdir()
            await self._writer.drain()

    async def _dispatch_client_message(self, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == "response":
            fwd = self._seqs.client_response_to_upstream(msg)
            if fwd is not None:
                self._write_up(fwd)
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
        if self._up_writer is None:
            self.send_error(msg, "no debug session")
            return
        self._write_up(self._seqs.client_request_to_upstream(msg))

    # ---- PSES socket side ----
    async def _pump_up(self, reader: asyncio.StreamReader) -> None:
        while True:
            try:
                msg = await read_message(reader)
            except (ConnectionError, asyncio.IncompleteReadError, EOFError, ValueError):
                return
            mtype = msg.get("type")
            if mtype == "event":
                if await self._note_and_filter_event(msg):
                    continue
                self._write_client(self._seqs.upstream_event_to_client(msg))
            elif mtype == "response":
                pending = self._proxy_requests.pop(msg.get("request_seq", -1), None)
                if pending is not None:
                    if not pending.done():
                        pending.set_result(msg)
                    continue
                out = self._seqs.upstream_response_to_client(msg)
                if out is None:
                    continue
                out = self._rewrite_response(out)
                self._write_client(out)
            elif mtype == "request":
                self._write_client(self._seqs.upstream_request_to_client(msg))
            await self._writer.drain()

    def _rewrite_response(self, msg: dict) -> dict:
        """Task 8 hook (setBreakpoints strip). Identity for now."""
        return msg

    async def _note_and_filter_event(self, msg: dict) -> bool:
        """Track session state; True -> swallow the event.
        Task 8 adds the stopped-reason rewrites here."""
        event = msg.get("event")
        if event == "terminated":
            # PSES: the script ended, but pwsh is still alive. Kill it;
            # _watch_exit then emits exited(code) + terminated in order,
            # after the stdout pump has drained (the sentinel arrives on
            # a different pipe than this event and may still be in flight).
            self._terminated_seen = True
            self._spawn_task(self._finish_session())
            return True
        if event == "exited":
            return True  # never observed from PSES; _watch_exit owns it
        return False

    async def _finish_session(self) -> None:
        await self._up_request("disconnect", {}, timeout=2.0)
        await self._ensure_pwsh_dead()

    async def _pump_stdout(self, stream: asyncio.StreamReader) -> None:
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace")
            code = parse_exit_sentinel(text)
            if code is not None:
                self._exit_code = code
                continue
            category = self._classifier.classify(text)
            if category is None:
                continue
            self.send_event("output", {"category": category, "output": text})
            await self._writer.drain()

    async def _watch_exit(self) -> None:
        assert self._proc is not None
        rc = await self._proc.wait()
        if self._pump_tasks:
            await asyncio.wait(self._pump_tasks, timeout=2.0)
        if not self._launched:
            return
        if self._exit_code is not None:
            code = self._exit_code
        elif self._terminated_seen:
            code = 1  # script ended without reaching the launcher's sentinel
        else:
            code = rc
        if not self._sent_exited:
            self._sent_exited = True
            self.send_event("exited", {"exitCode": code})
        if not self._sent_terminated:
            self._sent_terminated = True
            self.send_event("terminated")
        await self._writer.drain()

    # ---- lifecycle handlers ----
    async def _on_initialize(self, request: dict) -> None:
        self._client_init_args = dict(request.get("arguments") or {})
        self.send_response(request, CAPABILITIES)

    async def _on_launch(self, request: dict) -> None:
        args = request.get("arguments") or {}
        program = os.path.abspath(args.get("program", ""))
        if not os.path.isfile(program):
            self.send_error(request, f"program not found: {program}")
            return
        try:
            pwsh = find_pwsh(args.get("pwsh"))
            pses_dir = find_pses(args.get("pses"))
        except FileNotFoundError as e:
            self.send_error(request, str(e))
            return
        self._program = program
        self._stop_on_entry = bool(args.get("stopOnEntry", False))
        cwd = args.get("cwd") or os.getcwd()
        env = {**os.environ, **(args.get("env") or {}), "NO_COLOR": "1", "TERM": "dumb"}

        self._workdir = tempfile.mkdtemp(prefix="tdb-pses-")
        session_file = Path(self._workdir) / "session.json"
        log_dir = Path(self._workdir) / "log"
        log_dir.mkdir()
        pipe_name = f"tdb-pses-{os.getpid()}-{secrets.token_hex(4)}"
        cmd = build_pwsh_command(pwsh, pses_dir, session_file, log_dir, pipe_name)

        popen_kwargs: dict[str, Any] = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                **popen_kwargs,
            )
        except OSError as e:
            self.send_error(request, f"cannot start {pwsh}: {e}")
            return
        assert self._proc.stdout is not None
        early: list[str] = []
        try:
            details = await self._await_session_file(session_file, early)
            version = _parse_version(details.get("powerShellVersion"))
            if version < MIN_PWSH:
                raise RuntimeError(
                    f"PowerShell {details.get('powerShellVersion')} is too old — "
                    f"tdb needs pwsh >= {MIN_PWSH[0]}.{MIN_PWSH[1]}"
                )
            reader, writer = await self._connect_with_retry(details)
        except Exception as e:
            await self._ensure_pwsh_dead()
            tail = await self._drain_early_stdout(early)
            self.send_error(request, f"{e}\n{tail}".strip())
            await self._writer.drain()
            return
        self._up_writer = writer
        self._launched = True
        self._entry_pending = self._stop_on_entry
        up_pump = self._spawn_task(self._pump_up(reader))
        self._pump_tasks = [
            self._spawn_task(self._pump_stdout(self._proc.stdout)),
            up_pump,
        ]
        # Any prompt-echo/early lines read while waiting for the session
        # file were already consumed; replay them through the classifier.
        for line in early:
            category = self._classifier.classify(line)
            if category is not None:
                self.send_event("output", {"category": category, "output": line})
        self._watch_exit_task = self._spawn_task(self._watch_exit())
        # PSES needs its own initialize first (proxy-originated; its
        # response is swallowed). Then the client's launch, rewritten.
        self._write_up(
            {
                "seq": self._seqs.next_upstream_seq(),
                "type": "request",
                "command": "initialize",
                "arguments": dict(self._client_init_args),
            }
        )
        fwd = self._seqs.client_request_to_upstream(request)
        fwd["arguments"] = {
            "script": str(LAUNCHER),
            "args": [
                quote_ps_arg(program),
                *(quote_ps_arg(str(a)) for a in args.get("args") or []),
            ],
            "cwd": cwd,
        }
        self._write_up(fwd)
        await self._writer.drain()

    async def _await_session_file(self, session_file: Path, early: list[str]) -> dict:
        """Poll for PSES's session file while draining pwsh's stdout into
        `early` (so a dying pwsh's message is available for the error)."""
        assert self._proc is not None and self._proc.stdout is not None
        timeout = float(os.environ.get(_SESSION_TIMEOUT_ENV, _timeouts.ADAPTER_LISTEN))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        stdout = self._proc.stdout
        while True:
            if session_file.exists():
                try:
                    details = json.loads(session_file.read_text())
                except (OSError, ValueError):
                    details = None
                if details and details.get("status") == "started":
                    return details
            if self._proc.returncode is not None:
                raise RuntimeError(
                    f"pwsh exited with code {self._proc.returncode} before PSES started"
                )
            if loop.time() > deadline:
                raise TimeoutError(
                    f"PSES did not write its session file within {timeout:.0f}s"
                )
            try:
                line = await asyncio.wait_for(stdout.readline(), 0.1)
                if line:
                    early.append(line.decode("utf-8", errors="replace"))
            except asyncio.TimeoutError:
                pass

    async def _connect_with_retry(self, details: dict, timeout: float = 10.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            try:
                return await connect_debug_service(details)
            except (ConnectionError, FileNotFoundError, OSError):
                if self._proc is not None and self._proc.returncode is not None:
                    raise RuntimeError(
                        f"pwsh exited with code {self._proc.returncode} before accepting a connection"
                    )
                if loop.time() > deadline:
                    raise TimeoutError("timed out connecting to PSES's debug socket")
                await asyncio.sleep(0.1)

    async def _drain_early_stdout(self, early: list[str]) -> str:
        if self._proc is not None and self._proc.stdout is not None:
            try:
                rest = await asyncio.wait_for(self._proc.stdout.read(), 0.5)
                early.append(rest.decode("utf-8", errors="replace"))
            except (asyncio.TimeoutError, OSError):
                pass
        return "".join(early)[-2000:].strip()

    # ---- teardown ----
    def _kill_group(self, sig_kill: bool = False) -> None:
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

    async def _ensure_pwsh_dead(self, grace: float = 2.0) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        self._kill_group()
        try:
            await asyncio.wait_for(proc.wait(), grace)
        except asyncio.TimeoutError:
            self._kill_group(sig_kill=True)
            await proc.wait()

    async def _await_watch_exit(self, timeout: float = 4.0) -> None:
        task = self._watch_exit_task
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(task, timeout)
        except asyncio.TimeoutError:
            log.warning("_watch_exit did not finish within %.1fs of teardown", timeout)
        except Exception:
            log.exception("_watch_exit failed during teardown")

    def _cleanup_workdir(self) -> None:
        if self._workdir:
            shutil.rmtree(self._workdir, ignore_errors=True)
            self._workdir = None

    async def _on_disconnect(self, request: dict) -> None:
        if self._up_writer is not None:
            await self._up_request("disconnect", {}, timeout=2.0)
        await self._ensure_pwsh_dead()
        self.send_response(request)
        self._done.set()

    async def _on_terminate(self, request: dict) -> None:
        if self._up_writer is not None:
            await self._up_request("disconnect", {}, timeout=2.0)
        await self._ensure_pwsh_dead()
        self.send_response(request)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_powershell_proxy.py -q --no-cov -x`
Expected: PASS. Likely wrinkles: (a) the fake's `os._exit(0)` on disconnect races the proxy's kill — both are fine; (b) `test_disconnect_kills_pwsh` uses `pgrep`, present on Linux/macOS; (c) `early` lines read during the session-file wait are replayed through the classifier, so the prompt-echo test still holds even if PSES printed it before the socket connected (it doesn't in practice, but the fake may).

- [ ] **Step 5: Commit**

```bash
git add src/tdb/adapters/powershell/server.py src/tdb/adapters/powershell/__main__.py tests/unit/test_powershell_proxy.py
git commit -m "PowerShell: DAP proxy core (spawn PSES, socket connect, stdout pump, teardown)"
```

---

### Task 8: Proxy rewrites — stopOnEntry, pause, evaluate, stderr tagging

**Files:**
- Modify: `src/tdb/adapters/powershell/server.py`
- Test: `tests/unit/test_powershell_proxy.py` (append)

**Interfaces:**
- Consumes: Task 7's `PowerShellDapServer` (`_entry_pending`, `_entry_synthetic`, `_user_bps`, `_main_bps_sent`, `_pause_pending`, `_rewrite_response`, `_note_and_filter_event`, `_up_request`).
- Produces: handlers `_on_setBreakpoints`, `_on_configurationDone`, `_on_pause`, `_on_evaluate`; stopped-reason rewrites.

- [ ] **Step 1: Append the failing tests**

Append to `tests/unit/test_powershell_proxy.py`:

```python
# ---- Task 8: rewrites -------------------------------------------------------


async def test_stop_on_entry_adds_and_strips_line1_breakpoint(fake):
    client = await start_proxy()
    try:
        await client.request("initialize", {"adapterID": "pses"})
        fut = client.send("launch", launch_args(fake, stopOnEntry=True))
        await client.wait_event("initialized")
        resp = await client.request(
            "setBreakpoints",
            {"source": {"path": fake["script"]}, "breakpoints": [{"line": 6}]},
        )
        # the client never sees the synthetic entry breakpoint
        assert [b["line"] for b in resp["body"]["breakpoints"]] == [6]
        await client.request("configurationDone")
        await fut
        ev = await client.wait_event("stopped")
        assert ev["body"]["reason"] == "entry"
        seen = requests_seen(fake["log"], "setBreakpoints")
        # 1st: user list + synthetic line 1; 2nd (after the entry stop): user list only
        assert [b["line"] for b in seen[0]["arguments"]["breakpoints"]] == [6, 1]
        assert [b["line"] for b in seen[-1]["arguments"]["breakpoints"]] == [6]
    finally:
        await client.stop()


async def test_stop_on_entry_without_user_breakpoints(fake):
    client = await start_proxy()
    try:
        await client.request("initialize", {"adapterID": "pses"})
        fut = client.send("launch", launch_args(fake, stopOnEntry=True))
        await client.wait_event("initialized")
        await client.request("configurationDone")
        await fut
        ev = await client.wait_event("stopped")
        assert ev["body"]["reason"] == "entry"
        seen = requests_seen(fake["log"], "setBreakpoints")
        assert [b["line"] for b in seen[0]["arguments"]["breakpoints"]] == [1]
        assert seen[-1]["arguments"]["breakpoints"] == []
    finally:
        await client.stop()


async def test_user_breakpoint_on_line1_is_not_duplicated(fake):
    client = await start_proxy()
    try:
        await client.request("initialize", {"adapterID": "pses"})
        fut = client.send("launch", launch_args(fake, stopOnEntry=True))
        await client.wait_event("initialized")
        resp = await client.request(
            "setBreakpoints",
            {"source": {"path": fake["script"]}, "breakpoints": [{"line": 1}]},
        )
        assert [b["line"] for b in resp["body"]["breakpoints"]] == [1]
        await client.request("configurationDone")
        await fut
        await client.wait_event("stopped")
        seen = requests_seen(fake["log"], "setBreakpoints")
        assert all(
            [b["line"] for b in s["arguments"]["breakpoints"]] == [1] for s in seen
        )
    finally:
        await client.stop()


async def test_breakpoints_in_other_files_pass_through(fake):
    client = await start_proxy()
    try:
        await client.request("initialize", {"adapterID": "pses"})
        fut = client.send("launch", launch_args(fake, stopOnEntry=True))
        await client.wait_event("initialized")
        resp = await client.request(
            "setBreakpoints",
            {"source": {"path": "/elsewhere/lib.ps1"}, "breakpoints": [{"line": 3}]},
        )
        assert [b["line"] for b in resp["body"]["breakpoints"]] == [3]
        await client.request("configurationDone")
        await fut
        await client.wait_event("stopped")
        other = [
            s
            for s in requests_seen(fake["log"], "setBreakpoints")
            if s["arguments"]["source"]["path"] == "/elsewhere/lib.ps1"
        ]
        assert [b["line"] for b in other[0]["arguments"]["breakpoints"]] == [3]
    finally:
        await client.stop()


async def test_no_stop_on_entry_sends_no_synthetic_breakpoint(fake):
    client = await start_proxy()
    try:
        await client.request("initialize", {"adapterID": "pses"})
        fut = client.send("launch", launch_args(fake, stopOnEntry=False))
        await client.wait_event("initialized")
        await client.request("configurationDone")
        await fut
        await client.wait_event("terminated")
        assert requests_seen(fake["log"], "setBreakpoints") == []
    finally:
        await client.stop()


async def _launch_to_breakpoint(client, fake):
    await client.request("initialize", {"adapterID": "pses"})
    fut = client.send("launch", launch_args(fake))
    await client.wait_event("initialized")
    await client.request(
        "setBreakpoints",
        {"source": {"path": fake["script"]}, "breakpoints": [{"line": 6}]},
    )
    await client.request("configurationDone")
    await fut
    ev = await client.wait_event("stopped")
    assert ev["body"]["reason"] == "breakpoint"


async def test_pause_stop_reason_is_rewritten(fake):
    client = await start_proxy()
    try:
        await _launch_to_breakpoint(client, fake)
        await client.request("pause", {"threadId": 1})
        ev = await client.wait_event("stopped")
        assert ev["body"]["reason"] == "pause"
        # a plain step afterwards keeps its own reason
        await client.request("next", {"threadId": 1})
        ev = await client.wait_event("stopped")
        assert ev["body"]["reason"] == "step"
    finally:
        await client.stop()


async def test_evaluate_repl_context_is_rewritten_to_watch(fake):
    client = await start_proxy()
    try:
        await _launch_to_breakpoint(client, fake)
        resp = await client.request("evaluate", {"expression": "$x", "context": "repl"})
        assert resp["body"]["result"] == "ctx=watch:$x"
        resp = await client.request(
            "evaluate", {"expression": "$x", "context": "hover"}
        )
        assert resp["body"]["result"] == "ctx=hover:$x"
        resp = await client.request("evaluate", {"expression": "$x"})
        assert resp["body"]["result"] == "ctx=watch:$x"
    finally:
        await client.stop()


async def test_error_block_is_tagged_stderr_and_exit_is_1(fake, monkeypatch):
    monkeypatch.setenv("FAKE_PSES_MODE", "throw")
    client = await start_proxy()
    try:
        await client.request("initialize", {"adapterID": "pses"})
        fut = client.send("launch", launch_args(fake))
        await client.wait_event("initialized")
        await client.request("configurationDone")
        await fut
        exited = await client.wait_event("exited")
        assert exited["body"]["exitCode"] == 1
        await client.wait_event("terminated")
        outs = [e["body"] for e in client.events if e["event"] == "output"]
        stderr = "".join(o["output"] for o in outs if o["category"] == "stderr")
        stdout = "".join(o["output"] for o in outs if o["category"] == "stdout")
        assert stderr.startswith("Exception: /x/s.ps1:2")
        assert "kaboom" in stderr
        assert "hello from fake" in stdout and "Exception" not in stdout
    finally:
        await client.stop()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_powershell_proxy.py -q --no-cov -k "entry or pause or evaluate or error_block or other_files or duplicated or synthetic"`
Expected: FAIL (entry reason is "breakpoint", evaluate result is "ctx=repl:$x", etc.).

- [ ] **Step 3: Implement the rewrites**

In `src/tdb/adapters/powershell/server.py`:

Replace `_rewrite_response` with:

```python
    def _rewrite_response(self, msg: dict) -> dict:
        """Strip the synthetic entry breakpoint from a setBreakpoints
        response for the main script (it was appended last)."""
        hook = self._response_hooks.pop(msg["request_seq"], None)
        return hook(msg) if hook else msg
```

Add to `__init__` (next to the Task 8 state): `self._response_hooks: dict[int, Callable[[dict], dict]] = {}` keyed by the **client-side** request seq.

Add handlers:

```python
# ---- rewrites ----
def _is_main_script(self, args: dict) -> bool:
    path = (args.get("source") or {}).get("path") or ""
    return bool(path) and os.path.abspath(path) == self._program


async def _on_setBreakpoints(self, request: dict) -> None:
    if self._up_writer is None:
        self.send_error(request, "no debug session")
        return
    args = dict(request.get("arguments") or {})
    if not (self._entry_pending and self._is_main_script(args)):
        self._write_up(self._seqs.client_request_to_upstream(request))
        return
    self._user_bps = list(args.get("breakpoints") or [])
    self._main_bps_sent = True
    bps = list(self._user_bps)
    self._entry_synthetic = not any(b.get("line") == 1 for b in bps)
    if self._entry_synthetic:
        bps.append({"line": 1})
        n_user = len(self._user_bps)

        def strip(msg: dict) -> dict:
            body = dict(msg.get("body") or {})
            body["breakpoints"] = list(body.get("breakpoints") or [])[:n_user]
            return {**msg, "body": body}

        self._response_hooks[request["seq"]] = strip
    args["breakpoints"] = bps
    self._write_up(
        self._seqs.client_request_to_upstream({**request, "arguments": args})
    )


async def _on_configurationDone(self, request: dict) -> None:
    if self._up_writer is None:
        self.send_error(request, "no debug session")
        return
    if self._entry_pending and not self._main_bps_sent:
        self._user_bps = []
        self._entry_synthetic = True
        self._main_bps_sent = True
        await self._up_request(
            "setBreakpoints",
            {"source": {"path": self._program}, "breakpoints": [{"line": 1}]},
        )
    self._write_up(self._seqs.client_request_to_upstream(request))


async def _on_pause(self, request: dict) -> None:
    if self._up_writer is None:
        self.send_error(request, "no debug session")
        return
    self._pause_pending = True
    self._write_up(self._seqs.client_request_to_upstream(request))


async def _on_evaluate(self, request: dict) -> None:
    if self._up_writer is None:
        self.send_error(request, "no debug session")
        return
    args = dict(request.get("arguments") or {})
    if args.get("context", "repl") == "repl":
        args["context"] = "watch"
    self._write_up(
        self._seqs.client_request_to_upstream({**request, "arguments": args})
    )
```

Extend `_note_and_filter_event` with a `stopped` branch **before** the `terminated` branch:

```python
        if event == "stopped":
            body = dict(msg.get("body") or {})
            if self._entry_pending:
                self._entry_pending = False
                body["reason"] = "entry"
                if self._entry_synthetic:
                    # drop the synthetic line-1 breakpoint before the
                    # client can observe it (e.g. via a later continue)
                    await self._up_request(
                        "setBreakpoints",
                        {"source": {"path": self._program}, "breakpoints": self._user_bps},
                    )
                self._pause_pending = False
            elif self._pause_pending:
                if body.get("reason") == "step":
                    body["reason"] = "pause"
                self._pause_pending = False
            msg["body"] = body
            return False
```

Also clear `_pause_pending` on client resume commands: in `_dispatch_client_message`, before the generic forward, add `if msg["command"] in _RESUME_COMMANDS: self._pause_pending = False`.

- [ ] **Step 4: Run all proxy tests**

Run: `uv run pytest tests/unit/test_powershell_proxy.py tests/unit/test_powershell_output.py -q --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/adapters/powershell/server.py tests/unit/test_powershell_proxy.py
git commit -m "PowerShell proxy: stopOnEntry emulation, pause/evaluate rewrites, stderr tagging"
```

---

### Task 9: Integration harness, fixtures, adapter-level tests (real pwsh + PSES)

**Files:**
- Create: `tests/integration/powershell_adapter_harness.py`, `tests/integration/fixtures/powershell/simple.ps1`, `functions.ps1`, `loop.ps1`, `throws.ps1`, `writes_error.ps1`, `exit7.ps1`
- Test: `tests/integration/test_powershell_adapter_launch.py`, `test_powershell_adapter_breakpoints.py`, `test_powershell_adapter_stepping.py`, `test_powershell_adapter_inspection.py`

**Interfaces:**
- Produces: `pwsh_ok() -> bool` (pwsh on PATH and `find_pses(None)` succeeds), `FIXTURES: Path` (= `tests/integration/fixtures/powershell`), `start_powershell_adapter() -> AdapterClient`, `launch_stopped(client, program, breakpoints=None, stop_on_entry=True, args=None)`.

- [ ] **Step 1: Write fixtures**

`tests/integration/fixtures/powershell/simple.ps1`:

```powershell
# simple: entry stop must land on line 3
$x = 1
$y = $x + 1
Write-Host "sum=$y"
Write-Output "out=$y"
exit 7
```

Wait — the entry stop lands on the first *executable* statement, which is line 2 (`$x = 1`); fix the comment: `# simple: entry stop must land on line 2`.

`functions.ps1`:

```powershell
function Add($a, $b) {
    $s = $a + $b
    return $s
}
function Outer($v) {
    $r = Add $v 2
    return $r
}
$x = 1
Write-Host "args=$($args -join '|')"
$y = Outer $x
Write-Host "sum=$y"
```

`loop.ps1`:

```powershell
$i = 0
while ($true) {
    $i++
    Start-Sleep -Milliseconds 50
}
```

`throws.ps1`:

```powershell
function Inner { throw "kaboom" }
Write-Host "before"
Inner
Write-Host "after"
```

`writes_error.ps1`:

```powershell
Write-Error "not fatal"
Write-Host "still here"
```

`exit7.ps1`:

```powershell
Write-Host "bye"
exit 7
```

- [ ] **Step 2: Write the harness**

`tests/integration/powershell_adapter_harness.py`:

```python
"""Scripted DAP client for the PowerShell proxy + shared launch helper."""

import shutil
from pathlib import Path

from tdb.adapters.powershell.locate import find_pses
from tests.integration.perl_adapter_harness import AdapterClient

FIXTURES = Path(__file__).parent / "fixtures" / "powershell"


def pwsh_ok() -> bool:
    """pwsh on PATH and a resolvable PSES module (config/env/VS Code)."""
    if shutil.which("pwsh") is None:
        return False
    try:
        find_pses(None)
    except FileNotFoundError:
        return False
    return True


async def start_powershell_adapter() -> AdapterClient:
    client = AdapterClient()
    await client.start(module="tdb.adapters.powershell")
    await client.request(
        "initialize",
        {"adapterID": "pses", "linesStartAt1": True, "columnsStartAt1": True},
    )
    return client


async def launch_stopped(
    client: AdapterClient, program: str, breakpoints=None, stop_on_entry=True, args=None
):
    """launch -> initialized -> [setBreakpoints] -> configurationDone."""
    launch_fut = client.send(
        "launch",
        {
            "type": "powershell",
            "request": "launch",
            "program": program,
            "args": list(args or []),
            "cwd": str(Path(program).parent),
            "stopOnEntry": stop_on_entry,
            "console": "internalConsole",
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


def output_text(client: AdapterClient, category: str | None = None) -> str:
    return "".join(
        e["body"].get("output", "")
        for e in list(client.events)
        if e["event"] == "output"
        and (category is None or e["body"].get("category") == category)
    )
```

- [ ] **Step 3: Write the launch tests**

`tests/integration/test_powershell_adapter_launch.py`:

```python
"""DAP-level: launch, entry stop, nonstop, output pump, exit code, args, env."""

import pytest

from tests.integration.powershell_adapter_harness import (
    FIXTURES,
    launch_stopped,
    output_text,
    pwsh_ok,
    start_powershell_adapter,
)

pytestmark = pytest.mark.skipif(not pwsh_ok(), reason="needs pwsh + PSES")


async def test_stop_on_entry_lands_on_first_statement():
    client = await start_powershell_adapter()
    try:
        program = str(FIXTURES / "simple.ps1")
        await launch_stopped(client, program)
        ev = await client.wait_event("stopped")
        assert ev["body"]["reason"] == "entry"
        st = await client.request("stackTrace", {"threadId": 1, "levels": 20})
        top = st["body"]["stackFrames"][0]
        assert top["source"]["path"] == program
        assert top["line"] == 2
        await client.request("continue", {"threadId": 1})
        exited = await client.wait_event("exited")
        assert exited["body"]["exitCode"] == 7
        await client.wait_event("terminated")
        assert "sum=2" in output_text(client) and "out=2" in output_text(client)
    finally:
        await client.stop()


async def test_nonstop_runs_to_completion_and_filters_prompt():
    client = await start_powershell_adapter()
    try:
        await launch_stopped(client, str(FIXTURES / "exit7.ps1"), stop_on_entry=False)
        exited = await client.wait_event("exited")
        assert exited["body"]["exitCode"] == 7
        await client.wait_event("terminated")
        text = output_text(client)
        assert "bye" in text
        assert "PS " not in text and "tdb_launch" not in text
        assert "tdb-exit" not in text
    finally:
        await client.stop()


async def test_args_with_spaces_and_quotes():
    client = await start_powershell_adapter()
    try:
        await launch_stopped(
            client,
            str(FIXTURES / "functions.ps1"),
            stop_on_entry=False,
            args=["one two", "it's", "three"],
        )
        await client.wait_event("terminated")
        assert "args=one two|it's|three" in output_text(client)
    finally:
        await client.stop()


async def test_env_reaches_script(tmp_path):
    p = tmp_path / "env.ps1"
    p.write_text('Write-Host "K=$env:TDB_PS_TEST"\n')
    client = await start_powershell_adapter()
    try:
        fut = client.send(
            "launch",
            {
                "type": "powershell",
                "request": "launch",
                "program": str(p),
                "args": [],
                "cwd": str(tmp_path),
                "stopOnEntry": False,
                "console": "internalConsole",
                "env": {"TDB_PS_TEST": "hello"},
            },
        )
        await client.wait_event("initialized")
        await client.request("configurationDone")
        await fut
        await client.wait_event("terminated")
        assert "K=hello" in output_text(client)
    finally:
        await client.stop()


async def test_cwd_is_honoured(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    p = tmp_path / "cwd.ps1"
    p.write_text("Write-Host (Get-Location).Path\n")
    client = await start_powershell_adapter()
    try:
        fut = client.send(
            "launch",
            {
                "type": "powershell",
                "request": "launch",
                "program": str(p),
                "args": [],
                "cwd": str(sub),
                "stopOnEntry": False,
                "console": "internalConsole",
            },
        )
        await client.wait_event("initialized")
        await client.request("configurationDone")
        await fut
        await client.wait_event("terminated")
        assert str(sub) in output_text(client)
    finally:
        await client.stop()


async def test_uncaught_throw_exit_1_and_stderr_block():
    client = await start_powershell_adapter()
    try:
        program = str(FIXTURES / "throws.ps1")
        await launch_stopped(client, program, stop_on_entry=False)
        exited = await client.wait_event("exited")
        assert exited["body"]["exitCode"] == 1
        await client.wait_event("terminated")
        err = output_text(client, "stderr")
        assert err.startswith(f"Exception: {program}:1")
        assert "kaboom" in err
        assert "\x1b[" not in err  # NO_COLOR honoured
        assert "before" in output_text(client, "stdout")
        assert "after" not in output_text(client)
    finally:
        await client.stop()


async def test_write_error_is_not_fatal():
    client = await start_powershell_adapter()
    try:
        await launch_stopped(
            client, str(FIXTURES / "writes_error.ps1"), stop_on_entry=False
        )
        exited = await client.wait_event("exited")
        assert exited["body"]["exitCode"] == 0
        await client.wait_event("terminated")
        assert "still here" in output_text(client, "stdout")
        assert output_text(client, "stderr") == ""
    finally:
        await client.stop()


async def test_disconnect_leaves_no_pwsh(tmp_path):
    import subprocess, time

    client = await start_powershell_adapter()
    try:
        await launch_stopped(client, str(FIXTURES / "loop.ps1"), stop_on_entry=False)
        await client.request("pause", {"threadId": 1})
        await client.wait_event("stopped")
        await client.request("disconnect")
        await client.proc.wait()
        for _ in range(50):
            out = subprocess.run(
                ["pgrep", "-f", "Start-EditorServices.ps1"],
                capture_output=True,
                text=True,
            ).stdout
            if not out.strip():
                break
            time.sleep(0.1)
        assert not out.strip(), "pwsh survived disconnect"
    finally:
        await client.stop()
```

- [ ] **Step 4: Write breakpoint, stepping, inspection tests**

`tests/integration/test_powershell_adapter_breakpoints.py`:

```python
import pytest

from tests.integration.powershell_adapter_harness import (
    FIXTURES,
    launch_stopped,
    output_text,
    pwsh_ok,
    start_powershell_adapter,
)

pytestmark = pytest.mark.skipif(not pwsh_ok(), reason="needs pwsh + PSES")
FUNCS = str(FIXTURES / "functions.ps1")


async def _top(client):
    st = await client.request("stackTrace", {"threadId": 1, "levels": 20})
    return st["body"]["stackFrames"]


async def test_line_breakpoint_inside_function():
    client = await start_powershell_adapter()
    try:
        await launch_stopped(
            client, FUNCS, breakpoints=[{"line": 2}], stop_on_entry=False
        )
        ev = await client.wait_event("stopped")
        assert ev["body"]["reason"] == "breakpoint"
        frames = await _top(client)
        assert frames[0]["line"] == 2
        assert [f["name"] for f in frames[:3]] == ["<Breakpoint>", "Add", "Outer"]
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
        assert "sum=3" in output_text(client)
    finally:
        await client.stop()


async def test_conditional_breakpoint(tmp_path):
    p = tmp_path / "cond.ps1"
    p.write_text("foreach ($i in 1..5) {\n    Write-Host $i\n}\n")
    client = await start_powershell_adapter()
    try:
        await launch_stopped(
            client,
            str(p),
            breakpoints=[{"line": 2, "condition": "$i -eq 4"}],
            stop_on_entry=False,
        )
        await client.wait_event("stopped")
        resp = await client.request("evaluate", {"expression": "$i", "context": "repl"})
        assert resp["body"]["result"] == "4"
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
    finally:
        await client.stop()


async def test_hit_count_breakpoint(tmp_path):
    p = tmp_path / "hit.ps1"
    p.write_text("foreach ($i in 1..5) {\n    Write-Host $i\n}\n")
    client = await start_powershell_adapter()
    try:
        await launch_stopped(
            client,
            str(p),
            breakpoints=[{"line": 2, "hitCondition": "3"}],
            stop_on_entry=False,
        )
        await client.wait_event("stopped")
        resp = await client.request("evaluate", {"expression": "$i", "context": "repl"})
        assert resp["body"]["result"] == "3"
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
    finally:
        await client.stop()


async def test_logpoint_prints_without_stopping(tmp_path):
    p = tmp_path / "log.ps1"
    p.write_text("foreach ($i in 1..3) {\n    $j = $i\n}\nWrite-Host done\n")
    client = await start_powershell_adapter()
    try:
        await launch_stopped(
            client,
            str(p),
            breakpoints=[{"line": 2, "logMessage": "i is $i"}],
            stop_on_entry=False,
        )
        await client.wait_event("terminated")
        assert not [e for e in client.events if e["event"] == "stopped"]
        text = output_text(client)
        assert "i is 1" in text and "i is 3" in text and "done" in text
    finally:
        await client.stop()


async def test_set_breakpoint_while_stopped_then_hit():
    client = await start_powershell_adapter()
    try:
        await launch_stopped(client, FUNCS)
        await client.wait_event("stopped")  # entry
        await client.request(
            "setBreakpoints", {"source": {"path": FUNCS}, "breakpoints": [{"line": 6}]}
        )
        await client.request("continue", {"threadId": 1})
        ev = await client.wait_event("stopped")
        assert ev["body"]["reason"] == "breakpoint"
        assert (await _top(client))[0]["line"] == 6
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
    finally:
        await client.stop()


async def test_entry_breakpoint_is_gone_after_entry_stop(tmp_path):
    """The synthetic line-1 breakpoint must not fire again on a re-run of
    line 1 (loop back to top)."""
    p = tmp_path / "twice.ps1"
    p.write_text("$n = 0\nwhile ($n -lt 2) { $n++ }\nWrite-Host done\n")
    client = await start_powershell_adapter()
    try:
        await launch_stopped(client, str(p))
        ev = await client.wait_event("stopped")
        assert ev["body"]["reason"] == "entry"
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
        assert not [e for e in client.events if e["event"] == "stopped"]
    finally:
        await client.stop()
```

`tests/integration/test_powershell_adapter_stepping.py`:

```python
import pytest

from tests.integration.powershell_adapter_harness import (
    FIXTURES,
    launch_stopped,
    pwsh_ok,
    start_powershell_adapter,
)

pytestmark = pytest.mark.skipif(not pwsh_ok(), reason="needs pwsh + PSES")
FUNCS = str(FIXTURES / "functions.ps1")


async def _line_and_names(client):
    st = await client.request("stackTrace", {"threadId": 1, "levels": 20})
    frames = st["body"]["stackFrames"]
    return frames[0]["line"], [f["name"] for f in frames]


async def _step(client, cmd):
    await client.request(cmd, {"threadId": 1})
    ev = await client.wait_event("stopped")
    assert ev["body"]["reason"] == "step"


async def test_next_stepin_stepout():
    client = await start_powershell_adapter()
    try:
        await launch_stopped(client, FUNCS)
        await client.wait_event("stopped")
        line, _ = await _line_and_names(client)
        assert line == 9  # $x = 1
        await _step(client, "next")
        assert (await _line_and_names(client))[0] == 10
        await _step(client, "next")
        assert (await _line_and_names(client))[0] == 11  # $y = Outer $x
        await _step(client, "stepIn")
        line, names = await _line_and_names(client)
        assert line == 6 and names[1] == "Outer"
        await _step(client, "stepIn")
        line, names = await _line_and_names(client)
        assert line == 2 and names[1:3] == ["Add", "Outer"]
        await _step(client, "stepOut")
        line, names = await _line_and_names(client)
        assert names[1] == "Outer"
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
    finally:
        await client.stop()


async def test_launcher_frame_is_at_the_bottom():
    client = await start_powershell_adapter()
    try:
        await launch_stopped(
            client, FUNCS, breakpoints=[{"line": 2}], stop_on_entry=False
        )
        await client.wait_event("stopped")
        st = await client.request("stackTrace", {"threadId": 1, "levels": 20})
        frames = st["body"]["stackFrames"]
        assert frames[-1]["source"]["path"].endswith("tdb_launch.ps1")
        assert frames[0]["source"]["path"] == FUNCS
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
    finally:
        await client.stop()
```

`tests/integration/test_powershell_adapter_inspection.py`:

```python
import pytest

from tests.integration.powershell_adapter_harness import (
    FIXTURES,
    launch_stopped,
    output_text,
    pwsh_ok,
    start_powershell_adapter,
)

pytestmark = pytest.mark.skipif(not pwsh_ok(), reason="needs pwsh + PSES")
FUNCS = str(FIXTURES / "functions.ps1")


async def _stopped_in_add(client):
    await launch_stopped(client, FUNCS, breakpoints=[{"line": 3}], stop_on_entry=False)
    await client.wait_event("stopped")


async def test_scopes_and_locals():
    client = await start_powershell_adapter()
    try:
        await _stopped_in_add(client)
        sc = await client.request("scopes", {"frameId": 0})
        names = [s["name"] for s in sc["body"]["scopes"]]
        assert "Local" in names and "Script" in names and "Global" in names
        local = next(s for s in sc["body"]["scopes"] if s["name"] == "Local")
        vs = await client.request(
            "variables", {"variablesReference": local["variablesReference"]}
        )
        byname = {v["name"]: v["value"] for v in vs["body"]["variables"]}
        assert byname["$a"] == "1" and byname["$b"] == "2" and byname["$s"] == "3"
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
    finally:
        await client.stop()


async def test_evaluate_returns_values_in_repl_context():
    client = await start_powershell_adapter()
    try:
        await _stopped_in_add(client)
        resp = await client.request(
            "evaluate", {"expression": "$s * 10", "context": "repl"}
        )
        assert resp["body"]["result"] == "30"
        assert "30" not in output_text(client)  # not printed to the console
        resp = await client.request(
            "evaluate", {"expression": "$nope.Foo()", "context": "repl"}
        )
        assert resp["success"]  # PSES reports failures as empty results
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
    finally:
        await client.stop()


async def test_set_variable_changes_result():
    client = await start_powershell_adapter()
    try:
        await _stopped_in_add(client)
        sc = await client.request("scopes", {"frameId": 0})
        local = next(s for s in sc["body"]["scopes"] if s["name"] == "Local")
        resp = await client.request(
            "setVariable",
            {
                "variablesReference": local["variablesReference"],
                "name": "$s",
                "value": "40",
            },
        )
        assert resp["success"]
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
        assert "sum=40" in output_text(client)
    finally:
        await client.stop()


async def test_threads():
    client = await start_powershell_adapter()
    try:
        await _stopped_in_add(client)
        th = await client.request("threads")
        assert len(th["body"]["threads"]) == 1
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
    finally:
        await client.stop()
```

- [ ] **Step 5: Run the integration tests**

Ensure PSES is resolvable: `export TDB_PSES_PATH=~/.local/share/tdb/pses/PowerShellEditorServices` (this machine has it there).

Run: `TDB_PSES_PATH=~/.local/share/tdb/pses/PowerShellEditorServices uv run pytest tests/integration/test_powershell_adapter_launch.py tests/integration/test_powershell_adapter_breakpoints.py tests/integration/test_powershell_adapter_stepping.py tests/integration/test_powershell_adapter_inspection.py -q --no-cov -x`
Expected: PASS. Known things to adjust from real PSES behaviour rather than the plan's guesses: exact step landing lines in `test_next_stepin_stepout` (verify by reading the stack after each step and fix the asserted numbers, keeping the *shape* of the test), `$s` value formatting in `test_scopes_and_locals` (PSES may render `3` or `[int] 3`; assert with `endswith("3")` if so), and the `setVariable` value syntax (`40` vs `"40"`). Do NOT loosen the entry-line, exit-code, arg-quoting, prompt-filter, or stderr-tagging assertions — those are the spec's load-bearing claims.

- [ ] **Step 6: Commit**

```bash
git add tests/integration/powershell_adapter_harness.py tests/integration/fixtures/powershell tests/integration/test_powershell_adapter_*.py
git commit -m "PowerShell: integration harness, fixtures, adapter-level tests"
```

---

### Task 10: Session-level, run-mode, and replay tests

**Files:**
- Test: `tests/integration/test_powershell_session.py`, `tests/integration/test_powershell_run_mode.py`, `tests/integration/test_replay_powershell.py`

**Interfaces:**
- Consumes: `DebugController`, `ServerEventHandler` (as `test_ruby_pause_frame.py` does), `tdb.run_mode.run`, `tdb.replay.load_recording/run_replay`, `build_powershell_profile`, `parse_powershell_error`, harness `pwsh_ok`/`FIXTURES`.

- [ ] **Step 1: Write the session test**

`tests/integration/test_powershell_session.py`:

```python
"""End-to-end through DebugController: entry stop, stack, variables,
fatal-error modal data, and pause. Modeled on test_ruby_pause_frame.py."""

from __future__ import annotations

import asyncio

import pytest

from tdb.dap.types import SourceBreakpoint
from tdb.languages.errors import parse_powershell_error
from tdb.languages.powershell import build_powershell_profile
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController

from tests.integration.powershell_adapter_harness import FIXTURES, pwsh_ok

pytestmark = pytest.mark.skipif(not pwsh_ok(), reason="needs pwsh + PSES")
WAIT = 30.0


async def _start(program: str, stop_on_entry: bool):
    handler = ServerEventHandler()
    ctrl = DebugController(handler, profile=build_powershell_profile())
    await ctrl.start(program=program, stop_on_entry=stop_on_entry)
    await asyncio.wait_for(handler.initialized_event.wait(), WAIT)
    await ctrl.do_configure()
    return ctrl, handler


async def _stop(ctrl):
    try:
        await asyncio.wait_for(ctrl.stop(), timeout=WAIT)
    except Exception:
        pass


async def test_entry_stop_stack_and_variables():
    ctrl, handler = await _start(str(FIXTURES / "functions.ps1"), True)
    try:
        await asyncio.wait_for(handler.stopped_event.wait(), WAIT)
        assert ctrl.state.stop_reason == "entry"
        assert ctrl.state.current_line == 9
        frames = ctrl.state.stack_frames
        assert frames and frames[0].source_path == str(FIXTURES / "functions.ps1")
    finally:
        await _stop(ctrl)


async def test_fatal_error_yields_modal_data():
    program = str(FIXTURES / "throws.ps1")
    ctrl, handler = await _start(program, False)
    try:
        await asyncio.wait_for(handler.terminated_event.wait(), WAIT)
        stderr = "".join(t for t, cat in handler.output_chunks if cat == "stderr")
        exit_code = handler.exit_code
        assert exit_code == 1
        err = parse_powershell_error(stderr, exit_code)
        assert err is not None
        assert err.frames[0].path == program and err.frames[0].line == 1
        assert err.message == "kaboom"
    finally:
        await _stop(ctrl)


async def test_write_error_yields_no_modal():
    ctrl, handler = await _start(str(FIXTURES / "writes_error.ps1"), False)
    try:
        await asyncio.wait_for(handler.terminated_event.wait(), WAIT)
        assert handler.exit_code == 0
        stderr = "".join(t for t, cat in handler.output_chunks if cat == "stderr")
        assert parse_powershell_error(stderr, handler.exit_code) is None
    finally:
        await _stop(ctrl)


async def test_pause_while_running_then_continue():
    ctrl, handler = await _start(str(FIXTURES / "loop.ps1"), False)
    try:
        await asyncio.sleep(0.5)
        await ctrl.do_pause()
        await asyncio.wait_for(handler.stopped_event.wait(), WAIT)
        assert ctrl.state.stop_reason == "pause"
        result = await ctrl.evaluate("$i")
        assert int(result.result) >= 1
    finally:
        await _stop(ctrl)
```

Before running, open `src/tdb/server/event_handler.py` and `src/tdb/session/state.py` and replace the attribute names used above (`stopped_event`, `terminated_event`, `output_chunks`, `exit_code`, `stop_reason`, `current_line`, `stack_frames`, `source_path`, `do_pause`, `evaluate`) with the real ones — `test_ruby_pause_frame.py` and `test_go_session.py` show the actual API. Keep the assertions' meaning.

- [ ] **Step 2: Write the run-mode test**

`tests/integration/test_powershell_run_mode.py`:

```python
import asyncio

import pytest

from tdb import run_mode
from tdb.languages.powershell import build_powershell_profile
from tdb.persist import TdbConfig

from tests.integration.powershell_adapter_harness import FIXTURES, pwsh_ok

pytestmark = pytest.mark.skipif(not pwsh_ok(), reason="needs pwsh + PSES")


async def test_powershell_runs_headless_without_tui_episode(tmp_path, capfd):
    p = tmp_path / "hello.ps1"
    p.write_text('Write-Host "pshello"\n')
    episodes = []

    async def fake_episode(controller, handler, console, config, program):
        episodes.append(controller.state.phase)
        return False

    code = await asyncio.wait_for(
        run_mode.run(
            program=str(p),
            config=TdbConfig(),
            profile=build_powershell_profile(),
            tui_episode=fake_episode,
        ),
        timeout=90.0,
    )
    assert episodes == []
    assert code == 0
    assert "pshello" in capfd.readouterr().out


async def test_powershell_exit_code_passthrough(capfd):
    code = await asyncio.wait_for(
        run_mode.run(
            program=str(FIXTURES / "exit7.ps1"),
            config=TdbConfig(),
            profile=build_powershell_profile(),
        ),
        timeout=90.0,
    )
    assert code == 7
    assert "bye" in capfd.readouterr().out


async def test_powershell_fatal_error_exit_1(capfd):
    code = await asyncio.wait_for(
        run_mode.run(
            program=str(FIXTURES / "throws.ps1"),
            config=TdbConfig(),
            profile=build_powershell_profile(),
        ),
        timeout=90.0,
    )
    assert code == 1
    out = capfd.readouterr()
    assert "kaboom" in out.out + out.err
```

Also copy the signal-pause episode test shape from `test_run_mode.py::test_signal_pause_episode_detach_and_terminate` for `loop.ps1` if that test is parameterizable by profile; if it is Python-specific, skip it here (the controller-level pause test above already covers pause).

- [ ] **Step 3: Write the replay test**

`tests/integration/test_replay_powershell.py`:

```python
"""Replay is language-agnostic: a PowerShell recording replays through
the proxy adapter."""

import json

import pytest

from tdb.replay import load_recording, run_replay
from tests.integration.powershell_adapter_harness import pwsh_ok

pytestmark = pytest.mark.skipif(not pwsh_ok(), reason="needs pwsh + PSES")

TOY = """\
$x = 1
$y = 2
$z = $x + $y
Write-Host "z=$z"
"""


async def test_powershell_recording_replays(tmp_path):
    prog = tmp_path / "toy.ps1"
    prog.write_text(TOY)
    header = {
        "tdb_recording": 1,
        "created": "2026-09-03T00:00:00",
        "mode": "launch",
        "language": "powershell",
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
        {"t": 0.3, "action": "evaluate", "params": ["$x + $y"]},
        {"t": 0.4, "action": "quit", "params": []},
    ]
    path = tmp_path / "ps.jsonl"
    path.write_text(
        "\n".join([json.dumps(header)] + [json.dumps(r) for r in records]) + "\n"
    )
    out: list[str] = []
    errors = await run_replay(load_recording(str(path)), echo=out.append)
    assert errors == 0
    assert "3" in "\n".join(out)
```

- [ ] **Step 4: Run them**

Run: `TDB_PSES_PATH=~/.local/share/tdb/pses/PowerShellEditorServices uv run pytest tests/integration/test_powershell_session.py tests/integration/test_powershell_run_mode.py tests/integration/test_replay_powershell.py -q --no-cov -x`
Expected: PASS after fixing attribute names against the real handler/state API.

- [ ] **Step 5: Run the whole suite once**

Run: `TDB_PSES_PATH=~/.local/share/tdb/pses/PowerShellEditorServices uv run pytest -q -x --no-cov -p no:cacheprovider`
Expected: everything green (other languages' suites skip when their tools are absent). Per the OOM memory note, if the full suite is heavy on this machine, run it inside the `memcap-pytest` wrapper.

- [ ] **Step 6: Commit**

```bash
git add tests/integration/test_powershell_session.py tests/integration/test_powershell_run_mode.py tests/integration/test_replay_powershell.py
git commit -m "PowerShell: controller, run-mode, and replay integration tests"
```

---

### Task 11: CI — Dockerfile and workflow

**Files:**
- Modify: `Dockerfile` (a new layer after the Go/Delve layer, before `adduser`), `.github/workflows/test.yml` (no change needed if the Dockerfile carries everything; verify)

- [ ] **Step 1: Add pwsh + PSES to the image**

Insert after the Go/Delve `RUN` block:

```dockerfile
# PowerShell 7 (musl build for Alpine) + PowerShell Editor Services (the
# DAP server tdb's PowerShell proxy drives). Version pins match README's
# "PowerShell" section; bump both together. `libstdc++`/`icu-libs`/
# `lttng-ust` are pwsh's documented Alpine runtime deps.
ARG PWSH_VERSION=7.6.5
ARG PSES_VERSION=v4.7.0
RUN apk add --no-cache libstdc++ icu-libs lttng-ust krb5-libs zlib libgcc \
 && mkdir -p /opt/microsoft/powershell/7 \
 && wget -qO /tmp/pwsh.tgz "https://github.com/PowerShell/PowerShell/releases/download/v${PWSH_VERSION}/powershell-${PWSH_VERSION}-linux-musl-x64.tar.gz" \
 && tar -xzf /tmp/pwsh.tgz -C /opt/microsoft/powershell/7 \
 && chmod +x /opt/microsoft/powershell/7/pwsh \
 && ln -s /opt/microsoft/powershell/7/pwsh /usr/local/bin/pwsh \
 && rm /tmp/pwsh.tgz \
 && mkdir -p /opt/pses \
 && wget -qO /tmp/pses.zip "https://github.com/PowerShell/PowerShellEditorServices/releases/download/${PSES_VERSION}/PowerShellEditorServices.zip" \
 && unzip -q /tmp/pses.zip -d /opt/pses \
 && rm /tmp/pses.zip \
 && pwsh -NoProfile -Command '$PSVersionTable.PSVersion.ToString()'
ENV TDB_PSES_PATH=/opt/pses/PowerShellEditorServices
```

Check the pwsh tarball's actual file name for 7.6.5 before committing (`curl -sI https://github.com/PowerShell/PowerShell/releases/download/v7.6.5/powershell-7.6.5-linux-musl-x64.tar.gz | head -1` should be a 302). If Alpine's `unzip` is absent in the base image, add `unzip` to the `apk add` list.

- [ ] **Step 2: Build and run the PowerShell tests in Docker**

Run:

```bash
docker build --target base -t tdb-test . \
 && docker run --rm --cap-add=SYS_PTRACE --security-opt seccomp=unconfined \
      tdb-test uv run pytest tests/unit/test_powershell_proxy.py tests/integration/test_powershell_adapter_launch.py tests/integration/test_powershell_session.py -q --no-cov -p no:cacheprovider
```

Expected: PASS. If `pwsh` fails to start on Alpine, the usual cause is a missing runtime library; `ldd /opt/microsoft/powershell/7/pwsh` names it.

- [ ] **Step 3: Workflow**

`.github/workflows/test.yml` builds through `base` and runs pytest in the container, so the Dockerfile change is sufficient. Confirm by reading the file; make no change unless a step lists per-language installs (it does not today).

- [ ] **Step 4: Commit**

```bash
git add Dockerfile
git commit -m "CI: install pwsh (musl) and PSES v4.7.0 in the test image"
```

---

### Task 12: Documentation and example

**Files:**
- Modify: `README.md` (intro bullet list, Feature Overview sentence, language table, detection list, a new `### PowerShell` section after `### Go`, Configuration section's adapter-key note, External Terminal Support's language list, Remote Attach's language list)
- Create: `examples/hello_powershell.ps1`

- [ ] **Step 1: README edits**

Intro list (after the Go bullet):

```markdown
- PowerShell 7 (via [PowerShell Editor Services](https://github.com/PowerShell/PowerShellEditorServices); Linux/macOS, Windows experimental)
```

Feature Overview sentence: append `, and PowerShell 7 (via PowerShell Editor Services)` to the language enumeration.

Language table (after the Go row):

```markdown
| PowerShell 7 | `pses` (PowerShell Editor Services, driven by tdb's bundled proxy) | `pwsh` ≥ 7.2 on PATH + the PSES module (see [PowerShell](#powershell)) | core debugging (breakpoints incl. conditional/hit-count/log, stepping, stack, variables, evaluate console) + `--run`; no `--terminal`, no remote attach; Windows untested |
```

Detection list: in item 2 add `` `.ps1` / `.psm1` → PowerShell ``; in item 4 add `` `#!...pwsh` `` → PowerShell.

New section after `### Go` (before `## Layout`):

````markdown
### PowerShell

tdb debugs PowerShell 7 scripts through [PowerShell Editor Services](https://github.com/PowerShell/PowerShellEditorServices)
(PSES), the Debug Adapter Protocol server behind the VS Code PowerShell
extension. You need `pwsh` (>= 7.2; https://aka.ms/powershell) on PATH
and the PSES module. tdb looks for the module in this order:

1. `{"adapters": {"pses": "/path/to/PowerShellEditorServices"}}` in `config.json`
2. the `TDB_PSES_PATH` environment variable
3. the copy bundled with an installed VS Code PowerShell extension
   (`~/.vscode/extensions/ms-vscode.powershell-*/modules/PowerShellEditorServices`)

Without VS Code, download the release once:

```bash
mkdir -p ~/.local/share/tdb/pses && cd ~/.local/share/tdb/pses
curl -sLO https://github.com/PowerShell/PowerShellEditorServices/releases/download/v4.7.0/PowerShellEditorServices.zip
unzip -q PowerShellEditorServices.zip
export TDB_PSES_PATH=~/.local/share/tdb/pses/PowerShellEditorServices
```

A `pwsh` that isn't on PATH can be named with `{"adapters": {"pwsh": "/path/to/pwsh"}}`.

```bash
tdb script.ps1               # launch, stop at the first statement
tdb --run script.ps1         # run immediately, debug on demand (pause works)
```

Notes:

- The script runs inside a small bundled launcher (`tdb_launch.ps1`) so tdb
  can report the script's exit code; it shows up as the bottom `<ScriptBlock>`
  frame in the Stack view. The `<Breakpoint>` frame at the top is PSES's
  marker for the current position and is the frame to inspect.
- An uncaught terminating error ends the script with exit code 1 and opens
  the fatal-error modal from pwsh's concise error text. Non-terminating
  errors (`Write-Error`, failing cmdlets without `-ErrorAction Stop`) print
  the same text and let the script continue, as in pwsh itself. There is no
  break-on-error yet.
- `--terminal` and remote attach are not supported for PowerShell yet.
- Windows PowerShell 5.1 is not supported (pwsh 7 only). Running tdb *on*
  Windows against pwsh is designed for but not yet verified.
````

Configuration section: after the Perl "special case" paragraph add:

```markdown
**PowerShell** follows the same pattern with two keys: `adapters.pwsh` names
the interpreter and `adapters.pses` names the PowerShell Editor Services
module directory (see [PowerShell](#powershell)).
```

External Terminal Support and Remote Attach sections: wherever they enumerate languages, note PowerShell is excluded (one clause each, matching how Go is excluded from `--terminal`).

- [ ] **Step 2: Example**

`examples/hello_powershell.ps1`:

```powershell
# tdb example: `tdb examples/hello_powershell.ps1`
# Set a breakpoint on the `return` line inside Square, step, and evaluate $n.
function Square($n) {
    $sq = $n * $n
    return $sq
}

$total = 0
foreach ($i in 1..5) {
    $total += Square $i
    Write-Host "i=$i total=$total"
}
Write-Error "a non-fatal error goes to the console too"
Write-Host "done: $total"
```

- [ ] **Step 3: Sanity-run the example by hand**

Run: `TDB_PSES_PATH=~/.local/share/tdb/pses/PowerShellEditorServices uv run tdb --run examples/hello_powershell.ps1`
Expected: the five `i=` lines, the error text, `done: 55`, exit 0. Then `uv run tdb --headless --server ...` is not needed; one interactive `uv run tdb examples/hello_powershell.ps1` to eyeball the entry stop, a step, and the Variables view is enough (quit with `q`).

- [ ] **Step 4: Commit**

```bash
git add README.md examples/hello_powershell.ps1
git commit -m "PowerShell: README section, language table, example script"
```

---

### Task 13: Final review pass

- [ ] **Step 1: Full test run with coverage**

Run: `TDB_PSES_PATH=~/.local/share/tdb/pses/PowerShellEditorServices uv run pytest -q -p no:cacheprovider`
Expected: green; `tdb/adapters/powershell/*` and `tdb/languages/powershell.py` above ~85% covered (the Windows pipe function is the accepted gap).

- [ ] **Step 2: Lint/format as the repo does** (`grep -n "ruff\|black" pyproject.toml` — run whichever is configured, e.g. `uv run ruff check src tests && uv run ruff format --check src tests`).

- [ ] **Step 3: Request code review** via the `superpowers:requesting-code-review` skill against the spec, then address findings in a fix commit.

---

## Self-review notes

- Spec coverage: profile/registry/CLI (T1), parser (T2), locator + precedence + hints (T3), launcher/quoting/sentinel/classifier (T4), seq translation (T5), fake (T6), proxy core incl. session file, version floor, transport seam, teardown, exited synthesis (T7), stopOnEntry/pause/evaluate/stderr (T8), integration tiers (T9–T10), CI (T11), docs + example + `.deb` exclusion (T12, Global Constraints). Break-on-error, `--terminal`, attach, Windows CI are documented follow-ups, not tasks.
- Type consistency: `PsesAdapter(pwsh_executable, pses_dir)` (T1) feeds launch-body keys `"pwsh"`/`"pses"` read by `_on_launch` (T7) via `find_pwsh`/`find_pses` (T3). `OutputClassifier.classify`, `parse_exit_sentinel`, `quote_ps_arg`, `LAUNCHER` (T4) are used verbatim in T7. `SeqTranslator` method names (T5) match T7/T8 usage. `_up_request`, `_entry_pending`, `_entry_synthetic`, `_user_bps`, `_main_bps_sent`, `_pause_pending`, `_response_hooks`, `_rewrite_response` are declared in T7 and used in T8.
- Known plan-level uncertainties, flagged inline: exact step landing lines and value rendering in T9 (adjust to real PSES output), ServerEventHandler/state attribute names in T10 (copy from existing tests), Alpine pwsh tarball name and runtime deps in T11.

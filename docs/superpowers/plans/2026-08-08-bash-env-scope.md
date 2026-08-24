# Bash Environment Scope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an Environment scope (exported variables) to the bash Variables view, with a strict three-way split: Locals / Globals (unexported) / Environment (exported).

**Architecture:** `declare -p` output already encodes exportedness in its flags field (`declare -x`); the parser starts surfacing it as `BashVar.exported`, the session splits on it, and the DAP server adds the third scope. The launch-snapshot env filter from commit 3db145d is retired — under the strict split, inherited environment (always exported) can never reach Globals. No harness, protocol, or wire changes.

**Tech Stack:** Python stdlib only. Spec: `docs/superpowers/specs/2026-08-08-bash-env-scope-design.md` — read it first.

## Global Constraints

- Strict split: no variable appears in more than one of Globals/Environment. Globals = unexported (still `_INTERNAL_VARS`-filtered). Environment = exported, filtered ONLY by `__tdb_`/`__TDB_` prefixes — `PATH`, `HOME`, `PWD`, `TERM` must SHOW there.
- Snapshot machinery (`_launch_env_snapshot`, `_unquote_declare_scalar`, `_ESCAPED`) is removed, not left dead; its obsolete test is reworked, not deleted-without-replacement.
- Environment appears for every frame (like Globals); Locals stays innermost-only.
- `uv run pytest` from repo root (`/home/al/projects/tdbg/work`). Never bare `pip`. Branch: `bash-dap` (do not merge to main).
- Existing fixtures' line numbers are load-bearing for other tests — extend nothing in place; new fixture only.

---

### Task 1: Parser flag + session split

**Files:**
- Modify: `src/tdb/adapters/bash/declares.py` (BashVar ~line 24, parse_declares ~line 68)
- Modify: `src/tdb/adapters/bash/session.py` (remove `_ESCAPED`/`_unquote_declare_scalar` ~lines 80-108, `_launch_env_snapshot` init ~line 134 and assignment ~line 178; rewrite `globals_vars` ~line 414; add `environment_vars`)
- Create: `tests/integration/fixtures/bash_env_scopes.sh`
- Test: `tests/unit/test_bash_declares.py` (append), `tests/integration/test_bash_session.py` (rework one test, add one)

**Interfaces:**
- Consumes: `parse_declares`, `BashSession.request/launch`, `_INTERNAL_VARS` (all existing).
- Produces: `BashVar.exported: bool` (default False); `BashSession.environment_vars() -> list[BashVar]`; `globals_vars()` now returns unexported-only. Task 2's server calls `environment_vars` by exactly this name.

- [ ] **Step 1: Write the failing unit tests**

Append to `tests/unit/test_bash_declares.py`:

```python
def test_exported_flag_detected():
    out = parse_declares('declare -x PATH="/usr/bin"\ndeclare -- plain="v"')
    assert out[0].exported is True
    assert out[1].exported is False


def test_exported_default_false_for_arrays_and_ints():
    out = parse_declares(
        'declare -i n="5"\ndeclare -a a=([0]="x")\ndeclare -A m=([k]="v")'
    )
    assert [v.exported for v in out] == [False, False, False]


def test_exported_array_keeps_children():
    out = parse_declares('declare -ax arr=([0]="x" [1]="y")')
    v = out[0]
    assert v.exported is True
    assert v.value == "array[2]"
    assert v.children == [("0", '"x"'), ("1", '"y"')]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_bash_declares.py -v`
Expected: FAIL — `BashVar` has no attribute/field `exported`.

- [ ] **Step 3: Implement the parser change**

In `src/tdb/adapters/bash/declares.py`: add the field (defaulted, so every
existing construction stands) and set it from the already-captured flags
group:

```python
@dataclass(frozen=True)
class BashVar:
    name: str
    value: str
    children: list[tuple[str, str]] | None
    exported: bool = False
```

In `parse_declares`, both `out.append(...)` sites gain
`exported="x" in flags`:

```python
            out.append(
                BashVar(name=name, value=summary, children=items, exported="x" in flags)
            )
        else:
            out.append(
                BashVar(name=name, value=value, children=None, exported="x" in flags)
            )
```

- [ ] **Step 4: Run unit tests to verify pass**

Run: `uv run pytest tests/unit/test_bash_declares.py -v`
Expected: PASS (new tests and all pre-existing ones — the default keeps
old equality assertions valid).

- [ ] **Step 5: Write the fixture and the session-level tests**

```bash
# tests/integration/fixtures/bash_env_scopes.sh
plain_var="unexported"
export exported_var="from-script"
echo "marker"
```

In `tests/integration/test_bash_session.py`, REPLACE the now-obsolete
snapshot-filter test `test_globals_hide_untouched_inherited_env_but_show_script_vars`
(delete its body; the strict split supersedes its semantics) with:

```python
@pytest.mark.asyncio
async def test_strict_split_globals_vs_environment():
    """Strict split: unexported -> Globals only; exported (script's own
    AND inherited) -> Environment only; the two lists are disjoint."""
    rec = Recorder()
    fixture = FIXTURES / "bash_env_scopes.sh"
    session = BashSession(rec.on_output, rec.on_stop, rec.on_exit)
    env = dict(os.environ)
    env["TDB_TEST_SENTINEL"] = "inherited-untouched"
    await session.launch(
        program=str(fixture), args=[], cwd=str(fixture.parent), env=env
    )
    await session.set_breakpoint(str(fixture), 3)  # echo "marker"
    session.resume("continue")
    await rec.wait_stop()

    gnames = {v.name for v in await session.globals_vars()}
    envvars = {v.name: v for v in await session.environment_vars()}

    assert "plain_var" in gnames  # unexported -> Globals
    assert "plain_var" not in envvars
    assert "exported_var" in envvars  # script export -> Environment
    assert "exported_var" not in gnames
    assert '"from-script"' == envvars["exported_var"].value
    assert "TDB_TEST_SENTINEL" in envvars  # inherited -> Environment
    assert "TDB_TEST_SENTINEL" not in gnames
    assert "PATH" in envvars  # real env deliberately shown
    assert not any(n.startswith(("__tdb_", "__TDB_")) for n in envvars)
    assert not any(n.startswith(("__tdb_", "__TDB_")) for n in gnames)
    assert gnames.isdisjoint(envvars)

    session.resume("continue")
    await asyncio.wait_for(rec.exit_event.wait(), 10)
    await session.stop()
```

(`import os` at the top of the file if not already present. The existing
arrays/filter test `test_globals_include_arrays_and_filter_internals`
stays — its fixture variables are unexported, so its assertions survive
the split; if it asserted on an exported variable it would have to move
that assertion to `environment_vars()`, but it does not.)

- [ ] **Step 6: Implement the session split**

In `src/tdb/adapters/bash/session.py`:
- Delete `_ESCAPED` and `_unquote_declare_scalar` (~lines 80-108).
- Delete `self._launch_env_snapshot: dict[str, str] = {}` (~line 134) and
  `self._launch_env_snapshot = dict(child_env)` (~line 178).
- Replace `globals_vars` (~lines 414-431) and add `environment_vars`:

```python
async def globals_vars(self) -> list[BashVar]:
    """Unexported shell variables (the script's own state).

    Exported variables — inherited environment and the script's own
    exports alike — live in environment_vars() instead; the strict
    split means nothing appears in both.
    """
    return [
        v
        for v in parse_declares(await self.request("globals"))
        if not v.exported and not _INTERNAL_VARS.match(v.name)
    ]


async def environment_vars(self) -> list[BashVar]:
    """Exported variables (bash's actual environment).

    Deliberately NOT filtered by _INTERNAL_VARS — PATH/HOME/PWD/TERM
    are the point of an environment tree. Only the harness's own
    control variables are hidden.
    """
    return [
        v
        for v in parse_declares(await self.request("globals"))
        if v.exported and not v.name.startswith(("__tdb_", "__TDB_"))
    ]
```

(Each call issues its own `globals` request — still the existing wire
command, no new protocol; the DAP client expands scopes lazily so the
two are rarely fetched together.)

- [ ] **Step 7: Run the session tests**

Run: `uv run pytest tests/integration/test_bash_session.py tests/unit/test_bash_declares.py -v`
Expected: PASS, including every pre-existing test. If
`test_globals_include_arrays_and_filter_internals` fails on a variable
that turns out to be exported in the fixture, move that one assertion to
an `environment_vars()` check — do not weaken it.

- [ ] **Step 8: Commit**

```bash
git add src/tdb/adapters/bash/declares.py src/tdb/adapters/bash/session.py \
        tests/unit/test_bash_declares.py tests/integration/test_bash_session.py \
        tests/integration/fixtures/bash_env_scopes.sh
git commit -m "feat: bash strict Globals/Environment split in the session layer"
```

---

### Task 2: Environment scope in the DAP server + docs

**Files:**
- Modify: `src/tdb/adapters/bash/server.py` (`_on_scopes` ~line 280, `_on_variables` ~line 317)
- Modify: `README.md` (bash section)
- Test: `tests/integration/test_bash_adapter_inspection.py`

**Interfaces:**
- Consumes: `BashSession.environment_vars() -> list[BashVar]` (Task 1), existing `_add_ref`/`_var_to_dap` helpers.
- Produces: DAP scopes `["Locals", "Globals", "Environment"]` (frame 0) / `["Globals", "Environment"]` (outer frames); ref kind `("scope", "environment")`.

- [ ] **Step 1: Write the failing DAP tests**

Append to `tests/integration/test_bash_adapter_inspection.py`:

```python
@pytest.mark.asyncio
async def test_environment_scope_listed_and_populated():
    client = await start_bash_adapter()
    try:
        program = str(FIXTURES / "bash_env_scopes.sh")
        await launch_stopped(
            client, program, breakpoints=[{"line": 3}], stop_on_entry=False
        )
        await client.wait_event("stopped")
        await client.request("stackTrace", {"threadId": 1})
        scopes = (await client.request("scopes", {"frameId": 0}))["body"]["scopes"]
        assert [s["name"] for s in scopes] == ["Locals", "Globals", "Environment"]
        env_ref = scopes[2]["variablesReference"]
        env = {
            v["name"]: v
            for v in (
                await client.request("variables", {"variablesReference": env_ref})
            )["body"]["variables"]
        }
        assert "exported_var" in env
        assert "PATH" in env
        assert "plain_var" not in env
        globals_ref = scopes[1]["variablesReference"]
        gvars = {
            v["name"]
            for v in (
                await client.request("variables", {"variablesReference": globals_ref})
            )["body"]["variables"]
        }
        assert "plain_var" in gvars
        assert "exported_var" not in gvars
        await client.request("continue", {"threadId": 1})
        await client.wait_event("exited")
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_outer_frames_get_globals_and_environment():
    client = await start_bash_adapter()
    try:
        program = str(FIXTURES / "bash_functions.sh")
        await launch_stopped(
            client, program, breakpoints=[{"line": 3}], stop_on_entry=False
        )
        await client.wait_event("stopped")
        await client.request("stackTrace", {"threadId": 1})
        scopes1 = (await client.request("scopes", {"frameId": 1}))["body"]["scopes"]
        assert [s["name"] for s in scopes1] == ["Globals", "Environment"]
        await client.request("continue", {"threadId": 1})
        await client.wait_event("exited")
    finally:
        await client.stop()
```

Also UPDATE the existing `test_outer_frame_scopes_are_globals_only`:
its assertions `== ["Locals", "Globals"]` / `== ["Globals"]` become
`== ["Locals", "Globals", "Environment"]` / `== ["Globals", "Environment"]`
(and rename it to `test_scopes_per_frame` so its name stops lying).
The new `test_outer_frames_get_globals_and_environment` above then
duplicates it — keep only ONE: apply the rename+update and skip adding
the duplicate test.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/integration/test_bash_adapter_inspection.py -v`
Expected: new/updated scope-list assertions FAIL (no "Environment").

- [ ] **Step 3: Implement the server change**

In `_on_scopes`, after the Globals append:

```python
        scopes.append(
            {
                "name": "Environment",
                "expensive": False,
                "variablesReference": self._add_ref(("scope", "environment")),
            }
        )
```

In `_on_variables`, replace the two-way scope dispatch with:

```python
        if entry[0] == "scope":
            if entry[1] == "locals":
                vars_ = await self.session.locals()
            elif entry[1] == "environment":
                vars_ = await self.session.environment_vars()
            else:
                vars_ = await self.session.globals_vars()
            body = [self._var_to_dap(v) for v in vars_]
```

- [ ] **Step 4: Run the bash test set**

Run: `uv run pytest tests/integration -k bash -v && uv run pytest tests/unit -k bash -q`
Expected: all PASS.

- [ ] **Step 5: README line**

In README's bash subsection (near the limitations list added by the
bash-dap work), add one line to the description of what the Variables
view shows:

```markdown
The Variables view shows three scopes for bash: Locals (innermost frame
only), Globals (unexported shell variables), and Environment (exported
variables — inherited and script-`export`ed alike).
```

- [ ] **Step 6: Full suite + lint, commit**

Run: `uv run pytest tests -q && uv run ruff check src tests && uv run ruff format --check src tests`
Expected: all green.

```bash
git add src/tdb/adapters/bash/server.py README.md \
        tests/integration/test_bash_adapter_inspection.py
git commit -m "feat: Environment scope in the bash Variables view"
```

---

## Deviations

Follow the spec (`2026-08-08-bash-env-scope-design.md`) where this plan
and the spec disagree, and update whichever is wrong. Existing fixture
line numbers are load-bearing — never edit an existing fixture in place.

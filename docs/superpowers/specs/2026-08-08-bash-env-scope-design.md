# Bash Environment Variables Scope — Design

**Date:** 2026-08-08
**Branch:** `bash-dap`
**Status:** Approved
**Extends:** 2026-08-08-bash-dap-design.md (bash DAP support, complete)

## Goal

Add a third tree, **Environment**, to the Variables view when debugging
bash — alongside Locals and Globals.

## Semantics: strict split

In bash, "environment variable" means "exported variable" (`declare -x`).
The three scopes partition the visible variables with no duplication
**(revised during implementation): for top-level variables — see the
qualification below the scope table)**.

| Scope | Contents | Frames | Filter |
|---|---|---|---|
| Locals | `local -p` of the stopped frame | innermost only (unchanged) | none |
| Globals | **unexported** shell variables | all frames | `_INTERNAL_VARS` (unchanged) |
| Environment | **exported** variables — inherited (`PATH`, `HOME`, …) and the script's own `export`s alike | all frames | only `__tdb_*` / `__TDB_*` prefixes |

Consequences of the strict split:

- A script's `FOO=1` appears under Globals; its `export BAR=2` appears
  under Environment; nothing appears twice (top-level variables — see
  the "(revised during implementation)" qualification below).
- `PATH`, `HOME`, `PWD`, `TERM`, `_` etc. deliberately SHOW under
  Environment — surfacing the real environment is the feature. The
  `_INTERNAL_VARS` filter does NOT apply to the Environment scope.
- The harness's own exported control variables (`__TDB_CMD_FD`,
  `__TDB_RESP_FD`, `__TDB_TMP`) are hidden by the prefix filter.
- The launch-snapshot comparison added by the final-review fix wave
  (commit 3db145d: `_launch_env_snapshot`, `_unquote_declare_scalar`,
  and the name+value diff in `globals_vars()`) is **retired**: inherited
  environment is by definition exported, so under the strict split it
  can never reach Globals. That machinery and its now-obsolete test are
  removed/reworked, not left as dead code.

**(revised during implementation):** the final-review fix wave brought
the snapshot back, but purely as a display annotation, not a filter —
the strict split above is unchanged; membership between Globals and
Environment never depends on the snapshot. `BashSession.launch()`
snapshots the exact child env it passes to the subprocess
(`_launch_env_snapshot`). `environment_vars()` marks an entry "touched"
when its name is new since launch or its value differs (comparison
reverses `declare -p`'s quoting via `_unquote_declare_scalar`, which now
errs toward TOUCHED on anything unparseable); arrays/assoc arrays are
always touched. Touched values are rendered with a `"* "` prefix and
the list is sorted touched-group-first, alphabetically within each
group — the rationale being that a `set -a` script's handful of own
vars would otherwise drown among ~80 inherited entries. See
`_is_touched()`/`environment_vars()` in `session.py`.

**(revised during implementation):** "no duplication" holds for
top-level variables, not universally. Bash's `local` is dynamically
scoped: a variable `local`'d in an outer live frame stays visible to
every frame it called into, unless a nested frame shadows the same
name. The harness's `globals` command runs bare `declare -p` (no
name — the "all visible variables" form) from deep inside the DEBUG
trap's own call chain, which is itself a callee of whatever debuggee
frame is stopped — so `declare -p` there sees not just true globals but
every live frame's locals too. Consequence: a `local -x FOO=1` in any
live frame is picked up by both `locals()` (it's a local of the
innermost frame) and `environment_vars()` (it's genuinely exported,
so it's really in bash's environment right now) — the two are not
disjoint here. Likewise a plain (non-`-x`) local declared in a live
*outer* frame (not the innermost one `locals()` reads) can appear in
both `locals()`, once that frame is the one stopped in, and
`globals_vars()`. The partition claim in the table above is exact only
for variables declared outside any function (top-level script scope).

## Mechanism

No harness, protocol, or wire changes. The existing `globals` command's
`declare -p` output already encodes exportedness in its flags field
(`declare -x NAME=...`); the parser's `_DECLARE_RE` already captures the
flags group and currently discards it.

- `declares.py`: `BashVar` gains `exported: bool = False` (defaulted —
  every existing construction remains valid). `parse_declares` sets it
  when the captured flags contain `x`.
- `session.py`: `globals_vars()` keeps only unexported vars (then
  `_INTERNAL_VARS`, as today). New `environment_vars() -> list[BashVar]`
  keeps only exported vars minus `__tdb_`/`__TDB_` prefixes. Snapshot
  machinery removed.
  **(revised during implementation):** `globals_vars()` and
  `environment_vars()` each issue their own `request("globals")` rather
  than sharing one cached payload — freshness over caching; scopes are
  fetched independently (a client may request one scope without ever
  requesting the other, e.g. outer frames or a UI that lazily expands
  trees), and a shared cache would need explicit invalidation on every
  resume to avoid serving a stale snapshot.
- `server.py`: `_on_scopes` — frame 0: Locals, Globals, Environment;
  outer frames: Globals, Environment. `_on_variables` dispatches the new
  `("scope", "environment")` ref kind to `environment_vars()`. Array /
  assoc-array children expand exactly as in the other scopes (exported
  arrays exist: `declare -ax`).

## Error handling

Nothing new: the scope rides the existing `globals` request, so its
failure modes (not stopped, protocol error) are already handled and
tested. An empty environment renders as an empty tree, not an error.

## Testing

- Unit (`tests/unit/test_bash_declares.py`): `declare -x E="v"` →
  `exported=True`; `declare --`/`-a`/`-A` without `x` → `False`;
  `declare -ax ARR=(...)` → exported array with children.
- Session (`tests/integration/test_bash_session.py`): a fixture (extend
  `bash_arrays.sh` or a new one) with an unexported var, a script-level
  `export`, and a launch-time inherited sentinel (passed via the `env`
  launch param). Assert: unexported → `globals_vars()` only; script
  export → `environment_vars()` only; inherited sentinel →
  `environment_vars()`; `PATH` present in `environment_vars()`; no
  `__TDB_*` anywhere; the two lists are disjoint. The obsolete
  snapshot-filter test (`test_globals_hide_untouched_inherited_env_but_
  show_script_vars`) is reworked to these semantics.
- DAP (`tests/integration/test_bash_adapter_inspection.py`): scopes for
  frame 0 = [Locals, Globals, Environment], outer frame = [Globals,
  Environment]; Environment's variables include the sentinel and
  support array-children expansion.

## Docs

One line in README's bash section: the Variables view shows Locals
(innermost frame), Globals (unexported), and Environment (exported)
scopes.

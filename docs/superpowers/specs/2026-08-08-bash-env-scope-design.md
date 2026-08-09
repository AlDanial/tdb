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
The three scopes partition the visible variables with no duplication:

| Scope | Contents | Frames | Filter |
|---|---|---|---|
| Locals | `local -p` of the stopped frame | innermost only (unchanged) | none |
| Globals | **unexported** shell variables | all frames | `_INTERNAL_VARS` (unchanged) |
| Environment | **exported** variables — inherited (`PATH`, `HOME`, …) and the script's own `export`s alike | all frames | only `__tdb_*` / `__TDB_*` prefixes |

Consequences of the strict split:

- A script's `FOO=1` appears under Globals; its `export BAR=2` appears
  under Environment; nothing appears twice.
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

## Mechanism

No harness, protocol, or wire changes. The existing `globals` command's
`declare -p` output already encodes exportedness in its flags field
(`declare -x NAME=...`); the parser's `_DECLARE_RE` already captures the
flags group and currently discards it.

- `declares.py`: `BashVar` gains `exported: bool = False` (defaulted —
  every existing construction remains valid). `parse_declares` sets it
  when the captured flags contain `x`.
- `session.py`: one `request("globals")` payload feeds both lists.
  `globals_vars()` keeps only unexported vars (then `_INTERNAL_VARS`, as
  today). New `environment_vars() -> list[BashVar]` keeps only exported
  vars minus `__tdb_`/`__TDB_` prefixes. Snapshot machinery removed.
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

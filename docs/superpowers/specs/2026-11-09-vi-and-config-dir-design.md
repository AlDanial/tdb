# `--vi` and `--config-dir` — design

Date: 2026-11-09
Branch: `vi-and-config-dir`

Two new command-line switches. They are independent but are meant to be
used together, so they ship on one branch. Suggested build order:
`--config-dir` first (small, touches startup plumbing `--vi` also uses),
then `--vi`.

## Why

The motivating moment: a stripped-down Debian Docker container with no
editor, no `apt install`, but a full Python where `pip install` works.
`tdb`'s Code View Edit mode (the built-in vim engine under
`src/tdb/vi/`) is a capable editor, and the user used it to edit
`.bashrc` and other files. Two things got in the way:

1. The edit pane was small; the debugger layout (console, variables,
   stack, breakpoints) wasted the terminal on a session with nothing to
   debug.
2. Containers like this often have a read-only `$HOME`, so `tdb` cannot
   write `~/.config/tdb`.

Success: `pip install textual-debugger && tdb --vi ~/.bashrc` behaves
like a minimal vim that uses the whole terminal, and
`--config-dir /tmp/tdbcfg` lets every part of `tdb` run normally when
`$HOME` is read-only.

## Part 1 — `--config-dir DIR`

### Behavior (decided by the user)

- Today the config directory is `~/.config/tdb` (Unix) or
  `%APPDATA%\tdb` (Windows), computed by `persist._default_config_dir()`
  and frozen into module constants (`CONFIG_DIR`, `STATE_FILE`,
  `CONFIG_FILE`, `_LEGACY_STATE_FILE`) at import time.
- `--config-dir DIR` replaces that directory **on every platform**.
  Files go **directly in DIR**: `DIR/config.json`, `DIR/breakpoints.json`,
  `DIR/tdb.log`, `DIR/padwalker/`, and the debug-server auth files that
  `server/auth.py` keeps under `CONFIG_DIR`. No `.config/tdb` layer is
  added under DIR.
- Purpose: `tdb` functions normally when `$HOME` is read-only but DIR
  is writable.

### Calls made by Claude

- **Environment variable `TDB_CONFIG_DIR`** carries the same setting.
  `--config-dir` sets it in `os.environ` at startup so every `tdb`
  subprocess that `tdb` spawns (exception hook, `tdb.breakpoint()`
  hooks, server/remote-attach children, `--run` handoff into the TUI,
  etc.) inherits it. Users may also set `TDB_CONFIG_DIR` directly
  instead of passing the flag. Precedence: flag > env var > platform
  default.
- **`TDB_LOG_DIR` keeps priority over the config dir for the log file**
  (tests rely on it); otherwise the log lives in DIR.
- **Missing DIR is created** (`mkdir(parents=True, exist_ok=True)`).
  If it cannot be created or is not writable, `tdb` warns once on
  stderr and keeps running with the same OSError tolerance the
  read-only-filesystem work already established (no crash, writes
  skipped).
- **Path handling:** DIR is `expanduser()`'d and made absolute against
  the shell cwd at parse time, so children spawned with a different
  cwd still resolve it.
- `--info` reports the directory actually in use (and says when it
  came from `--config-dir`/`TDB_CONFIG_DIR`). `--config write-default`
  and `--config install-padwalker` operate in DIR.
- `--config-dir` combines with every other mode, including `--vi`.

### Tools `tdb` drives (decided by the user: point all of them at DIR)

The goal is that debugging in **every supported language** works with a
read-only `$HOME`, not only that `tdb`'s own files move. So the
environment `tdb` gives to adapters, debuggers, debuggees, and build
steps redirects their **write** locations under DIR (Claude's call:
a `DIR/tools/<tool>/...` subtree, e.g. `DIR/tools/go/cache`).

Rules (Claude's calls):

- Redirect only places tools **write** — build caches, history files,
  logs, state. Never relocate places tools **read their installation
  from** (`GOROOT`, `RUSTUP_HOME`, an existing `CARGO_HOME` with its
  toolchains/bin, `GOPATH/bin`, opam switches, gem install dirs), or
  the toolchain stops being found.
- A variable the user already set in their environment always wins;
  `tdb` only fills in ones that are unset.
- Only applied when `--config-dir`/`TDB_CONFIG_DIR` is in effect;
  without it, child environments are unchanged from today.
- Applied in one helper used by every place that builds a child
  environment (each adapter's launch, `--terminal` launches, build
  steps such as Go/Rust/OCaml/C compiles), not per call site.

Starting inventory for the builder to verify and complete by checking
each adapter under `src/tdb/adapters/` and `src/tdb/languages/`:

| Tool | Likely write locations | Candidate redirect |
|------|------------------------|--------------------|
| Go / dlv | `~/.cache/go-build`, `~/go/pkg/mod`, `~/.config/dlv` | `GOCACHE`, `GOMODCACHE` (and `GOPATH` only if unset and no `~/go` exists), `XDG_CONFIG_HOME` for dlv |
| Rust / cargo | `~/.cargo/registry`, build `target/` (in project) | `CARGO_HOME` only if `~/.cargo` doesn't hold the toolchain; otherwise leave it and accept project-local writes |
| gdb | `~/.gdb_history`, `~/.cache/debuginfod_client` | `GDBHISTFILE`, `XDG_CACHE_HOME` / `DEBUGINFOD_CACHE_PATH` |
| lldb-dap | `~/.lldb/`, `~/.cache/lldb` | `XDG_CACHE_HOME`; check whether lldb honors anything for `~/.lldb` |
| Ruby / rdbg | `~/.rdbg_history`, `~/.gem` | `RUBY_DEBUG_HISTORY_FILE`, `GEM_SPEC_CACHE` |
| PowerShell / PSES | `~/.local/share/powershell`, `~/.cache/powershell`, PSES logs | `XDG_DATA_HOME`, `XDG_CACHE_HOME`, PSES log path argument |
| OCaml / earlybird | opam/dune caches | `XDG_CACHE_HOME`, `DUNE_CACHE_ROOT` |
| Python / debugpy / pip | `~/.cache/pip`, pydevd logs | `XDG_CACHE_HOME` / `PIP_CACHE_DIR` |
| Perl, bash, tcsh | none known beyond `tdb`'s own PadWalker cache | verify |

Setting `XDG_CACHE_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME`, and
`XDG_CONFIG_HOME` (when unset) to subdirs of DIR covers many tools at
once; the per-tool variables cover the ones that ignore XDG. Windows:
the same rule applies with `%LOCALAPPDATA%`-based tool defaults;
redirect via each tool's own variable, since XDG variables don't apply.

Testing for this part: a test per language that runs a minimal debug
session with `$HOME` pointed at a read-only directory and
`--config-dir` at a writable one, skipped where the toolchain isn't
installed (same skip pattern the language suites already use).

### Left to the builder

- How to make the frozen module constants honor the override. Options
  include resolving the directory lazily via a function, or having
  `cli.main()` set the env var before anything imports `tdb.persist`.
  Whatever is chosen must work for code paths that import `persist`
  early and for subprocess children. Audit every `CONFIG_DIR` /
  `STATE_FILE` / `CONFIG_FILE` user (`persist.py`, `info.py`, `cli.py`,
  `adapters/perl/padwalker.py`, `server/auth.py`) and any test fixtures
  that monkeypatch them.
- Windows verification: an explicit DIR on Windows must bypass
  `%APPDATA%` entirely; config-backup filename rules (dots instead of
  colons) are unchanged.

## Part 2 — `--vi FILE`

### Behavior (decided by the user)

- `tdb --vi FILE` turns `tdb` into a vim-style editor for FILE. No
  program is launched, no debug adapter is started.
- **All other switches are ignored**, except `--config-dir` (see below).
  Ignored means: not acted on and not validated (e.g. `tdb --vi f -k x`
  or `--vi` with `--timing` must not produce argparse errors from the
  debugging-switch validation in `parse_args`).
- **Exactly one file.**
  - `tdb --vi` with no file: print an error like
    `tdb: --vi expects a file name` and exit non-zero.
  - More than one file: error and exit non-zero. (Multi-file needs
    `:n`, which the vi engine does not implement; multi-file is out of
    scope until it does.)
  - A directory: error and exit non-zero (unlike vim/nvim, no
    directory browsing).
  - A file that does not exist: open an empty buffer, vim-style. The
    file appears on disk only at the first `:w`; quitting without
    saving leaves nothing behind.
- **Layout:** the Code View takes the whole terminal. No Console,
  Variable, Stack, Breakpoint, or Evaluate Console views.
- **Menu bar:** only **File** and **Help**. (The user first listed File,
  Source, Help, then dropped Source: there is no program, so a module
  picker has no meaning.) No Edit, Configure, Threads, Processes, or
  Async Tasks entries.
- **Starts in Edit mode, vim normal mode**, regardless of the
  `keybindings` scheme in `config.json` (the vim engine is forced).
- **Ctrl+F / Ctrl+B page forward / back.** Today Ctrl+B is an app
  binding (`focus_breakpoints`, `app.py`) and is in `_VI_TDB_KEYS`
  (`widgets/code_editor.py`), so it never reaches the vi engine; under
  `--vi` both keys must page in normal mode.
- **Quitting:**
  - `:q` exits `tdb` back to the shell. With unsaved changes it keeps
    the existing Save / Discard / Cancel dialog (not vim's E37
    refusal); Save then exits, Discard exits, Cancel stays.
  - `:q!` exits without saving.
  - `:wq` / `:x` save and exit.
  - `:w` saves and stays.
  - Today these leave Edit mode and return to Code View; under `--vi`
    leaving Edit mode means exiting the app. Every other exit path
    (Ctrl+Q / Ctrl+C or whatever the app binds to quit, and the
    "Discard and Exit Edit Mode" path if reachable) must apply the
    same unsaved-changes rule: dialog unless explicitly discarding.
- **File menu:** opens the file picker showing **all** files (no
  language-extension filter, no "this session debugs X" check).
  Picking a file switches the editor to it; if the current buffer has
  unsaved changes, the same Save / Discard / Cancel dialog runs first.
  Picking a directory or an unreadable file is refused with a
  notification.
- **Help menu:** unchanged (Documentation, About).
- **`--config-dir`** is honored together with `--vi` (the read-only
  `$HOME` container is the main use case for both).

### Calls made by Claude

- Files Edit mode already refuses (non-UTF-8, binary per issue #54)
  are refused by `--vi` up front with a stderr message and non-zero
  exit, rather than opening a TUI that cannot edit.
- Syntax highlighting uses the existing lexer-by-filename logic;
  extension-less files like `.bashrc` get whatever that logic yields.
- **Saving (decided by the user: fall back to direct write).** Saving
  tries the existing atomic write first (`atomic_write_text` in
  `source_edit.py`: temp file in the target's directory, then
  `os.replace`). If that fails because the directory is not writable
  or the target cannot be replaced (EACCES/EPERM on the temp file,
  EBUSY/EXDEV from `os.replace` — e.g. Docker bind-mounted
  `/etc/hosts`, `/etc/resolv.conf`, or a writable `~/.bashrc` in a
  read-only `$HOME`), it falls back to opening the target itself and
  truncating/writing it in place, as vim does. Any leftover temp file is
  removed. Only if the direct write also fails does it notify and keep
  the buffer dirty. Claude's call: the fallback lives in the shared save
  path, so ordinary (non-`--vi`) Edit mode gets it too. File mode bits
  and ownership are kept by the direct write naturally; the atomic path
  keeps its current behavior.
- `--vi` writes nothing to `breakpoints.json`; theme and other config
  are read normally from the config dir.
- **Esc never exits.** Today Esc in vim normal mode with nothing pending
  posts `CodeEditor.LeaveRequested` (leave Edit mode). Under `--vi` that
  would quit on a habitual Esc, so Esc in normal mode is a no-op, as in
  vim. Ctrl+C is likewise a vim-style no-op/cancel, not a quit. The quit
  paths are exactly `:q`, `:q!`, `:wq`, `:x`, and the app's Ctrl+Q; all
  of them follow the unsaved-changes rule above.
- **All of `_VI_TDB_KEYS` go to the vi engine under `--vi`** (Ctrl+O,
  Ctrl+E, Ctrl+V, Ctrl+P, Ctrl+S as well as Ctrl+F/Ctrl+B), since the
  views they would focus don't exist. Ctrl+Q stays the app quit key.
  The parked Ctrl+V-in-normal-mode residual is not in scope to fix.

### Left to the builder

- Whether `--vi` is a mode flag on the existing `TdbApp` or a small
  dedicated `App` that reuses `CodeView`/`CodeEditor`. `TdbApp` is
  built around a debug controller and language profile; a dedicated
  app may be much simpler. Choose whichever keeps the debugger code
  paths untouched and avoids spawning any adapter or subprocess.
- How `--vi` short-circuits in `parse_args` (it must run before the
  debugging-switch validation) and how it coexists with the
  positional program/argument parsing.
- Whether Edit mode's "leave Edit mode" internals are reused with an
  exit hook or bypassed.

## Non-goals

- `:n`, `:e FILE`, `:w FILE`, buffers, splits, tabs.
- Directory browsing.
- Making `tdb` installable as a `vi`/`vim` command name.
- Any debugging feature inside `--vi`.

## Anti-goals (failure even if it "works")

- `--vi` spawning a debug adapter, debuggee, or another `tdb` process.
- `:q` silently discarding unsaved changes.
- `--config-dir` honored by the parent `tdb` but not by `tdb`
  processes it spawns, so state splits between two directories or
  children fail on the read-only `$HOME`.
- Any write under `$HOME` when `--config-dir` is given, by `tdb` or
  by the tools it runs.
- Redirecting a tool's install/lookup location so the toolchain is no
  longer found.

## Testing

`uv run pytest` must pass (use the memcap wrapper for full-suite runs).
Cover at least: `--config-dir` routing of every config-dir file
including env inheritance by a spawned child, unwritable DIR warning,
Windows path branch (monkeypatched `sys.platform`); `--vi` argument
errors (none / many / directory), non-existent file created only on
`:w`, Ctrl+F/Ctrl+B paging, `:q` dialog with dirty buffer, `:q!`,
`:wq`, File-menu switch with dirty buffer, menu bar contents, Esc in
normal mode not exiting, save fallback (writable file in a read-only
directory; `os.replace` raising EBUSY via monkeypatch), and that no
adapter/subprocess is started.

# Configuration

On UNIX-like systems (Linux, macOS, FreeBSD, etc.),
`tdb` stores configuration and breakpoints in `~/.config/tdb/`.
On Windows, it uses `%APPDATA%\tdb\`.

| File | Contents |
|------|----------|
| `config.json` | User preferences (keybinding scheme, color theme, step mode, adapter overrides) |
| `breakpoints.json` | Breakpoints from previous sessions, keyed by project directory |

## Read-only home directory

If the default location is not writable (for example a read-only home
directory), point `tdb` somewhere else with `--config-dir DIR` or the
`TDB_CONFIG_DIR` environment variable. The flag takes precedence over the
variable, a relative `DIR` is resolved against the current directory, and
`DIR` is created if it does not exist. Child processes started by `tdb`
inherit the setting. `tdb --info` shows the active directory.

The tools `tdb` runs (debug adapters, debuggers, build steps) and the
debuggee also write caches and history files under `$HOME` by default.
Under `--config-dir`/`TDB_CONFIG_DIR`, `tdb` points those write
locations into `DIR/tools`, for itself and every child process. Each
variable is set only when it is unset or empty; one you set yourself
always wins:

| Variable | Set to | Used by |
|---|---|---|
| `XDG_CACHE_HOME` | `DIR/tools/cache` | Go build cache default, pip, pwsh, debuginfod, gdb/lldb caches |
| `XDG_STATE_HOME` | `DIR/tools/state` | rdbg history default |
| `PIP_CACHE_DIR` | `DIR/tools/pip` | pip (XDG does not apply on Windows/macOS) |
| `GOCACHE` | `DIR/tools/go/build` | `go build` (run by dlv) |
| `GOMODCACHE` | `DIR/tools/go/mod` | Go module downloads; only when `GOPATH` is unset and `~/go/pkg/mod` does not exist, so an existing module cache keeps working offline |
| `GDBHISTFILE` | `DIR/tools/gdb/history` | gdb history (`set history save on`) |
| `DEBUGINFOD_CACHE_PATH` | `DIR/tools/gdb/debuginfod` | debuginfod client used by gdb |
| `RUBY_DEBUG_HISTORY_FILE` | `DIR/tools/ruby/rdbg_history` | rdbg history |
| `DUNE_CACHE_ROOT` | `DIR/tools/ocaml/dune` | dune shared cache |

Locations a tool reads its installation or your settings from (`HOME`,
`XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `GOPATH`, `CARGO_HOME`, `GEM_HOME`,
`OPAMROOT`, ...) are never changed.

Known limitation: Go telemetry still writes to `~/.config/go/telemetry`
when `$HOME` is writable. When `$HOME` is read-only that write fails
harmlessly and the build goes on.

## Sample `config.json`

Every key `tdb` reads, with its default or a representative value.
`tdb --config write-default` writes a fresh file with these defaults
(moving the old one aside as `config.json-YYYY-mm-DD-HH:MM:SS`), or copy
this to restore a deleted or corrupted file, then delete the entries you
don't need: a missing key takes its default, an unknown key is ignored,
and an invalid value (say a misspelled `step_mode`) falls back to the
default instead of stopping `tdb`.

```json
{
  "keybindings": "vim",
  "theme": null,
  "step_mode": "statement",
  "adapters": {
    "gdb": "/usr/bin/gdb",
    "lldb-dap": null,
    "dlv": "/home/me/go/bin/dlv",
    "perl": "/usr/bin/perl",
    "rdbg": "/usr/local/bin/rdbg",
    "bash": "/usr/bin/bash",
    "tcsh": "/usr/bin/tcsh",
    "pwsh": "/usr/bin/pwsh",
    "pses": "/home/me/.local/share/PowerShellEditorServices",
    "ocamlearlybird": "/home/me/.opam/default/bin/ocamlearlybird"
  },
  "default_adapters": {
    "cpp": "gdb",
    "rust": "gdb",
    "ocaml": "lldb-dap"
  }
}
```

| Key | Values | Meaning |
|-----|--------|---------|
| `keybindings` | `"vim"` (default), `"emacs"`, `"default"` | Code View keybinding scheme; also set by `--keybindings` |
| `theme` | `null` (default) or a Textual theme name such as `"textual-dark"`, `"textual-light"`, `"nord"`, `"gruvbox"`, `"dracula"`, `"monokai"`, `"tokyo-night"`, `"catppuccin-mocha"` | Color theme; the names offered under **Configure > Theme** are the valid ones, and an unknown name is ignored |
| `step_mode` | `"statement"` (default), `"line"` | How `n`/`s` treat multi-line statements (see **Step mode** below) |
| `adapters` | adapter id → executable path, or `null` | Where to find a debugger or interpreter instead of searching `PATH`. Ids: `gdb`, `lldb-dap`, `dlv` (debuggers); `perl`, `rdbg`, `bash`, `tcsh`, `pwsh` (interpreters `tdb` spawns); `pses` (the PowerShell Editor Services module directory); `ocamlearlybird`. Only list the ones you need |
| `default_adapters` | language id → adapter id | Which adapter a language uses when `--adapter` isn't given. Languages with a choice: `cpp` and `rust` (`gdb` or `lldb-dap`), `ocaml` (`lldb-dap`, `gdb`, or `ocamlearlybird`) |

The file is plain JSON: no comments, no trailing commas. `tdb` rewrites it
when you change a setting from the **Configure** menu, so hand edits made
while `tdb` is running may be overwritten.

Adapter-related keys in `config.json`: `adapters` maps an adapter id to an
executable path (`{"adapters": {"lldb-dap": "/opt/llvm/bin/lldb-dap"}}`), and
`default_adapters` picks a language's default adapter
(`{"default_adapters": {"cpp": "lldb-dap"}}`). For a one-off run,
`--adapter /path/to/gdb` (or `lldb-dap`, `dlv`) overrides the `adapters` entry
without editing the file; `tdb --info` reports the `gdb` and `lldb-dap`
currently resolved (config override first, then `PATH`) with their versions.

When `tdb` creates `config.json` for the first time it seeds `adapters` with
`gdb` and `lldb-dap` entries pointing at the executables found on `PATH`
(`null` when one isn't installed) so the keys are there to edit. A `null`
entry behaves like a missing one: `tdb` falls back to `PATH`.

**Perl is a special case:** `perl-tdb` is tdb's own bundled adapter (always
found; it's Python code, not an external executable), so
`adapters.perl` doesn't select an adapter binary. Instead it names the
*Perl interpreter* tdb should spawn to run the debuggee:
`{"adapters": {"perl": "/path/to/perl"}}`. Use this when the `perl` on
`PATH` is too old (< 5.18) or you need a specific `perlbrew`/`plenv` version.

**PowerShell** follows the same pattern with two keys: `adapters.pwsh` names
the interpreter and `adapters.pses` names the PowerShell Editor Services
module directory (see [PowerShell](../languages/powershell.md)).

Breakpoints are saved on exit and restored when debugging a program in the same
directory. Each project's breakpoints are independent. Breakpoints set with
`-t`/`--to-line` are the exception: they behave like `-k` breakpoints during
the session but are never saved. A `--if` condition on a `-k` breakpoint is
saved along with it.

**Step mode** (`step_mode` in `config.json`) controls how `n` (step over) and `s` (step
into) handle multi-line source statements:

| Value | Behavior |
|-------|----------|
| `"statement"` (default) | A multi-line statement (e.g. a `gather(...)` call spanning five lines) is one step. The debugger keeps issuing DAP steps until execution leaves the statement, then stops on the next logical line. |
| `"line"` | debugpy's native per-line behavior (stops on every physical line, including each interior sub-line of a multi-line expression)|

Change it from the menu (**Configure > Step Mode**); the choice is saved immediately and
applies to all future sessions. Breakpoint hits, exceptions, and pauses always interrupt
a statement step, so a breakpoint set on a sub-line of a multi-line expression still
fires as expected.

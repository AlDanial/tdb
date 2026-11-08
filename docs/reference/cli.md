# CLI Reference

```
usage: tdb [-h] [-v] [-r [HOST:]PORT] [--cwd CWD] [--no-stop-on-entry]
           [--no-just-my-code] [--no-subprocess] [--python PYTHON] [--pv]
           [--lang LANGUAGE] [--adapter ADAPTER] [--source FILE]
           [--keybindings {default,vim,emacs}]
           [--terminal {xterm,konsole,gnome-terminal,ghostty,kitty,iterm2,warp,wezterm,terminator}]
           [--local-root PATH] [--remote-root PATH]
           [--server] [--headless] [-k FILE:LINE|LINE] [--server-port SERVER_PORT] [--insecure-no-auth] [-d] [--doc-text] [--info]
           [--config {write-default,install-padwalker}]
           [program] [args ...]
```

| Flag | Description |
|------|-------------|
| `-r HOST:PORT` | Attach to a remote debugpy server |
| `-a`, `--attach PID` | Attach to a running local process by pid: [Python 3.11+](../python/attach-to-process.md), Go (Delve), or a native C/C++, Fortran, Rust, or OCaml program (gdb or lldb-dap). Linux identifies the language from `/proc/PID/exe`; elsewhere pass `--lang` and, for native programs, the executable path. |
| `--local-root PATH` | Local directory containing a copy of remote code (repeat to mirror multiple trees). Pair with `--remote-root`; counts must match. Required when `-k` sets a breakpoint against a remote debuggee whose code lives at a different path. |
| `--remote-root PATH` | Remote directory matched to `--local-root` (same CLI position via `zip()`). |
| `-k`, `--breakpoint FILE:LINE|LINE` | Set a breakpoint (may be repeated). Passing `-k` implies `--no-stop-on-entry` so the program runs straight to the first breakpoint. |
| `-t`, `--to-line FILE:LINE|LINE` | Like `-k`, but the breakpoint is not saved to the breakpoints file, it just takes you to that spot in the code for this session (may be repeated). |
| `--if EXPR` | Make the `-k`/`-t` breakpoint just before it conditional: the program stops there only when `EXPR` is true, e.g. `-k module.py:27 --if "a > 7"`. `EXPR` uses the debug adapter's expression syntax for the program's language. At most one `--if` per breakpoint; not supported for Tcsh. |
| `--no-stop-on-entry` | Do not pause at the first line (default: stop on entry; automatic when `-k` is given) |
| `--cwd DIR` | Working directory for the debuggee |
| `--source FILE` | Add files to the Source picker from a UTF-8 file containing one filename per line. Relative paths are resolved against the directory where `tdb` is launched. Supplements automatically discovered local Python sources. |
| `--python PATH` | Python interpreter for the debuggee (Python targets only) |
| `--pv` | Shorthand for --python .venv/bin/python |
| `--lang LANGUAGE` | Debuggee language (`python`, `cpp`, `perl`); default: auto-detect from the target |
| `--adapter ADAPTER` | Debug adapter within the language (e.g. `--lang cpp --adapter lldb-dap`), or a full path to a `gdb`/`lldb-dap`/`dlv` executable to use instead of the one on `PATH`; default: the language's standard adapter |
| `--no-just-my-code` | Step into stdlib/site-packages code instead of skipping it
  (default: skipped). On uncaught exceptions, the crash modal always shows the full traceback
  including library frames, regardless of this flag. |
| `--no-subprocess` | Disable debugpy's subprocess tracking (use when debugging `tdb` itself) |
| `--terminal TERM` | Run debuggee in the named external terminal: `xterm`, `konsole`,
  `gnome-terminal`, `ghostty`, `kitty`, `iterm2`, `warp`, `wezterm`, or `terminator` |
| `--keybindings SCHEME` | `vim`, `emacs`, or `default` (Notepad-style editing); saved to config |
| `--server` | Enable JSON-RPC server alongside TUI |
| `--headless` | JSON-RPC server only, no TUI |
| `--server-port PORT` | Server port (default: 8150) |
| `--insecure-no-auth` | Let the JSON-RPC server accept requests without its bearer token (see [JSON-RPC Server Mode](../automation/json-rpc.md#authentication)) |
| `--record FILE` | Record this TUI session's debugging actions to FILE (JSON-RPC lines) |
| `--replay FILE` | Replay a recording headless, printing a transcript (no program argument) |
| `--replay-tui FILE` | Replay a recording inside the TUI at the recorded pace, so you can watch it |
| `--replay-quiet` | With `--replay-tui`: no per-action toasts (errors and the final summary still show) |
| `--timing` | With `--replay`: reproduce the recorded pacing (`--replay-tui` always does) |
| `--replay-interval S` | With `--replay`/`--replay-tui`: fixed S-second delay before each action instead of the recorded gaps |
| `--replay-timeout S` | With `--replay`/`--replay-tui`: per-action stop-wait timeout (default 30) |
| `--examine-log DEST` | With --run: write each Ctrl-\ / SIGUSR2 stack snapshot to DEST (- = stdout; repeatable) |
| `--doc` | Display this documentation in a terminal Markdown viewer and exit |
| `--doc-text` | Print this documentation to stdout as wrapped plain text and exit |
| `--info` | Print the About text, file locations, and the path and version of each interpreter and debugger `tdb` would use, then exit |
| `--config write-default` | Rename the existing `config.json` to `config.json-YYYY-mm-DD-HH:MM:SS` (`HH.MM.SS` on Windows, which forbids colons in filenames) and write a new one holding every default value, then exit |
| `--config install-padwalker` | Build and cache the bundled PadWalker Perl module now, for the perl `tdb` would debug with (`adapters.perl` in `config.json`, else `perl` on `PATH`), instead of waiting for the first Perl launch; exits 1 if it cannot be built |

## Source files

Click **Source** beside **File**, or press **Alt+S**, to browse files relevant
to the current program. The picker shows a flat list with the debuggee source
file first, followed by the remaining files sorted by path. Files beneath the
launch directory appear as `./path/to/file`; external files use absolute paths.
Use Up/Down or j/k and Enter to open a file in Code View. Escape closes the picker. Selecting a source file leaves the
debug target and selected stack frame unchanged; unsaved edits prompt you
to save, discard, or cancel navigation.

For Python, `tdb` reads the entry script's imports and recursively follows
matching modules in the script directory and debuggee working directory.
Packages, local namespace packages, relative imports, aliases, and package
initializers are supported. Discovery reads saved files each time the picker
opens and never executes imports. It does not search installed libraries or
`PYTHONPATH`, resolve dynamic imports, or correct misspelled module names.
Files with syntax errors remain selectable, but their imports are not followed.

To include other files, create a UTF-8 list such as `sources.txt`:

```text
# Additional source files
src/helper.py  # Local helper
../shared/common.py

/opt/project/library/util.py
```

Then run `tdb --source=sources.txt main.py`. List entries supplement automatic
discovery, and listed Python files are also scanned in Python sessions.
Relative entries resolve from the directory where `tdb` is launched, regardless
of where `sources.txt` is stored or the debuggee working directory (`--cwd`). Everything from the first `#` to the end of each line is ignored,
including full-line comments. Remaining whitespace is trimmed and blank lines
are skipped. Paths have no glob or shell expansion; filenames containing `#`
cannot be listed. Duplicate paths appear once. An unreadable list
file is a command-line error; unavailable individual files produce warnings.

For other languages, Source shows an available source entry file plus the
explicit list, without automatic dependency discovery. Explicit files also
work in attach sessions where no entry script is known.

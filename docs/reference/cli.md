# CLI Reference

```
usage: tdb [-h] [-v] [-r [HOST:]PORT] [--cwd CWD] [--no-stop-on-entry]
           [--no-just-my-code] [--no-subprocess] [--python PYTHON] [--pv]
           [--lang LANGUAGE] [--adapter ADAPTER]
           [--keybindings {default,vim,emacs}]
           [--terminal {xterm,konsole,gnome-terminal,ghostty,kitty,iterm2,warp,wezterm,terminator}]
           [--local-root PATH] [--remote-root PATH]
           [--server] [--headless] [-k FILE:LINE|LINE] [--server-port SERVER_PORT] [-d] [--doc-text] [--info]
           [--config {write-default,install-padwalker}]
           [program] [args ...]
```

| Flag | Description |
|------|-------------|
| `-r HOST:PORT` | Attach to a remote debugpy server |
| `-a`, `--attach PID` | Attach to a running local process by pid: Go (Delve) or a native C/C++, Fortran, Rust, or OCaml program (gdb or lldb-dap). Linux identifies the language from `/proc/PID/exe`; elsewhere pass `--lang` and, for native programs, the executable path. |
| `--local-root PATH` | Local directory containing a copy of remote code (repeat to mirror multiple trees). Pair with `--remote-root`; counts must match. Required when `-k` sets a breakpoint against a remote debuggee whose code lives at a different path. |
| `--remote-root PATH` | Remote directory matched to `--local-root` (same CLI position via `zip()`). |
| `-k`, `--breakpoint FILE:LINE|LINE` | Set a breakpoint (may be repeated). Passing `-k` implies `--no-stop-on-entry` so the program runs straight to the first breakpoint. |
| `-t`, `--to-line FILE:LINE|LINE` | Like `-k`, but the breakpoint is not saved to the breakpoints file, it just takes you to that spot in the code for this session (may be repeated). |
| `--no-stop-on-entry` | Do not pause at the first line (default: stop on entry; automatic when `-k` is given) |
| `--cwd DIR` | Working directory for the debuggee |
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

# PowerShell

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

- Entry stop lands on the first statement of your script (comments and
  function definitions are skipped): PSES has no native `stopOnEntry` for
  this launch shape, so tdb sets a synthetic breakpoint on the bundled
  launcher, steps in, and clears it — no line-1 breakpoint appears in the
  Breakpoint view.
- The script runs inside that small bundled launcher (`tdb_launch.ps1`) so
  tdb can report the script's exit code. In the Stack view, top to bottom:
  PSES's `<Breakpoint>` marker frame (the frame to inspect — it tracks the
  accurate current position), your script's own frames, the launcher's
  `<ScriptBlock>`, and a source-less `Interactive Session` frame at the
  bottom.
- Stepping is statement-granular, as in VS Code: a line containing a
  `$( ... )` subexpression takes two `next` presses to clear, and `stepIn`
  first lands on the called function's declaration line before its body.
- An uncaught terminating error ends the script with exit code 1 and opens
  the fatal-error modal from pwsh's concise error text. Non-terminating
  errors (`Write-Error`, failing cmdlets without `-ErrorAction Stop`) print
  the same concise error block to the Console as stderr and let the script
  continue with exit code 0, as in pwsh itself; there is no break-on-error
  yet. The error block names the launcher's `& $Script` line rather than
  your script's actual line for some error kinds (a known cosmetic
  limitation).
- The fatal-error modal is gated on a non-zero exit code, since pwsh prints
  the identical block for terminating and non-terminating errors. A script
  that reports a non-terminating error and then `exit 3` can therefore still
  raise the modal, showing the last error block that names a file of yours;
  blocks attributed to tdb's own `tdb_launch.ps1` are skipped, so a bare
  `Write-Error` before the `exit` opens no modal at all.
- `--terminal` and remote attach are not supported for PowerShell yet.
- `Read-Host`, `$Host.UI.ReadLine()`, and `[Console]::ReadLine()` read
  from the Console view's stdin line (pwsh is no longer started with
  `-NonInteractive`, so mandatory-parameter and `-Confirm` prompts now wait
  for your input there instead of failing). pwsh echoes the prompt and the
  answer together once the line completes, so a bare `Read-Host` prompt
  appears only after you answer it; a preceding `Write-Host` shows at once.
- Windows PowerShell 5.1 is not supported (pwsh 7 only). Running tdb *on*
  Windows against pwsh is designed for but not yet verified (experimental).

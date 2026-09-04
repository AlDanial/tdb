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

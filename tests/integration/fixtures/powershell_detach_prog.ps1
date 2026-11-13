param([string]$Mode = "")
$ErrorActionPreference = "Stop"
[Console]::Out.Flush()
Write-Host ("before-" + (@("ran") -join ""))
$x = 1    # breakpoint line
Write-Host ("after-" + (@("ran") -join ""))
if ($Mode -eq "loop") {
    try {
        Write-Host ("looping-" + (@("now") -join ""))
        while ($true) { Start-Sleep -Milliseconds 200 }
    } finally {
        Write-Host ("finally-" + (@("ran") -join ""))
    }
}
if ($Mode -eq "read") {
    Write-Host ("looping-" + (@("now") -join ""))
}
$line = Read-Host
Write-Host "got:$line"
exit 7

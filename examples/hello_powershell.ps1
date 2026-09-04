# tdb example: `tdb examples/hello_powershell.ps1`
# Set a breakpoint on the `return` line inside Square, step, and evaluate $n.
function Square($n) {
    $sq = $n * $n
    return $sq
}

$total = 0
foreach ($i in 1..5) {
    $total += Square $i
    Write-Host "i=$i total=$total"
}
Write-Error "a non-fatal error goes to the console too"
Write-Host "done: $total"

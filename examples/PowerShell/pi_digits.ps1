param(
    [Alias('r')][int]$Runs = 1,
    [Alias('d')][int]$Digits = 10,
    [Alias('p')][switch]$Print
)
if ($Runs -lt 1 -or $Digits -lt 1) { throw 'Counts must be positive.' }

function Get-PiDigits([int]$Count) {
    $size = [int][math]::Floor($Count * 10 / 3) + 1
    $a = [long[]]::new($size)
    for ($i = 0; $i -lt $size; $i++) { $a[$i] = 2 }
    $out = [System.Text.StringBuilder]::new()
    $previous = 0
    $pending = 0
    for ($step = 0; $step -le $Count; $step++) {
        [long]$q = 0
        for ($i = $size; $i -ge 1; $i--) {
            [long]$x = 10 * $a[$i - 1] + $q * $i
            $a[$i - 1] = $x % (2 * $i - 1)
            $q = [long][math]::Floor($x / (2 * $i - 1))
        }
        $a[0] = $q % 10
        $q = [long][math]::Floor($q / 10)
        if ($q -eq 9) { $pending++ }
        elseif ($q -eq 10) {
            [void]$out.Append([char]([int][char]'0' + $previous + 1))
            [void]$out.Append('0', $pending)
            $previous = 0; $pending = 0
        } else {
            [void]$out.Append([char]([int][char]'0' + $previous))
            [void]$out.Append('9', $pending)
            $previous = [int]$q; $pending = 0
        }
    }
    [void]$out.Append([char]([int][char]'0' + $previous))
    [void]$out.Append('9', $pending)
    return $out.ToString().Substring(1, $Count)
}

for ($run = 1; $run -le $Runs; $run++) {
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    $result = Get-PiDigits $Digits
    $watch.Stop()
    'Run {0,3}/{1,3} completed in {2:F6} seconds.' -f $run, $Runs, $watch.Elapsed.TotalSeconds
    if ($run -eq $Runs -and $Print) { $result }
}

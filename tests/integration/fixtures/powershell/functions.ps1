function Add($a, $b) {
    $s = $a + $b
    return $s
}
function Outer($v) {
    $r = Add $v 2
    return $r
}
$x = 1
Write-Host "args=$($args -join '|')"
$y = Outer $x
Write-Host "sum=$y"

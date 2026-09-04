# simple: the entry stop lands on line 2, the first executable statement
$x = 1
$y = $x + 1
Write-Host "sum=$y"
Write-Output "out=$y"
exit 7

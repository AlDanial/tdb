Write-Host "before"
$x = Read-Host "enter"
Write-Output "got:[$x]"
$y = [Console]::ReadLine()
Write-Output "null:$($null -eq $y)"

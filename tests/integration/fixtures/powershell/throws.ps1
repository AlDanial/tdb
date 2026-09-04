function Inner { throw "kaboom" }
Write-Host "before"
Inner
Write-Host "after"

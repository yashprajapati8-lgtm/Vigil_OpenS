# Ship a payment version the way a blue/green deploy does: start the new version
# behind the same "payment" name, wait until it is healthy, then stop the old one.
#   .\scripts\deploy.ps1 v2    # ship v2 (has a bug baked in: 40% of payments fail)
#   .\scripts\deploy.ps1 v1    # go back by hand (VIGIL can do this itself)
param([ValidateSet("v1", "v2")][string]$Version = "v2")
$ErrorActionPreference = "Stop"
$new = "payment-$Version"
$old = if ($Version -eq "v2") { "payment-v1" } else { "payment-v2" }

Write-Host "Starting $new ..."
docker compose --profile deploy up -d --no-deps $new | Out-Null

$deadline = (Get-Date).AddSeconds(60)
do {
    Start-Sleep -Seconds 2
    $health = docker inspect -f "{{.State.Health.Status}}" $new 2>$null
    Write-Host "  $new is $health"
} until ($health -eq "healthy" -or (Get-Date) -gt $deadline)
if ($health -ne "healthy") { throw "$new did not become healthy; $old is still serving" }

$running = docker ps --filter "name=^$old$" --format "{{.Names}}"
if ($running) {
    Write-Host "Stopping $old ..."
    docker stop $old | Out-Null
}
Write-Host "Done: payment is now $Version."

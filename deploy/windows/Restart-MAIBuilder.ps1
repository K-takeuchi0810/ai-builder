#Requires -RunAsAdministrator
$ErrorActionPreference = 'Stop'

$serviceName = 'MAIBuilder'
$healthUrl = 'http://127.0.0.1:8780/api/health'

Write-Host 'Restarting MAIBuilder...'
Restart-Service -Name $serviceName -Force

$deadline = (Get-Date).AddSeconds(60)
$healthy = $false
do {
    Start-Sleep -Seconds 1
    try {
        $service = Get-Service -Name $serviceName
        $response = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 3
        if ($service.Status -eq 'Running' -and $response.ok) {
            $healthy = $true
            break
        }
    } catch {
        # The service can be Running shortly before the HTTP listener is ready.
    }
} while ((Get-Date) -lt $deadline)

if (-not $healthy) {
    throw 'MAIBuilder did not become healthy within 60 seconds.'
}

Write-Host 'MAIBuilder restarted successfully (HTTP health OK).' -ForegroundColor Green
Write-Host "Data date: $($response.date)"
Write-Host "Live refresh: $($response.live_updated_at)"


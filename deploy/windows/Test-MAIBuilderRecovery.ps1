#Requires -RunAsAdministrator
$ErrorActionPreference = 'Stop'

$serviceName = 'MAIBuilder'
$healthUrl = 'http://127.0.0.1:8780/api/auth/status'
$recoveryTimeoutSeconds = 90

$service = Get-CimInstance Win32_Service -Filter "Name='$serviceName'"
if (-not $service) {
    throw "Windows service '$serviceName' was not found."
}
if ($service.State -ne 'Running' -or $service.ProcessId -eq 0) {
    throw "Windows service '$serviceName' is not running."
}

$oldProcessId = [int]$service.ProcessId
Write-Host "MAIBuilder recovery test"
Write-Host "1. Terminating service process tree (PID $oldProcessId)..."
& taskkill.exe /PID $oldProcessId /T /F
if ($LASTEXITCODE -ne 0) {
    throw "Could not terminate MAIBuilder (taskkill exit code $LASTEXITCODE)."
}

Write-Host "2. Waiting for Windows Service Control Manager to restart it..."
$deadline = (Get-Date).AddSeconds($recoveryTimeoutSeconds)
$recovered = $false
do {
    Start-Sleep -Seconds 2
    $service = Get-CimInstance Win32_Service -Filter "Name='$serviceName'"
    $newProcessId = [int]$service.ProcessId
    if ($service.State -eq 'Running' -and $newProcessId -ne 0 -and $newProcessId -ne $oldProcessId) {
        try {
            $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -eq 200) {
                $recovered = $true
                break
            }
        } catch {
            # The service can report Running shortly before the HTTP listener is ready.
        }
    }
    $remaining = [Math]::Max(0, [int]($deadline - (Get-Date)).TotalSeconds)
    Write-Host "   Waiting... ${remaining}s remaining"
} while ((Get-Date) -lt $deadline)

if (-not $recovered) {
    throw "MAIBuilder did not recover within $recoveryTimeoutSeconds seconds."
}

Write-Host "3. Recovery succeeded (new PID $newProcessId, HTTP 200)." -ForegroundColor Green


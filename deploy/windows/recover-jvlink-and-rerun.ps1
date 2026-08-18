#requires -RunAsAdministrator
param()

$ErrorActionPreference = "Stop"
$taskName = "keiba-auto-predict"
$serviceName = "JVLinkAgent"
$logPath = "C:\Users\kizun\dev\keiba-yosou\data\logs\jvlink_recovery_20260808.log"

function Write-RecoveryLog([string]$Message) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

Write-RecoveryLog "restarting $serviceName"
Restart-Service -Name $serviceName -Force
$service = Get-Service -Name $serviceName
$service.WaitForStatus("Running", [TimeSpan]::FromSeconds(20))
Write-RecoveryLog "$serviceName is running"

Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 3
$task = Get-ScheduledTask -TaskName $taskName
Write-RecoveryLog "$taskName state=$($task.State)"

Write-Host "JVLinkAgent restarted. Daily recovery task started."
Write-Host "You can close this window."

param([string]$TaskName = "MAIBuilder Live JRA Data")
$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = (Resolve-Path (Join-Path $root "..\keiba-yosou\.venv64\Scripts\python.exe")).Path
$decisionScript = (Resolve-Path (Join-Path $PSScriptRoot "configure-live-jvdata-task.py")).Path
$runner = (Resolve-Path (Join-Path $PSScriptRoot "run-live-jvdata-hidden.vbs")).Path

$raw = & $python $decisionScript
if ($LASTEXITCODE -ne 0 -or -not $raw) {
    throw "Could not determine the JRA live-update window."
}
$decision = $raw | ConvertFrom-Json
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if (-not $decision.enabled) {
    if ($existing) {
        Disable-ScheduledTask -TaskName $TaskName | Out-Null
    }
    Write-Host "Live update disabled: $($decision.target) reason=$($decision.reason)"
    exit 0
}

$startAt = [datetime]::Parse($decision.start_at)
$endAt = [datetime]::Parse($decision.end_at)
$duration = $endAt - $startAt
if ($duration.TotalMinutes -lt 1) {
    if ($existing) {
        Disable-ScheduledTask -TaskName $TaskName | Out-Null
    }
    Write-Host "Live update disabled: activation window has finished."
    exit 0
}

$action = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\wscript.exe" `
  -Argument "//B //NoLogo `"$runner`""
$trigger = New-ScheduledTaskTrigger -Once -At $startAt `
  -RepetitionInterval (New-TimeSpan -Minutes 1) `
  -RepetitionDuration $duration
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 4)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive `
  -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
  -Settings $settings -Principal $principal -Force | Out-Null
Enable-ScheduledTask -TaskName $TaskName | Out-Null
Write-Host "Live update enabled: $($decision.target) $($decision.start_at) - $($decision.end_at) reason=$($decision.reason)"

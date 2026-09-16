param(
  [string]$TaskName = "MAIBuilder Live JRA Data",
  [string]$ControllerTaskName = "MAIBuilder Live JRA Data Controller"
)
$ErrorActionPreference = "Stop"
$controller = (Resolve-Path (Join-Path $PSScriptRoot "Configure-LiveJvdataTask.ps1")).Path
$arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$controller`" -TaskName `"$TaskName`""
$action = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" `
  -Argument $arguments
$daily = New-ScheduledTaskTrigger -Daily -At "00:05"
$repeat = New-ScheduledTaskTrigger -Once -At "00:05" `
  -RepetitionInterval (New-TimeSpan -Hours 6) `
  -RepetitionDuration (New-TimeSpan -Days 1)
$daily.Repetition = $repeat.Repetition
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive `
  -RunLevel Limited
Register-ScheduledTask -TaskName $ControllerTaskName -Action $action -Trigger $daily `
  -Settings $settings -Principal $principal -Force | Out-Null

& $controller -TaskName $TaskName
Write-Host "Installed: $ControllerTaskName (4 checks/day)"
Write-Host "Managed task: $TaskName (race days and race-time window only; every 1 minute)"

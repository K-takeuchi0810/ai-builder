#Requires -RunAsAdministrator
$ErrorActionPreference = 'Stop'

$taskName = 'MAIBuilder Daily Backup'
$configPath = Join-Path $PSScriptRoot 'shared.config.json'
$backupScript = Join-Path $PSScriptRoot 'Invoke-MAIBuilderBackup.ps1'
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$backupDirectory = Join-Path $config.workdir 'out\backups'

New-Item -ItemType Directory -Path $backupDirectory -Force | Out-Null

# Backups contain the administrator credential. Limit them to the installing
# user, LocalSystem, and the local Administrators group.
$currentUserSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& icacls.exe $backupDirectory /inheritance:r `
    /grant:r "*${currentUserSid}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Failed to restrict the backup directory.' }

$argument = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$backupScript`""
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argument
$trigger = New-ScheduledTaskTrigger -Daily -At '03:00'
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Description `
    'Daily verified backup of MAIBuilder users, invites, sessions, and settings.' -Force | Out-Null

Start-ScheduledTask -TaskName $taskName
Write-Host "Installed and started scheduled task: $taskName" -ForegroundColor Green
Write-Host "Backup directory: $backupDirectory"

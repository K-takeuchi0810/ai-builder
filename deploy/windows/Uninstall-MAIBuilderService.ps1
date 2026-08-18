$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script from an elevated PowerShell window.'
}
$config = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'shared.config.json') -Raw | ConvertFrom-Json
$python = [string]$config.python_exe
$script = Join-Path (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path 'builder\windows_service.py'
& $python $script stop
& $python $script remove
Write-Host 'The MAIBuilder service was removed. Data and configuration files were preserved.'

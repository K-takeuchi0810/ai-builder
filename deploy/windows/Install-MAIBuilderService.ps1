$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script from an elevated PowerShell window.'
}
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$config = Join-Path $PSScriptRoot 'shared.config.json'
if (-not (Test-Path -LiteralPath $config)) {
    throw 'Copy shared.config.json.example to shared.config.json and configure it first.'
}
$settings = Get-Content -LiteralPath $config -Raw | ConvertFrom-Json
$python = [string]$settings.python_exe
if (-not (Test-Path -LiteralPath $python)) { throw "python_exe was not found: $python" }
$currentSid = $identity.User.Value
& icacls.exe $config /inheritance:r /grant:r "*${currentSid}:(F)" '*S-1-5-18:(R)' '*S-1-5-32-544:(F)'
if ($LASTEXITCODE -ne 0) { throw 'Failed to restrict access to shared.config.json.' }
& $python -m pip install -r (Join-Path $root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
$postInstall = Join-Path (Split-Path -Parent $python) 'pywin32_postinstall.py'
if (Test-Path -LiteralPath $postInstall) {
    & $python $postInstall -install
    if ($LASTEXITCODE -ne 0) { throw 'pywin32 post-install setup failed.' }
}
# pythonservice.exe is placed at the virtual-environment root.  On Python 3.14+
# the matching Python DLL must be next to it for the Service Control Manager.
$basePrefix = (& $python -c 'import sys; print(sys.base_prefix)').Trim()
$pythonDllName = (& $python -c 'import sys; print(f"python{sys.version_info.major}{sys.version_info.minor}.dll")').Trim()
$sourcePythonDll = Join-Path $basePrefix $pythonDllName
$venvRoot = Split-Path -Parent (Split-Path -Parent $python)
if (Test-Path -LiteralPath $sourcePythonDll) {
    Copy-Item -LiteralPath $sourcePythonDll -Destination (Join-Path $venvRoot $pythonDllName) -Force
}
& $python (Join-Path $root 'builder\windows_service.py') --check-config
if ($LASTEXITCODE -ne 0) { throw 'shared.config.json validation failed.' }
& $python (Join-Path $root 'builder\windows_service.py') --startup auto install
if ($LASTEXITCODE -ne 0) { throw 'MAIBuilder service installation failed.' }
& sc.exe failure MAIBuilder reset= 86400 actions= restart/60000/restart/60000/restart/60000
& sc.exe failureflag MAIBuilder 1
& $python (Join-Path $root 'builder\windows_service.py') start
Write-Host 'The MAIBuilder service was installed and started.'

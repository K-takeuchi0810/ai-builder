$ErrorActionPreference = 'Stop'

$configPath = Join-Path $PSScriptRoot 'shared.config.json'
if (-not (Test-Path -LiteralPath $configPath)) {
    throw "MAIBuilder service configuration was not found: $configPath"
}

$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
if (-not (Test-Path -LiteralPath $config.python_exe)) {
    throw "Configured Python executable was not found."
}

Push-Location -LiteralPath $config.workdir
try {
    & $config.python_exe -m builder.backup --config $configPath --keep 14
    if ($LASTEXITCODE -ne 0) {
        throw "MAIBuilder backup failed (exit code $LASTEXITCODE)."
    }
} finally {
    Pop-Location
}


$ErrorActionPreference = 'Stop'
$tailscale = 'C:\Program Files\Tailscale\tailscale.exe'
if (-not (Test-Path -LiteralPath $tailscale)) {
    throw 'Tailscale is not installed.'
}
$health = Invoke-RestMethod 'http://127.0.0.1:8780/api/auth/status'
if (-not $health.enabled) {
    throw 'MAIBuilder shared authentication is not enabled. Funnel was not started.'
}
& $tailscale funnel --yes --bg --https=443 http://127.0.0.1:8780
if ($LASTEXITCODE -ne 0) { throw 'Tailscale Funnel failed to start.' }
& $tailscale funnel status


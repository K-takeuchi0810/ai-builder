#Requires -RunAsAdministrator
$ErrorActionPreference = 'Stop'

$adapters = Get-NetAdapter -Physical | Where-Object { $_.Status -ne 'Disabled' }
if (-not $adapters) { throw 'No enabled physical network adapter was found.' }

foreach ($adapter in $adapters) {
    try {
        Set-NetAdapterPowerManagement -Name $adapter.Name `
            -WakeOnMagicPacket Enabled -WakeOnPattern Disabled
        Write-Host "Wake-on-LAN enabled: $($adapter.Name) / $($adapter.MacAddress)"
    } catch {
        Write-Warning "Could not configure Wake-on-LAN for $($adapter.Name): $_"
    }
}

Write-Host ''
Write-Host 'Also enable Wake on LAN / Power on by PCI-E in BIOS or UEFI.'
Write-Host 'After entering sleep, verify that the target NIC appears below.'
& powercfg.exe /devicequery wake_armed

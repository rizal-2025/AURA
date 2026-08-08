[CmdletBinding()]
param([string]$Profile = 'production')

. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')
$startedAt = [Diagnostics.Stopwatch]::StartNew()
Assert-AuraProductionProfile -Profile $Profile
$null = Assert-AuraRepositoryLayout

# The public boundary always closes before the local gateway is touched.
& (Join-Path $PSScriptRoot 'Stop-TailscaleFunnel.ps1') `
    -Profile production | Out-Null
if (Test-AuraPublicHealth -Profile production) {
    throw 'AURA_PUBLIC_BOUNDARY_STILL_ACTIVE'
}
$funnel = Get-AuraOwnedProcessState -Kind funnel -Profile production
if ($funnel.State -notin @('absent', 'stale')) {
    throw 'HUMAN_GATE_PROCESS_OWNERSHIP_AMBIGUOUS'
}

& (Join-Path $PSScriptRoot 'Stop-Aura.ps1') `
    -Profile production | Out-Null
if (-not (Test-AuraPortClosed -Port 8000)) {
    throw 'AURA_PRODUCTION_PORT_STILL_OPEN'
}
$aura = Get-AuraOwnedProcessState -Kind aura -Profile production
if ($aura.State -notin @('absent', 'stale')) {
    throw 'AURA_PROCESS_OWNERSHIP_UNCERTAIN'
}
Write-AuraOperationLog -Profile production -Stage STOP -Code STOPPED `
    -ElapsedMs ([int]$startedAt.ElapsedMilliseconds)
Write-Output 'AURA_PUBLIC_DEMO_STOPPED profile=production'

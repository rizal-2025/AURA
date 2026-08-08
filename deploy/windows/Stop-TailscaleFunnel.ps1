[CmdletBinding()]
param(
    [ValidateSet('staging', 'production')]
    [string]$Profile = 'production'
)

. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')
$ownership = Get-AuraOwnedProcessState -Kind funnel -Profile $Profile `
    -RepairStaleMetadata
if ($ownership.State -in @('stale', 'absent')) {
    Write-Output 'AURA_FUNNEL_NOT_RUNNING'
    return
}
if ($ownership.State -eq 'ambiguous') {
    throw 'HUMAN_GATE_PROCESS_OWNERSHIP_AMBIGUOUS'
}

$current = Assert-AuraOwnedProcessStillMatches `
    -OriginalProcessInfo $ownership.ProcessInfo -Kind funnel -Profile $Profile
if ($null -ne $current) {
    Stop-Process -Id ([int]$current.ProcessId)
    Wait-Process -Id ([int]$current.ProcessId) -Timeout 15 `
        -ErrorAction SilentlyContinue
}
$remaining = Assert-AuraOwnedProcessStillMatches `
    -OriginalProcessInfo $ownership.ProcessInfo -Kind funnel -Profile $Profile
if ($null -ne $remaining) {
    Stop-Process -Id ([int]$remaining.ProcessId) -Force
    Wait-Process -Id ([int]$remaining.ProcessId) -Timeout 5 `
        -ErrorAction SilentlyContinue
}
if ($null -ne (Assert-AuraOwnedProcessStillMatches `
    -OriginalProcessInfo $ownership.ProcessInfo -Kind funnel -Profile $Profile)) {
    throw 'AURA_FUNNEL_STOP_TIMEOUT'
}
for ($attempt = 0; $attempt -lt 10; $attempt++) {
    if (-not (Test-AuraPublicHealth -Profile $Profile)) { break }
    Start-Sleep -Seconds 1
}
if (Test-AuraPublicHealth -Profile $Profile) {
    throw 'AURA_FUNNEL_STILL_SERVING_PRODUCTION'
}
if (-not (Test-AuraFunnelProcessesAbsent -Profile $Profile)) {
    throw 'HUMAN_GATE_PROCESS_OWNERSHIP_AMBIGUOUS'
}
Remove-Item -LiteralPath $ownership.Path -Force -ErrorAction SilentlyContinue
Write-Output 'AURA_FUNNEL_STOP_OK'

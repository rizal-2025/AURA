[CmdletBinding()]
param(
    [ValidateSet('staging', 'production')]
    [string]$Profile = 'production'
)

. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')
$port = Get-AuraProfilePort -Profile $Profile
$ownership = Get-AuraOwnedProcessState -Kind aura -Profile $Profile `
    -RepairStaleMetadata
if ($ownership.State -in @('stale', 'absent')) {
    if (-not (Test-AuraPortClosed -Port $port)) {
        throw 'AURA_PROCESS_OWNERSHIP_UNCERTAIN'
    }
    Write-Output 'AURA_NOT_RUNNING'
    return
}
if ($ownership.State -in @('ambiguous', 'uncertain')) {
    throw 'AURA_PROCESS_OWNERSHIP_UNCERTAIN'
}

$gateway = Get-AuraGatewayListenerProcessInfo `
    -OwnershipProcessInfo $ownership.ProcessInfo -Profile $Profile
$target = if ($null -ne $gateway) { $gateway } else { $ownership.ProcessInfo }
$current = Assert-AuraOwnedProcessStillMatches `
    -OriginalProcessInfo $target -Kind aura -Profile $Profile
if ($null -ne $current) {
    Stop-Process -Id ([int]$current.ProcessId)
    Wait-Process -Id ([int]$current.ProcessId) -Timeout 30 `
        -ErrorAction SilentlyContinue
}
$remaining = Assert-AuraOwnedProcessStillMatches `
    -OriginalProcessInfo $target -Kind aura -Profile $Profile
if ($null -ne $remaining) {
    Stop-Process -Id ([int]$remaining.ProcessId) -Force
    Wait-Process -Id ([int]$remaining.ProcessId) -Timeout 5 `
        -ErrorAction SilentlyContinue
}
if ($null -ne (Assert-AuraOwnedProcessStillMatches `
    -OriginalProcessInfo $target -Kind aura -Profile $Profile)) {
    throw 'AURA_STOP_TIMEOUT'
}

# A Python venv redirector may be the recorded legacy parent. Once its exact
# child gateway exits, it should exit too; terminate only that revalidated
# parent if it remains after the bounded wait.
if ([int]$target.ProcessId -ne [int]$ownership.ProcessInfo.ProcessId) {
    Wait-Process -Id ([int]$ownership.ProcessInfo.ProcessId) -Timeout 5 `
        -ErrorAction SilentlyContinue
    $parent = Assert-AuraOwnedProcessStillMatches `
        -OriginalProcessInfo $ownership.ProcessInfo -Kind aura -Profile $Profile
    if ($null -ne $parent) {
        Stop-Process -Id ([int]$parent.ProcessId) -Force
        Wait-Process -Id ([int]$parent.ProcessId) -Timeout 5 `
            -ErrorAction SilentlyContinue
    }
}
if (-not (Test-AuraPortClosed -Port $port)) { throw 'AURA_PORT_STILL_OPEN' }
Remove-Item -LiteralPath $ownership.Path -Force -ErrorAction SilentlyContinue
Write-Output 'AURA_STOP_OK'

[CmdletBinding()]
param(
    [ValidateSet('staging', 'production')]
    [string]$Profile = 'production'
)

. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')
Initialize-AuraDataDirectories
$publicPort = Get-AuraFunnelPort -Profile $Profile
$target = Get-AuraFunnelTarget -Profile $Profile
$otherProfile = if ($Profile -eq 'production') { 'staging' } else { 'production' }

$auraOwnership = Get-AuraOwnedProcessState -Kind aura -Profile $Profile
$gatewayProcess = if ($auraOwnership.State -eq 'owned') {
    Get-AuraGatewayListenerProcessInfo `
        -OwnershipProcessInfo $auraOwnership.ProcessInfo -Profile $Profile
} else { $null }
if (
    $auraOwnership.State -ne 'owned' `
    -or $null -eq $gatewayProcess `
    -or -not (Test-AuraLocalHealth -Profile $Profile)
) { throw 'AURA_GATEWAY_NOT_READY' }

$otherOwnership = Get-AuraOwnedProcessState -Kind funnel -Profile $otherProfile
if ($otherOwnership.State -notin @('absent', 'stale')) {
    throw 'AURA_FUNNEL_OTHER_PROFILE_ACTIVE'
}
$ownership = Get-AuraOwnedProcessState -Kind funnel -Profile $Profile `
    -RepairStaleMetadata
if ($ownership.State -eq 'ambiguous') {
    throw 'HUMAN_GATE_PROCESS_OWNERSHIP_AMBIGUOUS'
}
if ($ownership.State -eq 'owned') {
    if (Test-AuraPublicHealth -Profile $Profile) {
        if ($ownership.Legacy) {
            Write-AuraOwnershipMetadata -Path $ownership.Path `
                -ProcessInfo $ownership.ProcessInfo
        }
        Write-Output 'AURA_FUNNEL_ALREADY_READY'
        return
    }
    & (Join-Path $PSScriptRoot 'Stop-TailscaleFunnel.ps1') `
        -Profile $Profile | Out-Null
}

$tailscale = Get-TailscalePath
$ownershipPath = Get-AuraOwnershipPath -Kind funnel -Profile $Profile
# Deliberately foreground: no persistent public exposure after reboot.
$commandLine = '"{0}" funnel --https={1} {2}' -f `
    $tailscale, $publicPort, $target
$startup = New-CimInstance -ClassName Win32_ProcessStartup `
    -Namespace root\cimv2 -Property @{ ShowWindow = [uint16]0 } -ClientOnly
$created = Invoke-CimMethod -ClassName Win32_Process -MethodName Create `
    -Arguments @{
        CommandLine = $commandLine
        ProcessStartupInformation = $startup
    }
if ([int]$created.ReturnValue -ne 0 -or [int]$created.ProcessId -le 0) {
    throw 'AURA_FUNNEL_PROCESS_CREATE_FAILED'
}
$processId = [int]$created.ProcessId
$processInfo = $null
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    $processInfo = Get-CimInstance Win32_Process `
        -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
    if ($null -ne $processInfo) { break }
    Start-Sleep -Milliseconds 100
}
if ($null -eq $processInfo) { throw 'AURA_FUNNEL_START_FAILED' }
if (-not (Test-AuraExpectedProcessInfo -ProcessInfo $processInfo `
    -Kind funnel -Profile $Profile)) {
    throw 'HUMAN_GATE_PROCESS_OWNERSHIP_AMBIGUOUS'
}
try {
    Write-AuraOwnershipMetadata -Path $ownershipPath -ProcessInfo $processInfo
} catch {
    $verified = Assert-AuraOwnedProcessStillMatches `
        -OriginalProcessInfo $processInfo -Kind funnel -Profile $Profile
    if ($null -ne $verified) {
        Stop-Process -Id ([int]$verified.ProcessId) -Force
    }
    Remove-Item -LiteralPath $ownershipPath -Force -ErrorAction SilentlyContinue
    throw 'AURA_FUNNEL_METADATA_WRITE_FAILED'
}

$ready = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    if ($null -eq (Get-CimInstance Win32_Process `
        -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue)) { break }
    $current = Get-AuraOwnedProcessState -Kind funnel -Profile $Profile
    if ($current.State -eq 'owned' -and (Test-AuraPublicHealth -Profile $Profile)) {
        $ready = $true
        break
    }
    Start-Sleep -Seconds 1
}
if (-not $ready) {
    $current = Get-AuraOwnedProcessState -Kind funnel -Profile $Profile
    if ($current.State -eq 'owned') {
        $verified = Assert-AuraOwnedProcessStillMatches `
            -OriginalProcessInfo $current.ProcessInfo -Kind funnel -Profile $Profile
        if ($null -ne $verified) {
            Stop-Process -Id ([int]$verified.ProcessId) -Force
        }
        Remove-Item -LiteralPath $ownershipPath -Force `
            -ErrorAction SilentlyContinue
    } elseif ($current.State -eq 'stale') {
        Remove-Item -LiteralPath $ownershipPath -Force `
            -ErrorAction SilentlyContinue
    } else {
        throw 'HUMAN_GATE_PROCESS_OWNERSHIP_AMBIGUOUS'
    }
    throw 'AURA_FUNNEL_START_FAILED'
}
Write-Output 'AURA_FUNNEL_START_OK'

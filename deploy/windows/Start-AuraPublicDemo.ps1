[CmdletBinding()]
param([string]$Profile = 'production')

. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')
$startedAt = [Diagnostics.Stopwatch]::StartNew()
Assert-AuraProductionProfile -Profile $Profile
$null = Assert-AuraRepositoryLayout
Initialize-AuraDataDirectories

$configPath = Get-AuraSecretPath -Profile production
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw 'AURA_PRODUCTION_CONFIG_MISSING'
}
Assert-AuraOperatorSecretAcl -Path $configPath
$pgPassPath = Get-AuraPgPassPath -Profile production
if (-not (Test-Path -LiteralPath $pgPassPath -PathType Leaf)) {
    throw 'AURA_PRODUCTION_PGPASS_MISSING'
}
Assert-AuraOperatorSecretAcl -Path $pgPassPath

$previous = Import-AuraConfiguration -Profile production
try {
    Assert-AuraProductionConfiguration
    if (-not (Test-AuraPostgreSQLServiceRunning)) {
        throw 'AURA_POSTGRESQL_SERVICE_NOT_RUNNING'
    }
    if (-not (Test-AuraPostgreSQLLoopbackListener)) {
        throw 'AURA_POSTGRESQL_LISTENER_INVALID'
    }
    if (-not (Test-AuraProductionDatabaseReadiness)) {
        throw 'AURA_PRODUCTION_DATABASE_NOT_READY'
    }
} finally {
    Restore-AuraProcessEnvironment -Previous $previous
}

$auraBefore = Get-AuraOwnedProcessState -Kind aura -Profile production `
    -RepairStaleMetadata
if ($auraBefore.State -in @('ambiguous', 'uncertain')) {
    throw 'AURA_PROCESS_OWNERSHIP_UNCERTAIN'
}
if ($auraBefore.State -notin @('owned', 'absent', 'stale')) {
    throw 'HUMAN_GATE_PROCESS_OWNERSHIP_AMBIGUOUS'
}
if (
    $auraBefore.State -ne 'owned' `
    -and -not (Test-AuraPortClosed -Port 8000)
) { throw 'AURA_PORT_OWNERSHIP_UNEXPECTED' }

$auraStartedHere = $false
$funnelStartedHere = $false
try {
    $auraStartResult = @(& (Join-Path $PSScriptRoot 'Start-Aura.ps1') `
        -Profile production)
    $auraStartedHere = $auraStartResult -contains 'AURA_START_OK'
    $aura = Get-AuraOwnedProcessState -Kind aura -Profile production
    $gateway = if ($aura.State -eq 'owned') {
        Get-AuraGatewayListenerProcessInfo `
            -OwnershipProcessInfo $aura.ProcessInfo -Profile production
    } else { $null }
    if (
        $aura.State -ne 'owned' `
        -or $null -eq $gateway `
        -or -not (Test-AuraLocalHealth -Profile production)
    ) { throw 'AURA_PRODUCTION_GATEWAY_INVALID' }
    if (-not (Test-AuraFirewallRules)) { throw 'AURA_FIREWALL_INVALID' }

    $funnelBefore = Get-AuraOwnedProcessState -Kind funnel `
        -Profile production -RepairStaleMetadata
    if ($funnelBefore.State -eq 'ambiguous') {
        throw 'HUMAN_GATE_PROCESS_OWNERSHIP_AMBIGUOUS'
    }
    $funnelStartResult = @(& (Join-Path $PSScriptRoot 'Start-TailscaleFunnel.ps1') `
        -Profile production)
    $funnelStartedHere = $funnelStartResult -contains 'AURA_FUNNEL_START_OK'
    $funnel = Get-AuraOwnedProcessState -Kind funnel -Profile production
    if ($funnel.State -ne 'owned') {
        throw 'AURA_FUNNEL_PROCESS_OWNERSHIP_INVALID'
    }
    if (-not (Test-AuraPublicHealth -Profile production)) {
        throw 'AURA_PUBLIC_HEALTH_FAILED'
    }
} catch {
    $failureCode = $_.Exception.Message
    $rollbackFailed = $false
    if ($funnelStartedHere) {
        try {
            & (Join-Path $PSScriptRoot 'Stop-TailscaleFunnel.ps1') `
                -Profile production | Out-Null
        } catch { $rollbackFailed = $true }
    }
    if ($auraStartedHere -and -not $rollbackFailed) {
        try {
            & (Join-Path $PSScriptRoot 'Stop-Aura.ps1') `
                -Profile production | Out-Null
        } catch { $rollbackFailed = $true }
    }
    Write-AuraOperationLog -Profile production -Stage START `
        -Code AURA_PUBLIC_DEMO_START_FAILED `
        -ElapsedMs ([int]$startedAt.ElapsedMilliseconds)
    if ($rollbackFailed) { throw 'AURA_PUBLIC_DEMO_ROLLBACK_FAILED' }
    if ($failureCode -in @(
        'HUMAN_GATE_PROCESS_OWNERSHIP_AMBIGUOUS',
        'AURA_PROCESS_OWNERSHIP_UNCERTAIN'
    )) { throw $failureCode }
    throw 'AURA_PUBLIC_DEMO_START_FAILED'
}

$marker = if (
    $auraBefore.State -eq 'owned' `
    -and $funnelBefore.State -eq 'owned'
) {
    'AURA_PUBLIC_DEMO_ALREADY_READY profile=production'
} else {
    'AURA_PUBLIC_DEMO_READY profile=production'
}
Write-AuraOperationLog -Profile production -Stage START -Code READY `
    -ElapsedMs ([int]$startedAt.ElapsedMilliseconds)
Write-Output $marker

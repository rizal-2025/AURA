[CmdletBinding()]
param([string]$Profile = 'production')

. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')
Assert-AuraProductionProfile -Profile $Profile
$null = Assert-AuraRepositoryLayout
$configPath = Get-AuraSecretPath -Profile production
$pgPassPath = Get-AuraPgPassPath -Profile production
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw 'AURA_PRODUCTION_CONFIG_MISSING'
}
if (-not (Test-Path -LiteralPath $pgPassPath -PathType Leaf)) {
    throw 'AURA_PRODUCTION_PGPASS_MISSING'
}
Assert-AuraOperatorSecretAcl -Path $configPath
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

$aura = Get-AuraOwnedProcessState -Kind aura -Profile production
if ($aura.State -ne 'owned') { throw 'AURA_PROCESS_OWNERSHIP_UNCERTAIN' }
$gateway = Get-AuraGatewayListenerProcessInfo `
    -OwnershipProcessInfo $aura.ProcessInfo -Profile production
if ($null -eq $gateway) {
    throw 'AURA_PRODUCTION_LISTENER_INVALID'
}
if (-not (Test-AuraLocalHealth -Profile production)) {
    throw 'AURA_LOCAL_HEALTH_FAILED'
}
if (-not (Test-AuraFirewallRules)) { throw 'AURA_FIREWALL_INVALID' }
$funnel = Get-AuraOwnedProcessState -Kind funnel -Profile production
if ($funnel.State -ne 'owned') {
    throw 'HUMAN_GATE_PROCESS_OWNERSHIP_AMBIGUOUS'
}
if (-not (Test-AuraPublicHealth -Profile production)) {
    throw 'AURA_PUBLIC_HEALTH_FAILED'
}
$cleanupHealth = Get-AuraCleanupHealth -Profile production
if (-not $cleanupHealth.ReadyCompatible) {
    throw $cleanupHealth.Status
}
Write-Output 'AURA_PUBLIC_DEMO_READY profile=production'

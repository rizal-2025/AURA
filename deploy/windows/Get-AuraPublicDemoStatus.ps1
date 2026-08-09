[CmdletBinding()]
param([string]$Profile = 'production')

. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')
Assert-AuraProductionProfile -Profile $Profile
$reasons = [System.Collections.Generic.List[string]]::new()

$configAclValid = $false
$pgPassAclValid = $false
$configValid = $false
$databaseReady = $false
$configPath = Get-AuraSecretPath -Profile production
$pgPassPath = Get-AuraPgPassPath -Profile production
try {
    if (Test-Path -LiteralPath $configPath -PathType Leaf) {
        Assert-AuraOperatorSecretAcl -Path $configPath
        $configAclValid = $true
    }
} catch { }
try {
    if (Test-Path -LiteralPath $pgPassPath -PathType Leaf) {
        Assert-AuraOperatorSecretAcl -Path $pgPassPath
        $pgPassAclValid = $true
    }
} catch { }
if (-not $configAclValid) { $reasons.Add('CONFIG_ACL_INVALID') }
if (-not $pgPassAclValid) { $reasons.Add('PGPASS_ACL_INVALID') }

$previous = $null
if ($configAclValid -and $pgPassAclValid) {
    try {
        $previous = Import-AuraConfiguration -Profile production
        Assert-AuraProductionConfiguration
        $configValid = $true
        $databaseReady = Test-AuraProductionDatabaseReadiness
    } catch { } finally {
        if ($null -ne $previous) {
            Restore-AuraProcessEnvironment -Previous $previous
        }
    }
}
if (-not $configValid) { $reasons.Add('CONFIG_TARGET_INVALID') }

$postgresqlRunning = Test-AuraPostgreSQLServiceRunning
if (-not $postgresqlRunning) { $reasons.Add('POSTGRESQL_NOT_RUNNING') }
$postgresqlLoopback = Test-AuraPostgreSQLLoopbackListener
if (-not $postgresqlLoopback) { $reasons.Add('POSTGRESQL_LISTENER_INVALID') }
$databaseReady = $databaseReady -and $postgresqlLoopback
if (-not $databaseReady) { $reasons.Add('DATABASE_NOT_READY') }

try {
    $aura = Get-AuraOwnedProcessState -Kind aura -Profile production
} catch {
    $aura = [PSCustomObject]@{ State = 'ambiguous'; ProcessInfo = $null }
}
$auraPresent = $aura.State -eq 'owned'
$ownedPidValid = $auraPresent
if ($aura.State -eq 'uncertain') { $reasons.Add('AURA_PROCESS_OWNERSHIP_UNCERTAIN') }
if ($aura.State -eq 'ambiguous') { $reasons.Add('AURA_PROCESS_OWNERSHIP_AMBIGUOUS') }
if ($aura.State -eq 'stale') { $reasons.Add('AURA_PID_STALE') }

$listenerLoopback = $false
if ($auraPresent) {
    $listenerLoopback = $null -ne (Get-AuraGatewayListenerProcessInfo `
        -OwnershipProcessInfo $aura.ProcessInfo -Profile production)
} elseif (-not (Test-AuraPortClosed -Port 8000)) {
    $listenerLoopback = Test-AuraExactLoopbackListener -Port 8000
}
$localHealth = Test-AuraLocalHealth -Profile production
if ($auraPresent -and -not $listenerLoopback) { $reasons.Add('LISTENER_INVALID') }
if ($auraPresent -and -not $localHealth) { $reasons.Add('LOCAL_HEALTH_FAILED') }

try {
    $funnel = Get-AuraOwnedProcessState -Kind funnel -Profile production
} catch {
    $funnel = [PSCustomObject]@{ State = 'ambiguous'; ProcessInfo = $null }
}
$funnelPresent = $funnel.State -eq 'owned'
if ($funnel.State -eq 'ambiguous') { $reasons.Add('FUNNEL_OWNERSHIP_AMBIGUOUS') }
if ($funnel.State -eq 'stale') { $reasons.Add('FUNNEL_PID_STALE') }
$publicHealth = Test-AuraPublicHealth -Profile production
if ($funnelPresent -and -not $publicHealth) { $reasons.Add('PUBLIC_HEALTH_FAILED') }

$firewallValid = Test-AuraFirewallRules
if (-not $firewallValid) { $reasons.Add('FIREWALL_INVALID') }
$backupAge = Get-AuraBackupAgeClassification -Profile production
if ($backupAge -eq 'warning') { $reasons.Add('BACKUP_WARNING') }
if ($backupAge -eq 'stale') { $reasons.Add('BACKUP_STALE') }
if ($backupAge -eq 'missing') { $reasons.Add('BACKUP_MISSING') }

$cleanupHealth = Get-AuraCleanupHealth -Profile production
if ($cleanupHealth.Status -eq 'CLEANUP_NEVER_RAN') {
    $reasons.Add('CLEANUP_NEVER_RAN')
}
if ($cleanupHealth.Status -eq 'CLEANUP_STALE') {
    $reasons.Add('CLEANUP_STALE')
}
if ($cleanupHealth.Status -eq 'CLEANUP_FAILED') {
    $reasons.Add('CLEANUP_FAILED')
}

function ConvertTo-SafeYesNo([bool]$Value) {
    if ($Value) { return 'yes' }
    return 'no'
}
Write-Output ('AURA_PUBLIC_DEMO_CHECK postgresql_running={0}' -f `
    (ConvertTo-SafeYesNo $postgresqlRunning))
Write-Output ('AURA_PUBLIC_DEMO_CHECK database_ready={0}' -f `
    (ConvertTo-SafeYesNo $databaseReady))
Write-Output ('AURA_PUBLIC_DEMO_CHECK aura_process_present={0}' -f `
    (ConvertTo-SafeYesNo $auraPresent))
Write-Output ('AURA_PUBLIC_DEMO_CHECK owned_pid_valid={0}' -f `
    (ConvertTo-SafeYesNo $ownedPidValid))
Write-Output ('AURA_PUBLIC_DEMO_CHECK listener_loopback={0}' -f `
    (ConvertTo-SafeYesNo $listenerLoopback))
Write-Output ('AURA_PUBLIC_DEMO_CHECK local_health={0}' -f `
    (ConvertTo-SafeYesNo $localHealth))
Write-Output ('AURA_PUBLIC_DEMO_CHECK funnel_process_present={0}' -f `
    (ConvertTo-SafeYesNo $funnelPresent))
Write-Output ('AURA_PUBLIC_DEMO_CHECK public_health={0}' -f `
    (ConvertTo-SafeYesNo $publicHealth))
Write-Output ('AURA_PUBLIC_DEMO_CHECK firewall_valid={0}' -f `
    (ConvertTo-SafeYesNo $firewallValid))
Write-Output ('AURA_PUBLIC_DEMO_CHECK config_acl_valid={0}' -f `
    (ConvertTo-SafeYesNo $configAclValid))
Write-Output ('AURA_PUBLIC_DEMO_CHECK pgpass_acl_valid={0}' -f `
    (ConvertTo-SafeYesNo $pgPassAclValid))
Write-Output "AURA_PUBLIC_DEMO_CHECK backup_age=$backupAge"
Write-Output "AURA_PUBLIC_DEMO_CHECK cleanup_health=$($cleanupHealth.Status)"
Write-Output (
    'AURA_PUBLIC_DEMO_CHECK cleanup_last_success_age={0}' -f `
        $cleanupHealth.LastSuccessAge
)

$offline = (
    -not $auraPresent -and -not $funnelPresent `
    -and -not $localHealth -and -not $publicHealth `
    -and (Test-AuraPortClosed -Port 8000)
)
$ready = (
    $configValid -and $configAclValid -and $pgPassAclValid `
    -and $postgresqlRunning -and $databaseReady `
    -and $auraPresent -and $ownedPidValid -and $listenerLoopback `
    -and $localHealth -and $funnelPresent -and $publicHealth `
    -and $firewallValid -and $backupAge -notin @('stale', 'missing')
)
$state = if ($ready) { 'ready' } elseif ($offline) { 'offline' } else { 'degraded' }
$reasonOutput = if ($reasons.Count -eq 0) {
    'NONE'
} else { (@($reasons | Sort-Object -Unique) -join ',') }
Write-Output "AURA_PUBLIC_DEMO_CHECK reason_codes=$reasonOutput"
Write-Output "AURA_PUBLIC_DEMO_STATUS profile=production state=$state"

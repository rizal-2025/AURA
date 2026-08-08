[CmdletBinding()]
param([string]$Profile = 'production')

. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')
Assert-AuraProductionProfile -Profile $Profile
$null = Assert-AuraRepositoryLayout
Initialize-AuraDataDirectories
$started = [DateTime]::UtcNow

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
    if (-not (Test-AuraPostgreSQLServiceRunning) `
        -or -not (Test-AuraProductionDatabaseReadiness)) {
        throw 'AURA_PRODUCTION_DATABASE_NOT_READY'
    }
} finally {
    Restore-AuraProcessEnvironment -Previous $previous
}

& (Join-Path $PSScriptRoot 'Backup-DemoDatabase.ps1') `
    -Profile production | Out-Null
$created = @(Get-ChildItem -LiteralPath $script:AuraBackupRoot -File `
    -Filter 'aura_demo_public_*.dump' | Where-Object {
        $_.CreationTimeUtc -ge $started.AddSeconds(-2)
    } | Sort-Object CreationTimeUtc -Descending)
if ($created.Count -ne 1) { throw 'AURA_BACKUP_ARTIFACT_AMBIGUOUS' }
$backup = $created[0]
Assert-AuraOperatorSecretAcl -Path $backup.FullName
$pgRestore = Resolve-AuraPostgreSQLTool -ToolName 'pg_restore.exe'
& $pgRestore --list $backup.FullName 1>$null 2>$null
if ($LASTEXITCODE -ne 0) { throw 'AURA_BACKUP_ARCHIVE_INVALID' }
Write-Output (
    'AURA_PRODUCTION_BACKUP timestamp_class=utc bytes={0} archive_valid=yes acl_protected=yes' `
        -f $backup.Length
)

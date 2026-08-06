[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$BackupPath,
    [Parameter(Mandatory)][ValidateSet('RESTORE_TO_AURA_RESTORE_TEST')]
    [string]$Confirmation,
    [switch]$DropAfterVerification,
    [ValidateSet('', 'DROP_AURA_RESTORE_TEST')]
    [string]$DropConfirmation = ''
)

. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')
$safeBackup = Assert-AuraPathWithin -Path $BackupPath -Root $script:AuraBackupRoot
if (-not (Test-Path -LiteralPath $safeBackup -PathType Leaf) -or [System.IO.Path]::GetExtension($safeBackup) -ne '.dump') {
    throw 'AURA_RESTORE_BACKUP_INVALID'
}
if ((Get-Item -LiteralPath $safeBackup).Length -le 0) { throw 'AURA_RESTORE_BACKUP_EMPTY' }
if ($DropAfterVerification -and $DropConfirmation -ne 'DROP_AURA_RESTORE_TEST') {
    throw 'AURA_RESTORE_DROP_CONFIRMATION_REQUIRED'
}

$previous = Import-AuraConfiguration -Profile 'staging'
$targetDatabase = 'aura_restore_test'
try {
    if ($env:AURA_DB_HOST -ne '127.0.0.1' -or $env:AURA_DB_PORT -ne '5432') { throw 'AURA_RESTORE_HOST_INVALID' }
    $pgPassPath = Assert-AuraPathWithin -Path $env:PGPASSFILE -Root $script:AuraSecretRoot
    Assert-AuraSecretAcl -Path $pgPassPath
    $env:PGPASSFILE = $pgPassPath
    $psql = (Get-Command psql.exe -ErrorAction Stop).Source
    $createdb = (Get-Command createdb.exe -ErrorAction Stop).Source
    $pgRestore = (Get-Command pg_restore.exe -ErrorAction Stop).Source
    $dropdb = (Get-Command dropdb.exe -ErrorAction Stop).Source
    $exists = & $psql --no-psqlrc --tuples-only --no-align --host=127.0.0.1 --port=5432 "--username=$($env:AURA_MIGRATION_USER)" --dbname=postgres --command="SELECT 1 FROM pg_database WHERE datname = 'aura_restore_test'"
    if ($LASTEXITCODE -ne 0 -or ($exists -join '').Trim() -ne '') { throw 'AURA_RESTORE_TARGET_NOT_EMPTY' }
    & $createdb --host=127.0.0.1 --port=5432 "--username=$($env:AURA_MIGRATION_USER)" --owner="$($env:AURA_MIGRATION_USER)" $targetDatabase
    if ($LASTEXITCODE -ne 0) { throw 'AURA_RESTORE_CREATE_FAILED' }
    & $pgRestore --exit-on-error --no-owner --no-privileges --host=127.0.0.1 --port=5432 "--username=$($env:AURA_MIGRATION_USER)" "--dbname=$targetDatabase" $safeBackup
    if ($LASTEXITCODE -ne 0) { throw 'AURA_RESTORE_COMMAND_FAILED' }
    $tableCount = & $psql --no-psqlrc --tuples-only --no-align --host=127.0.0.1 --port=5432 "--username=$($env:AURA_MIGRATION_USER)" "--dbname=$targetDatabase" --command="SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
    if ($LASTEXITCODE -ne 0 -or [int](($tableCount -join '').Trim()) -lt 1) { throw 'AURA_RESTORE_SCHEMA_INVALID' }
    $rowEstimate = & $psql --no-psqlrc --tuples-only --no-align --host=127.0.0.1 --port=5432 "--username=$($env:AURA_MIGRATION_USER)" "--dbname=$targetDatabase" --command="SELECT COALESCE(sum(n_live_tup), 0)::bigint FROM pg_stat_user_tables"
    if ($LASTEXITCODE -ne 0 -or (($rowEstimate -join '').Trim()) -notmatch '^[0-9]+$') { throw 'AURA_RESTORE_AGGREGATE_INVALID' }
    Write-Output ("AURA_RESTORE_OK database={0} tableCount={1} aggregateRowEstimate={2}" -f $targetDatabase, (($tableCount -join '').Trim()), (($rowEstimate -join '').Trim()))
    if ($DropAfterVerification) {
        & $dropdb --host=127.0.0.1 --port=5432 "--username=$($env:AURA_MIGRATION_USER)" $targetDatabase
        if ($LASTEXITCODE -ne 0) { throw 'AURA_RESTORE_DROP_FAILED' }
        Write-Output 'AURA_RESTORE_TEST_DATABASE_DROPPED'
    }
} catch {
    throw 'AURA_RESTORE_FAILED'
} finally {
    Restore-AuraProcessEnvironment -Previous $previous
}

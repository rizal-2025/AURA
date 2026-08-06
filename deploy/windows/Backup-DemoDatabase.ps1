[CmdletBinding()]
param(
    [ValidateSet('staging', 'production')]
    [string]$Profile = 'production'
)

. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')
Initialize-AuraDataDirectories
$previous = Import-AuraConfiguration -Profile $Profile
$tempPath = $null
try {
    $expectedDatabase = if ($Profile -eq 'production') { 'aura_demo_public' } else { 'aura_demo_staging' }
    if (
        $env:AURA_DB_HOST -ne '127.0.0.1' -or
        $env:AURA_DB_PORT -ne '5432' -or
        $env:AURA_DB_NAME -ne $expectedDatabase -or
        [string]::IsNullOrWhiteSpace($env:AURA_DB_USER)
    ) { throw 'AURA_BACKUP_DATABASE_INVALID' }
    $pgPassPath = Assert-AuraPathWithin -Path $env:PGPASSFILE -Root $script:AuraSecretRoot
    if (-not (Test-Path -LiteralPath $pgPassPath -PathType Leaf)) { throw 'AURA_PGPASSFILE_MISSING' }
    Assert-AuraSecretAcl -Path $pgPassPath
    $pgDump = (Get-Command pg_dump.exe -ErrorAction Stop).Source
    $timestamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
    $finalPath = Join-Path $script:AuraBackupRoot "${expectedDatabase}_$timestamp.dump"
    $tempPath = "$finalPath.partial"
    if ((Test-Path -LiteralPath $finalPath) -or (Test-Path -LiteralPath $tempPath)) { throw 'AURA_BACKUP_COLLISION' }
    $env:PGPASSFILE = $pgPassPath
    & $pgDump --format=custom --no-owner --no-privileges --host=127.0.0.1 --port=5432 "--username=$($env:AURA_DB_USER)" "--dbname=$expectedDatabase" "--file=$tempPath"
    if ($LASTEXITCODE -ne 0) { throw 'AURA_BACKUP_COMMAND_FAILED' }
    $backup = Get-Item -LiteralPath $tempPath
    if ($backup.Length -le 0) { throw 'AURA_BACKUP_EMPTY' }
    Move-Item -LiteralPath $tempPath -Destination $finalPath
    $retention = [int]$env:AURA_BACKUP_RETENTION_DAYS
    Remove-AuraExpiredFiles -Root $script:AuraBackupRoot -Filter 'aura_demo_*.dump' -RetentionDays $retention -PreservePath $finalPath
    Write-Output ("AURA_BACKUP_OK timestamp={0} bytes={1} database={2}" -f $timestamp, $backup.Length, $expectedDatabase)
} catch {
    if ($null -ne $tempPath -and (Test-Path -LiteralPath $tempPath)) {
        $safeTemp = Assert-AuraPathWithin -Path $tempPath -Root $script:AuraBackupRoot
        Remove-Item -LiteralPath $safeTemp -Force
    }
    throw 'AURA_BACKUP_FAILED'
} finally {
    Restore-AuraProcessEnvironment -Previous $previous
}

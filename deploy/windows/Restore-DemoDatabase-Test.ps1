[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('staging', 'production')]
    [string]$SourceProfile,
    [Parameter(Mandatory)][string]$BackupPath,
    [Parameter(Mandatory)][ValidateSet('RESTORE_TO_AURA_RESTORE_TEST')]
    [string]$Confirmation,
    [switch]$DropAfterVerification,
    [ValidateSet('', 'DROP_AURA_RESTORE_TEST')]
    [string]$DropConfirmation = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'AURA_ADMIN_REQUIRED'
}

$repositoryRoot = Get-AuraRepositoryRoot
$currentRoot = [System.IO.Path]::GetFullPath((Get-Location).Path).TrimEnd('\')
if ($currentRoot -ne $repositoryRoot.TrimEnd('\')) {
    throw 'AURA_RESTORE_REPOSITORY_ROOT_REQUIRED'
}

$safeBackup = Assert-AuraPathWithin -Path $BackupPath -Root $script:AuraBackupRoot
$expectedSourceDatabase = if ($SourceProfile -eq 'production') {
    'aura_demo_public'
} else {
    'aura_demo_staging'
}
$expectedNamePattern = '^' + [Regex]::Escape($expectedSourceDatabase) + `
    '_[0-9]{8}T[0-9]{6}Z\.dump$'
if (
    -not (Test-Path -LiteralPath $safeBackup -PathType Leaf) `
    -or [System.IO.Path]::GetFileName($safeBackup) -notmatch $expectedNamePattern
) {
    throw 'AURA_RESTORE_BACKUP_INVALID'
}
if ((Get-Item -LiteralPath $safeBackup).Length -le 0) {
    throw 'AURA_RESTORE_BACKUP_EMPTY'
}
Assert-AuraOperatorSecretAcl -Path $safeBackup
if ($DropAfterVerification -and $DropConfirmation -ne 'DROP_AURA_RESTORE_TEST') {
    throw 'AURA_RESTORE_DROP_CONFIRMATION_REQUIRED'
}

Assert-AuraOperatorSecretAcl -Path $script:AuraSecretRoot
$targetDatabase = 'aura_restore_test'
$migrationUser = 'aura_migration_owner'
$psql = Resolve-AuraPostgreSQLTool -ToolName 'psql.exe'
$createdb = Resolve-AuraPostgreSQLTool -ToolName 'createdb.exe'
$pgRestore = Resolve-AuraPostgreSQLTool -ToolName 'pg_restore.exe'
$dropdb = Resolve-AuraPostgreSQLTool -ToolName 'dropdb.exe'

function Set-AuraRestoreCredentialAcl {
    param([Parameter(Mandatory)][string]$Path)
    $icacls = (Get-Command icacls.exe -ErrorAction Stop).Source
    & $icacls $Path '/inheritance:r' '/grant:r' 'SYSTEM:F' `
        'Administrators:F' "$($identity.Name):F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'AURA_RESTORE_PGPASSFILE_ACL_FAILED' }
    Assert-AuraOperatorSecretAcl -Path $Path
}

function Invoke-AuraRestoreSchemaVerification {
    param([Parameter(Mandatory)][string]$CredentialPath)

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = Get-AuraPythonPath
    $startInfo.Arguments = '-B -m app.jobs.demo_schema --operation verify'
    $startInfo.WorkingDirectory = $repositoryRoot
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true
    foreach ($name in @(
        'DATABASE_URL', 'DEMO_DATABASE_URL', 'DEMO_BFF_SERVICE_TOKEN',
        'AUTH_JWT_SECRET', 'OPENAI_API_KEY', 'TELEGRAM_BOT_TOKEN',
        'TELEGRAM_IDENTITY_SECRET', 'TELEGRAM_OWNER_CHAT_ID'
    )) {
        [void]$startInfo.EnvironmentVariables.Remove($name)
    }
    foreach ($name in @($startInfo.EnvironmentVariables.Keys)) {
        if (([string]$name).StartsWith(
            'PG', [StringComparison]::OrdinalIgnoreCase
        )) {
            [void]$startInfo.EnvironmentVariables.Remove($name)
        }
    }
    $startInfo.EnvironmentVariables['APP_ENV'] = 'demo'
    $startInfo.EnvironmentVariables['AURA_DISABLE_DOTENV'] = '1'
    $startInfo.EnvironmentVariables['DEMO_DATABASE_URL'] = (
        "postgresql+psycopg://${migrationUser}@127.0.0.1:5432/$targetDatabase"
    )
    $startInfo.EnvironmentVariables['SQL_ECHO'] = 'false'
    $startInfo.EnvironmentVariables['PGPASSFILE'] = $CredentialPath

    $process = [System.Diagnostics.Process]::Start($startInfo)
    try {
        $standardOutput = $process.StandardOutput.ReadToEnd()
        $standardError = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        return ConvertFrom-AuraSchemaProcessResult `
            -Profile $SourceProfile `
            -Operation verify `
            -ExitCode $process.ExitCode `
            -StandardOutput $standardOutput `
            -StandardError $standardError
    } finally {
        $standardOutput = $null
        $standardError = $null
        $process.Dispose()
    }
}

$tempName = 'restore-migration.pgpass.' + [Guid]::NewGuid().ToString('N') + '.tmp'
$tempPath = Assert-AuraPathWithin `
    -Path (Join-Path $script:AuraSecretRoot $tempName) `
    -Root $script:AuraSecretRoot
$previousPgPass = [Environment]::GetEnvironmentVariable('PGPASSFILE', 'Process')

try {
    $securePassword = Read-Host `
        'Password for existing PostgreSQL role aura_migration_owner' -AsSecureString
    $bstr = [IntPtr]::Zero
    $plainPassword = $null
    $escapedPassword = $null
    $postgresEntry = $null
    $restoreEntry = $null
    try {
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
        $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        if ([string]::IsNullOrEmpty($plainPassword) -or $plainPassword -match '[\x00\r\n]') {
            throw 'AURA_RESTORE_MIGRATION_PASSWORD_INVALID'
        }
        $escapedPassword = $plainPassword.Replace('\', '\\').Replace(':', '\:')
        $postgresEntry = "127.0.0.1:5432:postgres:${migrationUser}:$escapedPassword"
        $restoreEntry = "127.0.0.1:5432:${targetDatabase}:${migrationUser}:$escapedPassword"
        [IO.File]::WriteAllText(
            $tempPath,
            $postgresEntry + [Environment]::NewLine + `
                $restoreEntry + [Environment]::NewLine,
            [Text.UTF8Encoding]::new($false)
        )
    } finally {
        $restoreEntry = $null
        $postgresEntry = $null
        $escapedPassword = $null
        $plainPassword = $null
        $securePassword.Dispose()
        if ($bstr -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }
    }
    Set-AuraRestoreCredentialAcl -Path $tempPath
    $env:PGPASSFILE = $tempPath

    & $pgRestore --list $safeBackup 1>$null 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'AURA_RESTORE_ARCHIVE_INVALID' }

    $databaseExistsSql = (
        "SELECT 1 FROM pg_database WHERE datname = '$targetDatabase'"
    )
    $exists = & $psql --no-psqlrc --tuples-only --no-align `
        --host=127.0.0.1 --port=5432 "--username=$migrationUser" `
        --dbname=postgres "--command=$databaseExistsSql" 2>$null
    if ($LASTEXITCODE -ne 0 -or ($exists -join '').Trim() -ne '') {
        throw 'AURA_RESTORE_TARGET_NOT_EMPTY'
    }

    & $createdb --host=127.0.0.1 --port=5432 `
        "--username=$migrationUser" "--owner=$migrationUser" `
        $targetDatabase 1>$null 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'AURA_RESTORE_CREATE_FAILED' }

    & $pgRestore --exit-on-error --no-owner --no-privileges `
        --host=127.0.0.1 --port=5432 "--username=$migrationUser" `
        "--dbname=$targetDatabase" $safeBackup 1>$null 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'AURA_RESTORE_COMMAND_FAILED' }

    $verification = Invoke-AuraRestoreSchemaVerification -CredentialPath $tempPath
    if (
        $verification.status -ne 'verified' `
        -or $verification.classification -ne 'converged' `
        -or [int]$verification.expectedTableCount -ne 10 `
        -or [int]$verification.actualTableCount -ne 10
    ) {
        throw 'AURA_RESTORE_SCHEMA_INVALID'
    }

    $rowEstimateSql = (
        'SELECT COALESCE(sum(n_live_tup), 0)::bigint FROM pg_stat_user_tables'
    )
    $rowEstimate = & $psql --no-psqlrc --tuples-only --no-align `
        --host=127.0.0.1 --port=5432 "--username=$migrationUser" `
        "--dbname=$targetDatabase" "--command=$rowEstimateSql" 2>$null
    if (
        $LASTEXITCODE -ne 0 `
        -or (($rowEstimate -join '').Trim()) -notmatch '^[0-9]+$'
    ) {
        throw 'AURA_RESTORE_AGGREGATE_INVALID'
    }
    Write-Output (
        'AURA_RESTORE_OK database={0} tableCount=10 aggregateRowEstimate={1}' -f `
            $targetDatabase, (($rowEstimate -join '').Trim())
    )

    if ($DropAfterVerification) {
        & $dropdb --host=127.0.0.1 --port=5432 `
            "--username=$migrationUser" $targetDatabase 1>$null 2>$null
        if ($LASTEXITCODE -ne 0) { throw 'AURA_RESTORE_DROP_FAILED' }
        Write-Output 'AURA_RESTORE_TEST_DATABASE_DROPPED'
    }
} catch {
    throw 'AURA_RESTORE_FAILED'
} finally {
    $verification = $null
    $rowEstimate = $null
    [Environment]::SetEnvironmentVariable(
        'PGPASSFILE', $previousPgPass, 'Process'
    )
    if (Test-Path -LiteralPath $tempPath -PathType Leaf) {
        Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
    }
}

[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('staging', 'production')]
    [string]$SourceProfile,
    [Parameter(Mandatory)][string]$BackupPath,
    [Parameter(Mandatory)][ValidateSet('VERIFY_EXISTING_AURA_RESTORE_TEST')]
    [string]$Confirmation
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
$currentRoot = [IO.Path]::GetFullPath((Get-Location).Path).TrimEnd('\')
if ($currentRoot -ne $repositoryRoot.TrimEnd('\')) {
    throw 'AURA_RESTORE_EXISTING_REPOSITORY_ROOT_REQUIRED'
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
    -or [IO.Path]::GetFileName($safeBackup) -notmatch $expectedNamePattern `
    -or (Get-Item -LiteralPath $safeBackup).Length -le 0
) {
    throw 'AURA_RESTORE_EXISTING_BACKUP_INVALID'
}
Assert-AuraOperatorSecretAcl -Path $safeBackup
Assert-AuraOperatorSecretAcl -Path $script:AuraSecretRoot

$targetDatabase = 'aura_restore_test'
$migrationUser = 'aura_migration_owner'
$psql = Resolve-AuraPostgreSQLTool -ToolName 'psql.exe'

function Invoke-AuraExistingRestoreSchemaVerification {
    param([Parameter(Mandatory)][string]$CredentialPath)

    $startInfo = [Diagnostics.ProcessStartInfo]::new()
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
    $startInfo.EnvironmentVariables['PGOPTIONS'] = (
        '-c default_transaction_read_only=on'
    )

    $process = [Diagnostics.Process]::Start($startInfo)
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

$tempName = 'restore-existing-verification.pgpass.' + `
    [Guid]::NewGuid().ToString('N') + '.tmp'
$tempPath = Assert-AuraPathWithin `
    -Path (Join-Path $script:AuraSecretRoot $tempName) `
    -Root $script:AuraSecretRoot
$previousPgPass = [Environment]::GetEnvironmentVariable('PGPASSFILE', 'Process')
$previousPgOptions = [Environment]::GetEnvironmentVariable('PGOPTIONS', 'Process')
$failureCode = 'AURA_RESTORE_EXISTING_CREDENTIAL_STAGE_FAILED'

try {
    $securePassword = Read-Host `
        'Password for existing PostgreSQL role aura_migration_owner' -AsSecureString
    $bstr = [IntPtr]::Zero
    $plainPassword = $null
    $escapedPassword = $null
    $restoreEntry = $null
    try {
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
        $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        if ([string]::IsNullOrEmpty($plainPassword) -or $plainPassword -match '[\x00\r\n]') {
            throw 'AURA_RESTORE_EXISTING_MIGRATION_PASSWORD_INVALID'
        }
        $escapedPassword = $plainPassword.Replace('\', '\\').Replace(':', '\:')
        $restoreEntry = (
            "127.0.0.1:5432:${targetDatabase}:${migrationUser}:$escapedPassword"
        )
        [IO.File]::WriteAllText(
            $tempPath,
            $restoreEntry + [Environment]::NewLine,
            [Text.UTF8Encoding]::new($false)
        )
    } finally {
        $restoreEntry = $null
        $escapedPassword = $null
        $plainPassword = $null
        $securePassword.Dispose()
        if ($bstr -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }
    }
    Set-AuraOperatorProtectedAcl -Path $tempPath
    $env:PGPASSFILE = $tempPath
    $env:PGOPTIONS = '-c default_transaction_read_only=on'

    $failureCode = 'AURA_RESTORE_EXISTING_SCHEMA_VERIFICATION_STAGE_FAILED'
    $verification = Invoke-AuraExistingRestoreSchemaVerification `
        -CredentialPath $tempPath

    $failureCode = 'AURA_RESTORE_EXISTING_AGGREGATE_VERIFICATION_STAGE_FAILED'
    $rowEstimateSql = (
        'SELECT COALESCE(sum(n_live_tup), 0)::bigint FROM pg_stat_user_tables'
    )
    $rowEstimate = & $psql --no-psqlrc --set=ON_ERROR_STOP=1 `
        --tuples-only --no-align --host=127.0.0.1 --port=5432 `
        "--username=$migrationUser" "--dbname=$targetDatabase" `
        "--command=$rowEstimateSql" 2>$null
    if (
        $LASTEXITCODE -ne 0 `
        -or (($rowEstimate -join '').Trim()) -notmatch '^[0-9]+$'
    ) {
        throw 'AURA_RESTORE_EXISTING_AGGREGATE_INVALID'
    }

    Write-Output (
        (
            'AURA_RESTORE_EXISTING_VERIFIED database={0} tables={1}/{2} ' +
            'columns={3}/{4} primaryKeys={5} structures={6} ' +
            'aggregateRowEstimate={7} readOnly=true'
        ) -f `
            $targetDatabase,
            [int]$verification.actualTableCount,
            [int]$verification.expectedTableCount,
            [int]$verification.matchingColumnCount,
            [int]$verification.expectedColumnCount,
            [int]$verification.matchingPrimaryKeyCount,
            [int]$verification.matchingTableStructureCount,
            (($rowEstimate -join '').Trim())
    )
} catch {
    throw $failureCode
} finally {
    $verification = $null
    $rowEstimate = $null
    [Environment]::SetEnvironmentVariable(
        'PGPASSFILE', $previousPgPass, 'Process'
    )
    [Environment]::SetEnvironmentVariable(
        'PGOPTIONS', $previousPgOptions, 'Process'
    )
    if (Test-Path -LiteralPath $tempPath -PathType Leaf) {
        Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
    }
}

[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('DROP_AURA_RESTORE_TEST')]
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
    throw 'AURA_RESTORE_CLEANUP_REPOSITORY_ROOT_REQUIRED'
}
Assert-AuraOperatorSecretAcl -Path $script:AuraSecretRoot

$targetDatabase = 'aura_restore_test'
$migrationUser = 'aura_migration_owner'
$psql = Resolve-AuraPostgreSQLTool -ToolName 'psql.exe'
$dropdb = Resolve-AuraPostgreSQLTool -ToolName 'dropdb.exe'

function Invoke-AuraCleanupSchemaVerification {
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
            -Profile production `
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

$tempName = 'restore-cleanup.pgpass.' + [Guid]::NewGuid().ToString('N') + '.tmp'
$tempPath = Assert-AuraPathWithin `
    -Path (Join-Path $script:AuraSecretRoot $tempName) `
    -Root $script:AuraSecretRoot
$previousPgPass = [Environment]::GetEnvironmentVariable('PGPASSFILE', 'Process')
$previousPgOptions = [Environment]::GetEnvironmentVariable('PGOPTIONS', 'Process')
$failureCode = 'AURA_RESTORE_CLEANUP_CREDENTIAL_STAGE_FAILED'

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
            throw 'AURA_RESTORE_CLEANUP_PASSWORD_INVALID'
        }
        $escapedPassword = $plainPassword.Replace('\', '\\').Replace(':', '\:')
        $postgresEntry = (
            "127.0.0.1:5432:postgres:${migrationUser}:$escapedPassword"
        )
        $restoreEntry = (
            "127.0.0.1:5432:${targetDatabase}:${migrationUser}:$escapedPassword"
        )
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
    Set-AuraOperatorProtectedAcl -Path $tempPath
    $env:PGPASSFILE = $tempPath
    $env:PGOPTIONS = '-c default_transaction_read_only=on'

    $failureCode = 'AURA_RESTORE_CLEANUP_TARGET_PREFLIGHT_STAGE_FAILED'
    $databaseIdentitySql = (
        "SELECT datname || '|' || pg_get_userbyid(datdba) " +
        "FROM pg_database WHERE datname = '$targetDatabase'"
    )
    $databaseIdentity = & $psql --no-psqlrc --set=ON_ERROR_STOP=1 `
        --tuples-only --no-align --host=127.0.0.1 --port=5432 `
        "--username=$migrationUser" --dbname=postgres `
        "--command=$databaseIdentitySql" 2>$null
    if (
        $LASTEXITCODE -ne 0 `
        -or (($databaseIdentity -join '').Trim()) -ne `
            "${targetDatabase}|${migrationUser}"
    ) {
        throw 'AURA_RESTORE_CLEANUP_TARGET_INVALID'
    }

    $failureCode = 'AURA_RESTORE_CLEANUP_SCHEMA_VERIFICATION_STAGE_FAILED'
    $verification = Invoke-AuraCleanupSchemaVerification -CredentialPath $tempPath
    if (
        [int]$verification.actualTableCount -ne 10 `
        -or [int]$verification.matchingColumnCount -ne 88 `
        -or [int]$verification.matchingPrimaryKeyCount -ne 10 `
        -or [int]$verification.matchingTableStructureCount -ne 10
    ) {
        throw 'AURA_RESTORE_CLEANUP_SCHEMA_INVALID'
    }

    $failureCode = 'AURA_RESTORE_CLEANUP_DROP_STAGE_FAILED'
    [Environment]::SetEnvironmentVariable('PGOPTIONS', $null, 'Process')
    & $dropdb --host=127.0.0.1 --port=5432 `
        "--username=$migrationUser" --maintenance-db=postgres `
        $targetDatabase 1>$null 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'AURA_RESTORE_CLEANUP_DROP_FAILED' }

    $failureCode = 'AURA_RESTORE_CLEANUP_POSTCHECK_STAGE_FAILED'
    $env:PGOPTIONS = '-c default_transaction_read_only=on'
    $existsSql = "SELECT 1 FROM pg_database WHERE datname = '$targetDatabase'"
    $exists = & $psql --no-psqlrc --set=ON_ERROR_STOP=1 `
        --tuples-only --no-align --host=127.0.0.1 --port=5432 `
        "--username=$migrationUser" --dbname=postgres `
        "--command=$existsSql" 2>$null
    if ($LASTEXITCODE -ne 0 -or -not [string]::IsNullOrWhiteSpace(($exists -join ''))) {
        throw 'AURA_RESTORE_CLEANUP_POSTCHECK_FAILED'
    }
    Write-Output 'AURA_RESTORE_TEST_CLEANUP_OK database=aura_restore_test'
} catch {
    throw $failureCode
} finally {
    $verification = $null
    $databaseIdentity = $null
    $exists = $null
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

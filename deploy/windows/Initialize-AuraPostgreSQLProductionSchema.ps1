[CmdletBinding()]
param()

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
    throw 'AURA_PRODUCTION_SCHEMA_REPOSITORY_ROOT_REQUIRED'
}
Assert-AuraOperatorSecretAcl -Path $script:AuraSecretRoot

$expectedRuntimeUrl = (
    'postgresql+psycopg://aura_public_runtime@127.0.0.1:5432/aura_demo_public'
)
$migrationUrl = (
    'postgresql+psycopg://aura_migration_owner@127.0.0.1:5432/aura_demo_public'
)
$previous = Import-AuraConfiguration -Profile production
try {
    if (
        $env:DEMO_DATABASE_URL -ne $expectedRuntimeUrl `
        -or $env:AURA_DB_HOST -ne '127.0.0.1' `
        -or $env:AURA_DB_PORT -ne '5432' `
        -or $env:AURA_DB_NAME -ne 'aura_demo_public' `
        -or $env:AURA_DB_USER -ne 'aura_public_runtime' `
        -or $env:AURA_MIGRATION_USER -ne 'aura_migration_owner'
    ) {
        throw 'AURA_PRODUCTION_DATABASE_PROFILE_INVALID'
    }
} finally {
    Restore-AuraProcessEnvironment -Previous $previous
}

function Set-AuraProductionSchemaCredentialAcl {
    param([Parameter(Mandatory)][string]$Path)
    $icacls = (Get-Command icacls.exe -ErrorAction Stop).Source
    & $icacls $Path '/inheritance:r' '/grant:r' 'SYSTEM:F' `
        'Administrators:F' "$($identity.Name):F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'AURA_PRODUCTION_SCHEMA_PGPASSFILE_ACL_FAILED' }
    Assert-AuraOperatorSecretAcl -Path $Path
}

function Invoke-AuraProductionSchemaOperation {
    param(
        [Parameter(Mandatory)]
        [ValidateSet('plan', 'apply-empty-schema', 'verify')]
        [string]$Operation,
        [Parameter(Mandatory)][string]$CredentialPath
    )

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = Get-AuraPythonPath
    $startInfo.Arguments = "-B -m app.jobs.demo_schema --operation $Operation"
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
    $startInfo.EnvironmentVariables['DEMO_DATABASE_URL'] = $migrationUrl
    $startInfo.EnvironmentVariables['SQL_ECHO'] = 'false'
    $startInfo.EnvironmentVariables['PGPASSFILE'] = $CredentialPath

    $process = [System.Diagnostics.Process]::Start($startInfo)
    try {
        $standardOutput = $process.StandardOutput.ReadToEnd()
        $standardError = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        return ConvertFrom-AuraSchemaProcessResult `
            -Profile production `
            -Operation $Operation `
            -ExitCode $process.ExitCode `
            -StandardOutput $standardOutput `
            -StandardError $standardError
    } finally {
        $standardOutput = $null
        $standardError = $null
        $process.Dispose()
    }
}

$tempName = 'production-migration.pgpass.' + [Guid]::NewGuid().ToString('N') + '.tmp'
$tempPath = Assert-AuraPathWithin `
    -Path (Join-Path $script:AuraSecretRoot $tempName) `
    -Root $script:AuraSecretRoot

try {
    $securePassword = Read-Host `
        'Password for existing PostgreSQL role aura_migration_owner' -AsSecureString
    $bstr = [IntPtr]::Zero
    $plainPassword = $null
    $escapedPassword = $null
    $entry = $null
    try {
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
        $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        if ([string]::IsNullOrEmpty($plainPassword) -or $plainPassword -match '[\x00\r\n]') {
            throw 'AURA_PRODUCTION_MIGRATION_PASSWORD_INVALID'
        }
        $escapedPassword = $plainPassword.Replace('\', '\\').Replace(':', '\:')
        $entry = "127.0.0.1:5432:aura_demo_public:aura_migration_owner:$escapedPassword"
        [IO.File]::WriteAllText(
            $tempPath,
            $entry + [Environment]::NewLine,
            [Text.UTF8Encoding]::new($false)
        )
    } finally {
        $entry = $null
        $escapedPassword = $null
        $plainPassword = $null
        $securePassword.Dispose()
        if ($bstr -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }
    }
    Set-AuraProductionSchemaCredentialAcl -Path $tempPath

    $plan = Invoke-AuraProductionSchemaOperation `
        -Operation plan -CredentialPath $tempPath
    if (
        $plan.status -ne 'ready' `
        -or $plan.classification -ne 'additive-empty-schema' `
        -or [int]$plan.expectedTableCount -ne 10 `
        -or [int]$plan.actualTableCount -ne 0
    ) {
        throw 'AURA_PRODUCTION_SCHEMA_NOT_EMPTY'
    }
    $applied = Invoke-AuraProductionSchemaOperation `
        -Operation apply-empty-schema -CredentialPath $tempPath
    if (
        $applied.status -ne 'verified' `
        -or $applied.classification -ne 'converged' `
        -or [int]$applied.expectedTableCount -ne 10 `
        -or [int]$applied.actualTableCount -ne 10
    ) {
        throw 'AURA_PRODUCTION_SCHEMA_APPLY_FAILED'
    }
    $verified = Invoke-AuraProductionSchemaOperation `
        -Operation verify -CredentialPath $tempPath
    if (
        $verified.status -ne 'verified' `
        -or $verified.classification -ne 'converged' `
        -or [int]$verified.expectedTableCount -ne 10 `
        -or [int]$verified.actualTableCount -ne 10
    ) {
        throw 'AURA_PRODUCTION_SCHEMA_VERIFY_FAILED'
    }
} finally {
    $plan = $null
    $applied = $null
    $verified = $null
    if (Test-Path -LiteralPath $tempPath -PathType Leaf) {
        Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
    }
}
Write-Output 'AURA_PRODUCTION_SCHEMA_INITIALIZED'

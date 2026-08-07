[CmdletBinding()]
param([switch]$ReplaceExisting)

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
    throw 'AURA_STAGING_CREDENTIAL_REPOSITORY_ROOT_REQUIRED'
}

Assert-AuraOperatorSecretAcl -Path $script:AuraSecretRoot
$expectedPgPassPath = 'C:\ProgramData\AURA\secrets\staging.pgpass'
$pgPassPath = Assert-AuraPathWithin `
    -Path (Join-Path $script:AuraSecretRoot 'staging.pgpass') `
    -Root $script:AuraSecretRoot
if ($pgPassPath -ne $expectedPgPassPath) {
    throw 'AURA_STAGING_PGPASSFILE_PATH_INVALID'
}

$previous = Import-AuraConfiguration -Profile staging
try {
    if (
        $env:DEMO_DATABASE_URL -ne 'postgresql+psycopg://aura_staging_runtime@127.0.0.1:5432/aura_demo_staging' `
        -or $env:AURA_DB_HOST -ne '127.0.0.1' `
        -or $env:AURA_DB_PORT -ne '5432' `
        -or $env:AURA_DB_NAME -ne 'aura_demo_staging' `
        -or $env:AURA_DB_USER -ne 'aura_staging_runtime' `
        -or $env:AURA_MIGRATION_USER -ne 'aura_migration_owner' `
        -or $env:PGPASSFILE -ne $expectedPgPassPath
    ) {
        throw 'AURA_STAGING_DATABASE_PROFILE_INVALID'
    }
} finally {
    Restore-AuraProcessEnvironment -Previous $previous
}

$credentialExists = Test-Path -LiteralPath $pgPassPath -PathType Leaf
if ($credentialExists -and -not $ReplaceExisting) {
    throw 'AURA_STAGING_PGPASSFILE_ALREADY_EXISTS'
}
if (-not $credentialExists -and $ReplaceExisting) {
    throw 'AURA_STAGING_PGPASSFILE_MISSING_FOR_REPLACEMENT'
}
if ($credentialExists) {
    Assert-AuraOperatorSecretAcl -Path $pgPassPath
}

function Set-AuraStagingCredentialAcl {
    param([Parameter(Mandatory)][string]$Path)
    $icacls = (Get-Command icacls.exe -ErrorAction Stop).Source
    & $icacls $Path '/inheritance:r' '/grant:r' 'SYSTEM:F' `
        'Administrators:F' "$($identity.Name):F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'AURA_STAGING_PGPASSFILE_ACL_FAILED' }
    Assert-AuraOperatorSecretAcl -Path $Path
}

function Test-AuraStagingCredential {
    param([Parameter(Mandatory)][string]$CredentialPath)

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = Get-AuraPythonPath
    $startInfo.Arguments = '-B -m tools.postgresql_staging_preflight'
    $startInfo.WorkingDirectory = $repositoryRoot
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true
    foreach ($name in @(
        'DATABASE_URL', 'DEMO_DATABASE_URL', 'DEMO_BFF_SERVICE_TOKEN',
        'OPENAI_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_IDENTITY_SECRET',
        'TELEGRAM_OWNER_CHAT_ID'
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
        'postgresql+psycopg://aura_staging_runtime@127.0.0.1:5432/aura_demo_staging'
    )
    $startInfo.EnvironmentVariables['PGPASSFILE'] = $CredentialPath

    $process = [System.Diagnostics.Process]::Start($startInfo)
    try {
        $standardOutput = $process.StandardOutput.ReadToEnd()
        $standardError = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        return (
            $process.ExitCode -eq 0 `
            -and $standardOutput.Contains('AURA_POSTGRESQL_STAGING_PREFLIGHT_OK') `
            -and [string]::IsNullOrEmpty($standardError)
        )
    } finally {
        $standardOutput = $null
        $standardError = $null
        $process.Dispose()
    }
}

function Replace-AuraFileWithoutBackup {
    param(
        [Parameter(Mandatory)][string]$SourcePath,
        [Parameter(Mandatory)][string]$DestinationPath
    )

    $replaceMethod = [IO.File].GetMethod(
        'Replace',
        [Type[]]@([string], [string], [string])
    )
    if ($null -eq $replaceMethod) {
        throw 'AURA_STAGING_PGPASSFILE_REPLACE_UNAVAILABLE'
    }
    try {
        [void]$replaceMethod.Invoke(
            $null,
            [object[]]@($SourcePath, $DestinationPath, $null)
        )
    } catch {
        throw 'AURA_STAGING_PGPASSFILE_REPLACE_FAILED'
    }
}

$tempName = 'staging.pgpass.' + [Guid]::NewGuid().ToString('N') + '.tmp'
$tempPath = Assert-AuraPathWithin `
    -Path (Join-Path $script:AuraSecretRoot $tempName) `
    -Root $script:AuraSecretRoot

try {
    $securePassword = Read-Host `
        'Password for existing PostgreSQL role aura_staging_runtime' -AsSecureString
    $bstr = [IntPtr]::Zero
    $plainPassword = $null
    $escapedPassword = $null
    $entry = $null
    try {
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
        $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        if ([string]::IsNullOrEmpty($plainPassword) -or $plainPassword -match '[\x00\r\n]') {
            throw 'AURA_STAGING_DATABASE_PASSWORD_INVALID'
        }
        $escapedPassword = $plainPassword.Replace('\', '\\').Replace(':', '\:')
        $entry = "127.0.0.1:5432:aura_demo_staging:aura_staging_runtime:$escapedPassword"
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
    Set-AuraStagingCredentialAcl -Path $tempPath
    if (-not (Test-AuraStagingCredential -CredentialPath $tempPath)) {
        throw 'AURA_STAGING_CREDENTIAL_VALIDATION_FAILED'
    }
    if ($ReplaceExisting) {
        Replace-AuraFileWithoutBackup `
            -SourcePath $tempPath -DestinationPath $pgPassPath
    } else {
        [IO.File]::Move($tempPath, $pgPassPath)
    }
    Assert-AuraOperatorSecretAcl -Path $pgPassPath
} finally {
    if (Test-Path -LiteralPath $tempPath -PathType Leaf) {
        Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
    }
}
if ($ReplaceExisting) {
    Write-Output 'AURA_STAGING_PGPASSFILE_UPDATED'
} else {
    Write-Output 'AURA_STAGING_PGPASSFILE_CREATED'
}

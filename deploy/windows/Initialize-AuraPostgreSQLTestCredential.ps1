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
    throw 'AURA_TEST_CREDENTIAL_REPOSITORY_ROOT_REQUIRED'
}

if (-not (Test-Path -LiteralPath $script:AuraSecretRoot -PathType Container)) {
    [void](New-Item -ItemType Directory -Path $script:AuraSecretRoot -Force)
    $icacls = (Get-Command icacls.exe -ErrorAction Stop).Source
    & $icacls $script:AuraSecretRoot '/inheritance:r' `
        '/grant:r' 'SYSTEM:(OI)(CI)F' 'Administrators:(OI)(CI)F' `
        "$($identity.Name):(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'AURA_SECRET_ROOT_ACL_FAILED' }
}
Assert-AuraOperatorSecretAcl -Path $script:AuraSecretRoot

$expectedPgPassPath = 'C:\ProgramData\AURA\secrets\test.pgpass'
$pgPassPath = Assert-AuraPathWithin `
    -Path (Join-Path $script:AuraSecretRoot 'test.pgpass') `
    -Root $script:AuraSecretRoot
if ($pgPassPath -ne $expectedPgPassPath) {
    throw 'AURA_TEST_PGPASSFILE_PATH_INVALID'
}
$credentialExists = Test-Path -LiteralPath $pgPassPath -PathType Leaf
if ($credentialExists -and -not $ReplaceExisting) {
    throw 'AURA_TEST_PGPASSFILE_ALREADY_EXISTS'
}
if (-not $credentialExists -and $ReplaceExisting) {
    throw 'AURA_TEST_PGPASSFILE_MISSING_FOR_REPLACEMENT'
}
if ($credentialExists) {
    Assert-AuraOperatorSecretAcl -Path $pgPassPath
}

function Set-AuraTestCredentialAcl {
    param([Parameter(Mandatory)][string]$Path)
    $icacls = (Get-Command icacls.exe -ErrorAction Stop).Source
    & $icacls $Path '/inheritance:r' '/grant:r' 'SYSTEM:F' `
        'Administrators:F' "$($identity.Name):F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'AURA_TEST_PGPASSFILE_ACL_FAILED' }
    Assert-AuraOperatorSecretAcl -Path $Path
}

function Test-AuraTestCredential {
    param([Parameter(Mandatory)][string]$CredentialPath)

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = Get-AuraPythonPath
    $startInfo.Arguments = '-B -m tools.postgresql_test_preflight'
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
    $startInfo.EnvironmentVariables['APP_ENV'] = 'test'
    $startInfo.EnvironmentVariables['AURA_DISABLE_DOTENV'] = '1'
    $startInfo.EnvironmentVariables['TEST_DATABASE_URL'] = (
        'postgresql+psycopg://aura_test_runner@127.0.0.1:5432/aura_test'
    )
    $startInfo.EnvironmentVariables['PGPASSFILE'] = $CredentialPath

    $process = [System.Diagnostics.Process]::Start($startInfo)
    try {
        $standardOutput = $process.StandardOutput.ReadToEnd()
        $standardError = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        return (
            $process.ExitCode -eq 0 `
            -and $standardOutput.Contains('AURA_POSTGRESQL_PREFLIGHT_OK') `
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
        throw 'AURA_TEST_PGPASSFILE_REPLACE_UNAVAILABLE'
    }
    try {
        [void]$replaceMethod.Invoke(
            $null,
            [object[]]@($SourcePath, $DestinationPath, $null)
        )
    } catch {
        throw 'AURA_TEST_PGPASSFILE_REPLACE_FAILED'
    }
}

$tempName = 'test.pgpass.' + [Guid]::NewGuid().ToString('N') + '.tmp'
$tempPath = Assert-AuraPathWithin `
    -Path (Join-Path $script:AuraSecretRoot $tempName) `
    -Root $script:AuraSecretRoot

try {
    $securePassword = Read-Host `
        'Password for existing PostgreSQL role aura_test_runner' -AsSecureString
    $bstr = [IntPtr]::Zero
    $plainPassword = $null
    $escapedPassword = $null
    $entry = $null
    try {
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
        $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        if ([string]::IsNullOrEmpty($plainPassword) -or $plainPassword -match '[\x00\r\n]') {
            throw 'AURA_TEST_DATABASE_PASSWORD_INVALID'
        }
        $escapedPassword = $plainPassword.Replace('\', '\\').Replace(':', '\:')
        $entry = "127.0.0.1:5432:aura_test:aura_test_runner:$escapedPassword"
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
    Set-AuraTestCredentialAcl -Path $tempPath
    if (-not (Test-AuraTestCredential -CredentialPath $tempPath)) {
        throw 'AURA_TEST_CREDENTIAL_VALIDATION_FAILED'
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
    Write-Output 'AURA_TEST_PGPASSFILE_UPDATED'
} else {
    Write-Output 'AURA_TEST_PGPASSFILE_CREATED'
}

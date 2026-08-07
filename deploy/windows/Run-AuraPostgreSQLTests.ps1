[CmdletBinding()]
param(
    [switch]$Focused,
    [switch]$PreflightOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')

$repositoryRoot = Get-AuraRepositoryRoot
$currentRoot = [System.IO.Path]::GetFullPath((Get-Location).Path).TrimEnd('\')
if ($currentRoot -ne $repositoryRoot.TrimEnd('\')) {
    throw 'AURA_TEST_RUNNER_REPOSITORY_ROOT_REQUIRED'
}

$python = Get-AuraPythonPath
$pgPassPath = Assert-AuraPathWithin `
    -Path (Join-Path $script:AuraSecretRoot 'test.pgpass') `
    -Root $script:AuraSecretRoot
if (-not (Test-Path -LiteralPath $pgPassPath -PathType Leaf)) {
    throw 'AURA_TEST_PGPASSFILE_MISSING'
}
Assert-AuraOperatorSecretAcl -Path $pgPassPath
if ((Get-Item -LiteralPath $pgPassPath).Length -le 0) {
    throw 'AURA_TEST_PGPASSFILE_EMPTY'
}
Write-Output 'PGPASSFILE present: yes'
Write-Output 'credential file ACL protected: yes'

function New-AuraPostgreSQLTestProcess {
    param([Parameter(Mandatory)][string]$Arguments)

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $python
    $startInfo.Arguments = $Arguments
    $startInfo.WorkingDirectory = $repositoryRoot
    $startInfo.UseShellExecute = $false

    foreach ($name in @(
        'OPENAI_API_KEY', 'DEMO_DATABASE_URL', 'DEMO_BFF_SERVICE_TOKEN',
        'TELEGRAM_BOT_TOKEN', 'TELEGRAM_IDENTITY_SECRET',
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
    foreach ($name in @(
        'TELEGRAM_CLEAR_WEBHOOK_ON_START', 'TELEGRAM_DROP_PENDING_UPDATES',
        'TELEGRAM_OWNER_NOTIFICATIONS_ENABLED', 'TELEGRAM_OWNER_COMMANDS_ENABLED',
        'TELEGRAM_POLL_TIMEOUT_SECONDS',
        'TELEGRAM_OWNER_NOTIFICATION_POLL_SECONDS',
        'TELEGRAM_OWNER_NOTIFICATION_MAX_ATTEMPTS',
        'TELEGRAM_OWNER_NOTIFICATION_RETRY_BASE_SECONDS',
        'TELEGRAM_OWNER_NOTIFICATION_LEASE_SECONDS'
    )) {
        [void]$startInfo.EnvironmentVariables.Remove($name)
    }

    $startInfo.EnvironmentVariables['APP_ENV'] = 'test'
    $startInfo.EnvironmentVariables['AURA_DISABLE_DOTENV'] = '1'
    $startInfo.EnvironmentVariables['DATABASE_URL'] = 'sqlite+pysqlite:///:memory:'
    $startInfo.EnvironmentVariables['SQL_ECHO'] = 'false'
    $startInfo.EnvironmentVariables['AUTH_JWT_SECRET'] = 'aura-scoped-jwt-material-for-local-unittest-runner-2026'
    $startInfo.EnvironmentVariables['AUTH_JWT_ISSUER'] = 'aura'
    $startInfo.EnvironmentVariables['AUTH_JWT_AUDIENCE'] = 'aura-api'
    $startInfo.EnvironmentVariables['AUTH_JWT_EXPIRE_MINUTES'] = '60'
    $startInfo.EnvironmentVariables['AI_PROVIDER'] = 'ollama'
    $startInfo.EnvironmentVariables['OLLAMA_BASE_URL'] = 'http://127.0.0.1:9/v1'
    $startInfo.EnvironmentVariables['OLLAMA_MODEL'] = 'aura-local-tests-no-live-provider'
    [void]$startInfo.EnvironmentVariables.Remove('AI_PROVIDER_TIMEOUT_SECONDS')
    $startInfo.EnvironmentVariables['PGPASSFILE'] = $pgPassPath
    $startInfo.EnvironmentVariables['TEST_DATABASE_URL'] = (
        'postgresql+psycopg://aura_test_runner@127.0.0.1:5432/aura_test'
    )

    return [System.Diagnostics.Process]::Start($startInfo)
}

$preflight = New-AuraPostgreSQLTestProcess `
    -Arguments '-B -m tools.postgresql_test_preflight'
$preflight.WaitForExit()
$preflightExitCode = $preflight.ExitCode
$preflight.Dispose()
if ($preflightExitCode -ne 0) { exit $preflightExitCode }
if ($PreflightOnly) { exit 0 }

$focusedArguments = (
    '-m unittest tests.integration.test_public_reservation_api_postgresql -v'
)
$focusedTests = New-AuraPostgreSQLTestProcess -Arguments $focusedArguments
$focusedTests.WaitForExit()
$focusedExitCode = $focusedTests.ExitCode
$focusedTests.Dispose()
if ($focusedExitCode -ne 0) { exit $focusedExitCode }
if ($Focused) { exit 0 }

$fullArguments = '-m unittest discover -s tests -p "test_*.py" -v'
$fullTests = New-AuraPostgreSQLTestProcess -Arguments $fullArguments
$fullTests.WaitForExit()
$fullExitCode = $fullTests.ExitCode
$fullTests.Dispose()
exit $fullExitCode

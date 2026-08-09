[CmdletBinding()]
param(
    [string]$Profile = 'production',
    [ValidateSet('DryRun', 'Execute')]
    [string]$Mode = 'DryRun',
    [string]$Confirmation = ''
)

. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')
$startedAt = [Diagnostics.Stopwatch]::StartNew()
$operationMode = if ($Mode -eq 'DryRun') { 'dry-run' } else { 'execute' }
$eligibleSessions = 0
$attemptedSessions = 0
$successfulCleanupCount = 0
$failedCleanupCount = 0
$operationResult = 'failure'
$previous = $null
$dotenvPrevious = [Environment]::GetEnvironmentVariable(
    'AURA_DISABLE_DOTENV',
    'Process'
)
$locationPushed = $false

try {
    Assert-AuraProductionProfile -Profile $Profile
    if ($Mode -eq 'Execute' -and $Confirmation -cne 'RUN_AURA_DEMO_CLEANUP') {
        throw 'AURA_CLEANUP_CONFIRMATION_REQUIRED'
    }

    $repositoryRoot = Assert-AuraRepositoryLayout
    Push-Location -LiteralPath $repositoryRoot
    $locationPushed = $true
    Initialize-AuraDataDirectories

    $configPath = Get-AuraSecretPath -Profile production
    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
        throw 'AURA_PRODUCTION_CONFIG_MISSING'
    }
    Assert-AuraOperatorSecretAcl -Path $configPath
    $pgPassPath = Get-AuraPgPassPath -Profile production
    if (-not (Test-Path -LiteralPath $pgPassPath -PathType Leaf)) {
        throw 'AURA_PRODUCTION_PGPASS_MISSING'
    }
    Assert-AuraOperatorSecretAcl -Path $pgPassPath

    $previous = Import-AuraConfiguration -Profile production
    $env:AURA_DISABLE_DOTENV = '1'
    Assert-AuraProductionConfiguration
    if (-not (Test-AuraPostgreSQLServiceRunning)) {
        throw 'AURA_POSTGRESQL_SERVICE_NOT_RUNNING'
    }
    if (-not (Test-AuraPostgreSQLLoopbackListener)) {
        throw 'AURA_POSTGRESQL_LISTENER_INVALID'
    }
    if (-not (Test-AuraProductionDatabaseReadiness)) {
        throw 'AURA_PRODUCTION_DATABASE_NOT_READY'
    }

    $python = Get-AuraPythonPath
    $arguments = @(
        '-m',
        'app.jobs.demo_cleanup',
        '--once',
        '--batch-size',
        '100'
    )
    if ($Mode -eq 'DryRun') { $arguments += '--dry-run' }
    $output = @(& $python @arguments)
    $exitCode = $LASTEXITCODE
    $rendered = $output -join "`n"
    Write-Output $rendered
    try {
        $payload = $rendered | ConvertFrom-Json -ErrorAction Stop
        if ($null -ne $payload.eligible_sessions) {
            $eligibleSessions = [int]$payload.eligible_sessions
        }
        if ($null -ne $payload.successful_cleanup_count) {
            $successfulCleanupCount = [int]$payload.successful_cleanup_count
        }
        if ($null -ne $payload.attempted_sessions) {
            $attemptedSessions = [int]$payload.attempted_sessions
        }
        if ($null -ne $payload.failed_cleanup_count) {
            $failedCleanupCount = [int]$payload.failed_cleanup_count
        }
    } catch {
        throw 'AURA_CLEANUP_OUTPUT_INVALID'
    }
    if ($exitCode -ne 0) {
        if ($payload.code -eq 'DEMO_CLEANUP_PARTIAL_FAILURE') {
            $operationResult = 'partial_failure'
            throw 'AURA_CLEANUP_PARTIAL_FAILURE'
        }
        throw 'AURA_CLEANUP_FAILED'
    }
    if ($payload.status -ne 'ok' -or $payload.mode -ne $operationMode) {
        throw 'AURA_CLEANUP_OUTPUT_INVALID'
    }
    $operationResult = 'success'
} finally {
    $startedAt.Stop()
    if ($null -ne $previous) {
        Restore-AuraProcessEnvironment -Previous $previous
    }
    [Environment]::SetEnvironmentVariable(
        'AURA_DISABLE_DOTENV',
        $dotenvPrevious,
        'Process'
    )
    if ($locationPushed) { Pop-Location }
    Write-AuraCleanupOperationLog -Profile production `
        -Mode $operationMode -EligibleSessions $eligibleSessions `
        -AttemptedSessions $attemptedSessions `
        -SuccessfulCleanupCount $successfulCleanupCount `
        -FailedCleanupCount $failedCleanupCount -Result $operationResult `
        -ElapsedMs ([Math]::Min([int]$startedAt.ElapsedMilliseconds, 3600000))
}

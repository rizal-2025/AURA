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
$finalExitCode = 1
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
    if ($exitCode -notin @(0, 1, 2)) {
        throw 'AURA_CLEANUP_OUTPUT_INVALID'
    }
    if ($exitCode -eq 0) {
        if ($payload.status -ne 'ok' -or $payload.mode -ne $operationMode) {
            throw 'AURA_CLEANUP_OUTPUT_INVALID'
        }
        $operationResult = 'success'
    } elseif ($exitCode -eq 2) {
        if (
            $payload.status -cne 'error' `
            -or $payload.mode -cne $operationMode `
            -or $payload.code -cne 'DEMO_CLEANUP_PARTIAL_FAILURE'
        ) {
            throw 'AURA_CLEANUP_OUTPUT_INVALID'
        }
        $operationResult = 'partial_failure'
    } else {
        if ($payload.status -cne 'error' -or $payload.mode -cne $operationMode) {
            throw 'AURA_CLEANUP_OUTPUT_INVALID'
        }
        $operationResult = 'failure'
    }
    $finalExitCode = $exitCode
} catch {
    Write-Error 'AURA_CLEANUP_WRAPPER_FAILED' -ErrorAction Continue
    $finalExitCode = 1
} finally {
    $startedAt.Stop()
    try {
        if ($null -ne $previous) {
            Restore-AuraProcessEnvironment -Previous $previous
        }
    } catch { if ($finalExitCode -eq 0) { $finalExitCode = 1 } }
    try {
        [Environment]::SetEnvironmentVariable(
            'AURA_DISABLE_DOTENV',
            $dotenvPrevious,
            'Process'
        )
    } catch { if ($finalExitCode -eq 0) { $finalExitCode = 1 } }
    try {
        if ($locationPushed) { Pop-Location }
    } catch { if ($finalExitCode -eq 0) { $finalExitCode = 1 } }
    try {
        Write-AuraCleanupOperationLog -Profile production `
            -Mode $operationMode -EligibleSessions $eligibleSessions `
            -AttemptedSessions $attemptedSessions `
            -SuccessfulCleanupCount $successfulCleanupCount `
            -FailedCleanupCount $failedCleanupCount -Result $operationResult `
            -ElapsedMs ([Math]::Min([int]$startedAt.ElapsedMilliseconds, 3600000))
    } catch {
        Write-Error 'AURA_CLEANUP_OPERATION_LOG_FAILED' -ErrorAction Continue
        if ($finalExitCode -eq 0) { $finalExitCode = 1 }
    }
}
exit $finalExitCode

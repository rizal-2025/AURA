[CmdletBinding()]
param(
    [string]$Profile = 'production',
    [Parameter(Mandatory)]
    [ValidateSet('ACTIVATE_AURA_DEMO_CLEANUP')]
    [string]$Confirmation
)

. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')
Assert-AuraProductionProfile -Profile $Profile
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principalCheck = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principalCheck.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'AURA_ADMIN_REQUIRED'
}

$repositoryRoot = Assert-AuraRepositoryLayout
$cleanupScript = Join-Path $PSScriptRoot 'Run-DemoCleanup.ps1'
$powerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
$python = Get-AuraPythonPath
$configPath = Get-AuraSecretPath -Profile production
$pgPassPath = Get-AuraPgPassPath -Profile production
Initialize-AuraDataDirectories
foreach ($path in @(
    $repositoryRoot, $cleanupScript, $powerShell, $python, $configPath, $pgPassPath
)) {
    if (-not (Test-Path -LiteralPath $path)) { throw 'AURA_CLEANUP_PREREQUISITE_MISSING' }
    Assert-AuraSystemReadAccess -Path $path
}
foreach ($path in @($script:AuraLogRoot, $script:AuraRunRoot)) {
    Assert-AuraSystemReadAccess -Path $path -RequireModify
}
Assert-AuraOperatorSecretAcl -Path $configPath
Assert-AuraOperatorSecretAcl -Path $pgPassPath

$previous = Import-AuraConfiguration -Profile production
try {
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
} finally {
    Restore-AuraProcessEnvironment -Previous $previous
}

$result = Enable-AuraCleanupTaskActivation -PowerShellPath $powerShell `
    -CleanupScript $cleanupScript -RepositoryRoot $repositoryRoot
Write-Output $result

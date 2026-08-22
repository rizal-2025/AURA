[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('UPGRADE_AURA_DEMO_CLEANUP_TASK')]
    [string]$Confirmation
)

. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')

$repositoryRoot = Assert-AuraRepositoryLayout
$cleanupScript = Join-Path $PSScriptRoot 'Run-DemoCleanup.ps1'
$powerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
$result = Upgrade-AuraCleanupTaskVersioned -PowerShellPath $powerShell `
    -CleanupScript $cleanupScript -RepositoryRoot $repositoryRoot
Write-Output "AURA_CLEANUP_TASK_UPGRADE_RESULT result=$result"

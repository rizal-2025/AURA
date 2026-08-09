[CmdletBinding()]
param(
    [string]$Profile = 'production',
    [Parameter(Mandatory)]
    [ValidateSet('DEACTIVATE_AURA_DEMO_CLEANUP')]
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
$result = Disable-AuraCleanupTaskActivation -PowerShellPath $powerShell `
    -CleanupScript $cleanupScript -RepositoryRoot $repositoryRoot
Write-Output $result

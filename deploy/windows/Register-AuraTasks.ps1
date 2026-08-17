[CmdletBinding()]
param()

. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principalCheck = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principalCheck.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'AURA_ADMIN_REQUIRED'
}
$userId = $identity.Name
Initialize-AuraDataDirectories
$icacls = (Get-Command icacls.exe -ErrorAction Stop).Source
foreach ($path in @($script:AuraLogRoot, $script:AuraBackupRoot, $script:AuraRunRoot)) {
    & $icacls $path '/inheritance:r' '/grant:r' 'SYSTEM:(OI)(CI)F' 'Administrators:(OI)(CI)F' "${userId}:(OI)(CI)M" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'AURA_DATA_ACL_FAILED' }
}
$powerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
$backupPrincipal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$maintenanceSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -StartWhenAvailable

$cleanupScript = Join-Path $PSScriptRoot 'Run-DemoCleanup.ps1'
$backupScript = Join-Path $PSScriptRoot 'Backup-DemoDatabase.ps1'
$repositoryRoot = Assert-AuraRepositoryLayout
$backupAction = New-ScheduledTaskAction -Execute $powerShell -Argument "-NoProfile -NonInteractive -File `"$backupScript`" -Profile production"
$backupTrigger = New-ScheduledTaskTrigger -Daily -At '02:41'

Register-ScheduledTask -TaskName 'AURA Demo Backup' -Action $backupAction -Trigger $backupTrigger -Principal $backupPrincipal -Settings $maintenanceSettings -Description 'Daily local production demo backup.' -Force | Out-Null
$cleanupResult = Register-AuraCleanupTaskStaged -PowerShellPath $powerShell `
    -CleanupScript $cleanupScript -RepositoryRoot $repositoryRoot
Write-Output "AURA_TASKS_REGISTERED cleanup=$cleanupResult"

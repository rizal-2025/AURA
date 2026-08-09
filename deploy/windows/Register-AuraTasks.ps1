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
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$powerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
$maintenanceSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -StartWhenAvailable

$cleanupScript = Join-Path $PSScriptRoot 'Run-DemoCleanup.ps1'
$backupScript = Join-Path $PSScriptRoot 'Backup-DemoDatabase.ps1'
$repositoryRoot = Assert-AuraRepositoryLayout
$cleanupAction = New-ScheduledTaskAction -Execute $powerShell `
    -Argument "-NoProfile -NonInteractive -File `"$cleanupScript`" -Profile production -Mode Execute -Confirmation RUN_AURA_DEMO_CLEANUP" `
    -WorkingDirectory $repositoryRoot
$backupAction = New-ScheduledTaskAction -Execute $powerShell -Argument "-NoProfile -NonInteractive -File `"$backupScript`" -Profile production"
$cleanupTrigger = New-ScheduledTaskTrigger -Daily -At '00:17'
$cleanupTrigger.Repetition.Interval = 'PT1H'
$cleanupTrigger.Repetition.Duration = 'P1D'
$backupTrigger = New-ScheduledTaskTrigger -Daily -At '02:41'

Register-ScheduledTask -TaskName 'AURA Demo Cleanup' -Action $cleanupAction -Trigger $cleanupTrigger -Principal $principal -Settings $maintenanceSettings -Description 'Hourly bounded demo cleanup at minute 17.' -Force | Out-Null
Register-ScheduledTask -TaskName 'AURA Demo Backup' -Action $backupAction -Trigger $backupTrigger -Principal $principal -Settings $maintenanceSettings -Description 'Daily local production demo backup.' -Force | Out-Null
Write-Output 'AURA_TASKS_REGISTERED'

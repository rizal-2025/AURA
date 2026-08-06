[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('UNREGISTER_AURA_TASKS')]
    [string]$Confirmation
)

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principalCheck = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principalCheck.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'AURA_ADMIN_REQUIRED'
}
foreach ($name in @('AURA API Production', 'AURA Demo Cleanup', 'AURA Demo Backup')) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
    }
}
Write-Output 'AURA_TASKS_UNREGISTERED'

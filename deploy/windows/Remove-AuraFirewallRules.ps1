[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('REMOVE_AURA_FIREWALL_RULES')]
    [string]$Confirmation
)

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principalCheck = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principalCheck.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'AURA_ADMIN_REQUIRED'
}
$rules = Get-NetFirewallRule -Group 'AURA Self-Host' -ErrorAction SilentlyContinue
if ($rules) { $rules | Remove-NetFirewallRule }
Write-Output 'AURA_FIREWALL_RULES_REMOVED'

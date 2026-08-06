[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('INSTALL_AURA_FIREWALL_RULES')]
    [string]$Confirmation
)

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principalCheck = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principalCheck.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'AURA_ADMIN_REQUIRED'
}
$group = 'AURA Self-Host'
if (Get-NetFirewallRule -Group $group -ErrorAction SilentlyContinue) {
    throw 'AURA_FIREWALL_RULES_ALREADY_EXIST'
}
New-NetFirewallRule -DisplayName 'AURA block direct API 8000' -Group $group -Direction Inbound -Action Block -Protocol TCP -LocalPort 8000 -Profile Any | Out-Null
New-NetFirewallRule -DisplayName 'AURA block direct API 8001' -Group $group -Direction Inbound -Action Block -Protocol TCP -LocalPort 8001 -Profile Any | Out-Null
New-NetFirewallRule -DisplayName 'AURA block direct PostgreSQL 5432' -Group $group -Direction Inbound -Action Block -Protocol TCP -LocalPort 5432 -Profile Any | Out-Null
Write-Output 'AURA_FIREWALL_RULES_INSTALLED'

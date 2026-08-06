[CmdletBinding()]
param(
    [ValidateSet('staging', 'production')]
    [string]$Profile = 'production'
)

$funnelStop = Join-Path $PSScriptRoot 'Stop-TailscaleFunnel.ps1'
$auraStop = Join-Path $PSScriptRoot 'Stop-Aura.ps1'
try {
    & $funnelStop -Profile $Profile | Out-Null
} finally {
    & $auraStop -Profile $Profile | Out-Null
}
Write-Output 'AURA_PUBLIC_DEMO_STOP_OK'

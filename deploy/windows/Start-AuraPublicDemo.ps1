[CmdletBinding()]
param(
    [ValidateSet('staging', 'production')]
    [string]$Profile = 'production'
)

. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')
Assert-AuraProfile -Profile $Profile
Initialize-AuraDataDirectories
$previous = Import-AuraConfiguration -Profile $Profile
try {
    $expectedDatabase = if ($Profile -eq 'production') { 'aura_demo_public' } else { 'aura_demo_staging' }
    if ($env:AURA_DB_HOST -ne '127.0.0.1' -or $env:AURA_DB_PORT -ne '5432' -or $env:AURA_DB_NAME -ne $expectedDatabase) {
        throw 'AURA_DATABASE_PROFILE_INVALID'
    }
    $python = Get-AuraPythonPath
    & $python -m app.jobs.public_demo_readiness | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'AURA_DATABASE_NOT_READY' }
} finally {
    Restore-AuraProcessEnvironment -Previous $previous
}

$auraStarted = $false
$funnelStarted = $false
try {
    & (Join-Path $PSScriptRoot 'Start-Aura.ps1') -Profile $Profile | Out-Null
    $auraStarted = $true
    & (Join-Path $PSScriptRoot 'Start-TailscaleFunnel.ps1') -Profile $Profile | Out-Null
    $funnelStarted = $true
    & (Join-Path $PSScriptRoot 'Test-PublicDemoReadiness.ps1') -Profile $Profile -AuthenticatedSmoke | Out-Null
} catch {
    if ($funnelStarted) { & (Join-Path $PSScriptRoot 'Stop-TailscaleFunnel.ps1') -Profile $Profile | Out-Null }
    if ($auraStarted) { & (Join-Path $PSScriptRoot 'Stop-Aura.ps1') -Profile $Profile | Out-Null }
    throw 'AURA_PUBLIC_DEMO_START_FAILED'
}
Write-Output 'AURA_PUBLIC_DEMO_START_OK'

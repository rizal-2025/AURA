[CmdletBinding()]
param(
    [ValidateSet('staging', 'production')]
    [string]$Profile = 'production'
)

. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')
try {
    $status = Get-AuraTailscaleStatus
    if (-not (Test-AuraFunnelStatusObject -Status $status -Profile $Profile)) {
        throw 'inactive'
    }
    $baseUri = Get-AuraFunnelBaseUri -Profile $Profile
    $response = Invoke-WebRequest -Uri "$baseUri/health" -Method Get -TimeoutSec 15 -UseBasicParsing -MaximumRedirection 0
    if ($response.StatusCode -ne 200 -or $response.Content -ne '{"status":"healthy"}') { throw 'invalid' }
} catch {
    throw 'AURA_FUNNEL_READINESS_FAILED'
}
Write-Output 'AURA_FUNNEL_READY'

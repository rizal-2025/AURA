[CmdletBinding()]
param(
    [ValidateSet('staging', 'production')]
    [string]$Profile = 'production'
)

. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')
$port = Get-AuraProfilePort -Profile $Profile
try {
    $response = Invoke-WebRequest -Uri "http://127.0.0.1:$port/ready" -Method Get -TimeoutSec 5 -UseBasicParsing
    if ($response.StatusCode -ne 200 -or $response.Content -ne '{"status":"ready"}') {
        throw 'AURA_READINESS_RESPONSE_INVALID'
    }
} catch {
    throw 'AURA_READINESS_FAILED'
}
Write-Output 'AURA_READY'

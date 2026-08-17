[CmdletBinding()]
param(
    [ValidateSet('staging', 'production')]
    [string]$Profile = 'production'
)

. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')
$port = Get-AuraProfilePort -Profile $Profile
$previous = Import-AuraConfiguration -Profile $Profile
try {
    $python = Get-AuraPythonPath
    & $python -m app.jobs.public_demo_readiness | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'AURA_DATABASE_NOT_READY' }
    $response = Invoke-WebRequest -Uri "http://127.0.0.1:$port/health" -Method Get -TimeoutSec 5 -UseBasicParsing
    if ($response.StatusCode -ne 200 -or $response.Content -ne '{"status":"healthy"}') {
        throw 'AURA_READINESS_RESPONSE_INVALID'
    }
    if ($Profile -eq 'production') {
        $cleanupHealth = Get-AuraCleanupHealth -Profile production
        if (-not $cleanupHealth.ReadyCompatible) {
            throw $cleanupHealth.Status
        }
    }
} catch {
    throw 'AURA_READINESS_FAILED'
} finally {
    Restore-AuraProcessEnvironment -Previous $previous
}
Write-Output 'AURA_READY'

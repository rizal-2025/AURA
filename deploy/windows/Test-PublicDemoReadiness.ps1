[CmdletBinding()]
param(
    [ValidateSet('staging', 'production')]
    [string]$Profile = 'production',
    [switch]$AuthenticatedSmoke
)

. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')
$port = Get-AuraProfilePort -Profile $Profile
$localBase = "http://127.0.0.1:$port"

try {
    $health = Invoke-WebRequest -Uri "$localBase/health" -Method Get -TimeoutSec 5 -UseBasicParsing -MaximumRedirection 0
    if ($health.StatusCode -ne 200 -or $health.Content -ne '{"status":"healthy"}') { throw 'invalid' }
    foreach ($path in @('/', '/ready', '/docs', '/redoc', '/openapi.json', '/chat', '/reservations', '/telegram', '/admin', '/internal/demo/sessions/')) {
        try {
            $unexpected = Invoke-WebRequest -Uri "$localBase$path" -Method Get -TimeoutSec 3 -UseBasicParsing -MaximumRedirection 0
            throw 'unexpected'
        } catch {
            if ($_.Exception.Response.StatusCode.value__ -ne 404) { throw }
        }
    }
} catch {
    throw 'AURA_GATEWAY_ROUTE_INVENTORY_FAILED'
}

& (Join-Path $PSScriptRoot 'Test-AuraReadiness.ps1') -Profile $Profile | Out-Null
& (Join-Path $PSScriptRoot 'Test-TailscaleFunnel.ps1') -Profile $Profile | Out-Null

if ($AuthenticatedSmoke) {
    $previous = Import-AuraConfiguration -Profile $Profile
    try {
        $baseUri = Get-AuraFunnelBaseUri -Profile $Profile
        $headers = @{
            'X-BFF-Service-Token' = $env:DEMO_BFF_SERVICE_TOKEN
            'X-Demo-Client-Subject' = ('0' * 64)
        }
        $response = Invoke-WebRequest -Uri "$baseUri/internal/demo/sessions" -Method Post -Headers $headers -ContentType 'application/json' -Body ([byte[]]@()) -TimeoutSec 15 -UseBasicParsing -MaximumRedirection 0
        if ($response.StatusCode -ne 201 -or $response.Headers['Content-Type'] -notmatch '^application/json') { throw 'invalid' }
        $document = $response.Content | ConvertFrom-Json
        if ([string]::IsNullOrWhiteSpace([string]$document.sessionToken)) { throw 'invalid' }
        $document = $null
        $response = $null
    } catch {
        throw 'AURA_AUTHENTICATED_SMOKE_FAILED'
    } finally {
        Restore-AuraProcessEnvironment -Previous $previous
    }
}
Write-Output 'AURA_PUBLIC_DEMO_READY'

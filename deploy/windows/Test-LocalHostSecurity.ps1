[CmdletBinding()]
param()

$protectedPorts = @(8000, 8001, 5432)
foreach ($port in $protectedPorts) {
    $listeners = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    foreach ($listener in $listeners) {
        if ($listener.LocalAddress -notin @('127.0.0.1', '::1')) {
            throw 'AURA_NON_LOOPBACK_LISTENER_FOUND'
        }
    }
}
$firewallRules = Get-NetFirewallRule -Group 'AURA Self-Host' -ErrorAction SilentlyContinue
if (($firewallRules | Measure-Object).Count -lt 4) { throw 'AURA_FIREWALL_RULES_MISSING' }
foreach ($port in @(8000, 8001)) {
    $listener = Get-NetTCPConnection -State Listen -LocalAddress 127.0.0.1 -LocalPort $port -ErrorAction SilentlyContinue
    if ($listener) {
        try {
            $response = Invoke-WebRequest -Uri "http://127.0.0.1:$port/health" -Method Get -TimeoutSec 3 -UseBasicParsing
            if ($response.StatusCode -ne 200 -or $response.Content -ne '{"status":"healthy"}') { throw 'invalid' }
        } catch { throw 'AURA_LOOPBACK_HEALTH_FAILED' }
    }
}
Write-Output 'AURA_LOCALHOST_SECURITY_OK'

[CmdletBinding()]
param(
    [ValidateSet('staging', 'production')]
    [string]$Profile = 'production'
)

. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')
$tailscale = Get-TailscalePath
$publicPort = Get-AuraFunnelPort -Profile $Profile
$pidPath = Join-Path $script:AuraRunRoot "tailscale-funnel-$Profile.pid"

& $tailscale funnel "--https=$publicPort" off *> $null
if ($LASTEXITCODE -ne 0) { throw 'AURA_FUNNEL_STOP_FAILED' }

if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
    $rawPid = (Get-Content -Raw -LiteralPath $pidPath).Trim()
    if ($rawPid -notmatch '^[1-9][0-9]*$') { throw 'AURA_FUNNEL_PID_FILE_INVALID' }
    $pidValue = [int]$rawPid
    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $pidValue" -ErrorAction SilentlyContinue
    if ($null -ne $processInfo) {
        if ($processInfo.Name -ne 'tailscale.exe' -or $processInfo.CommandLine -notlike "*funnel*--https=$publicPort*") {
            throw 'AURA_FUNNEL_PID_OWNERSHIP_INVALID'
        }
        Stop-Process -Id $pidValue -ErrorAction SilentlyContinue
        Wait-Process -Id $pidValue -Timeout 15 -ErrorAction SilentlyContinue
        if (Get-Process -Id $pidValue -ErrorAction SilentlyContinue) { throw 'AURA_FUNNEL_STOP_TIMEOUT' }
    }
    Remove-Item -LiteralPath $pidPath -Force
}

try {
    $status = Get-AuraTailscaleStatus
    if (Test-AuraFunnelStatusObject -Status $status -Profile $Profile) { throw 'AURA_FUNNEL_STILL_ACTIVE' }
} catch {
    if ($_.Exception.Message -eq 'AURA_FUNNEL_STILL_ACTIVE') { throw }
}
Write-Output 'AURA_FUNNEL_STOP_OK'

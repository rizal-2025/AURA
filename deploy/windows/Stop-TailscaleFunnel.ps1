[CmdletBinding()]
param(
    [ValidateSet('staging', 'production')]
    [string]$Profile = 'production'
)

. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')
$tailscale = Get-TailscalePath
$publicPort = Get-AuraFunnelPort -Profile $Profile
$pidPath = Join-Path $script:AuraRunRoot "tailscale-funnel-$Profile.pid"
$otherProfile = if ($Profile -eq 'production') { 'staging' } else { 'production' }

$status = Get-AuraTailscaleStatus
$profileActive = Test-AuraFunnelStatusObject -Status $status -Profile $Profile
if (Test-AuraFunnelStatusObject -Status $status -Profile $otherProfile) {
    throw 'AURA_FUNNEL_OTHER_PROFILE_ACTIVE'
}
if ($profileActive) {
    & $tailscale funnel reset *> $null
    if ($LASTEXITCODE -ne 0) { throw 'AURA_FUNNEL_STOP_FAILED' }
}

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

$status = Get-AuraTailscaleStatus
if (Test-AuraFunnelStatusObject -Status $status -Profile $Profile) {
    throw 'AURA_FUNNEL_STILL_ACTIVE'
}
Write-Output 'AURA_FUNNEL_STOP_OK'

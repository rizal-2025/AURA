[CmdletBinding()]
param(
    [ValidateSet('staging', 'production')]
    [string]$Profile = 'production'
)

. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')
Initialize-AuraDataDirectories
$tailscale = Get-TailscalePath
$publicPort = Get-AuraFunnelPort -Profile $Profile
$target = Get-AuraFunnelTarget -Profile $Profile
$localPort = Get-AuraProfilePort -Profile $Profile
$pidPath = Join-Path $script:AuraRunRoot "tailscale-funnel-$Profile.pid"
$otherProfile = if ($Profile -eq 'production') { 'staging' } else { 'production' }

if (-not (Get-NetTCPConnection -State Listen -LocalAddress 127.0.0.1 -LocalPort $localPort -ErrorAction SilentlyContinue)) {
    throw 'AURA_GATEWAY_NOT_RUNNING'
}
if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
    $rawPid = (Get-Content -Raw -LiteralPath $pidPath).Trim()
    if ($rawPid -match '^[1-9][0-9]*$' -and (Get-Process -Id ([int]$rawPid) -ErrorAction SilentlyContinue)) {
        throw 'AURA_FUNNEL_ALREADY_RUNNING'
    }
    Remove-Item -LiteralPath $pidPath -Force
}
$existingStatus = Get-AuraTailscaleStatus
if (Test-AuraFunnelStatusObject -Status $existingStatus -Profile $Profile) {
    throw 'AURA_FUNNEL_ALREADY_ACTIVE'
}
if (Test-AuraFunnelStatusObject -Status $existingStatus -Profile $otherProfile) {
    throw 'AURA_FUNNEL_OTHER_PROFILE_ACTIVE'
}

# No --bg: the detached foreground CLI session does not persist Funnel across
# device or daemon restarts. Start-AuraPublicDemo.ps1 is the only orchestrator.
$process = Start-Process -FilePath $tailscale -ArgumentList @('funnel', "--https=$publicPort", $target) -WindowStyle Hidden -PassThru
Set-Content -LiteralPath $pidPath -Value ([string]$process.Id) -Encoding ascii -NoNewline

$ready = $false
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    if ($process.HasExited) { break }
    try {
        $status = Get-AuraTailscaleStatus
        if (Test-AuraFunnelStatusObject -Status $status -Profile $Profile) { $ready = $true; break }
    } catch { }
    Start-Sleep -Seconds 1
}
if (-not $ready) {
    & $tailscale funnel reset *> $null
    if ($LASTEXITCODE -ne 0) { throw 'AURA_FUNNEL_RESET_FAILED' }
    if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force }
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
    throw 'AURA_FUNNEL_START_FAILED'
}
Write-Output 'AURA_FUNNEL_START_OK'

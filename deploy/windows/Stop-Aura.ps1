[CmdletBinding()]
param(
    [ValidateSet('staging', 'production')]
    [string]$Profile = 'production'
)

. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')
$pidPath = Join-Path $script:AuraRunRoot "aura-$Profile.pid"
if (-not (Test-Path -LiteralPath $pidPath -PathType Leaf)) {
    Write-Output 'AURA_NOT_RUNNING'
    exit 0
}
$rawPid = (Get-Content -Raw -LiteralPath $pidPath).Trim()
if ($rawPid -notmatch '^[1-9][0-9]*$') { throw 'AURA_PID_FILE_INVALID' }
$pidValue = [int]$rawPid
$processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $pidValue" -ErrorAction SilentlyContinue
if ($null -eq $processInfo) {
    Remove-Item -LiteralPath $pidPath -Force
    Write-Output 'AURA_NOT_RUNNING'
    exit 0
}
$marker = "app.self_host --profile $Profile"
if ($processInfo.Name -notmatch '^python(?:\.exe)?$' -or $processInfo.CommandLine -notlike "*$marker*") {
    throw 'AURA_PID_OWNERSHIP_INVALID'
}
Stop-Process -Id $pidValue
Wait-Process -Id $pidValue -Timeout 30 -ErrorAction SilentlyContinue
if (Get-Process -Id $pidValue -ErrorAction SilentlyContinue) { throw 'AURA_STOP_TIMEOUT' }
Remove-Item -LiteralPath $pidPath -Force
Write-Output 'AURA_STOP_OK'

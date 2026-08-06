[CmdletBinding()]
param(
    [ValidateSet('staging', 'production')]
    [string]$Profile = 'production',
    [switch]$Foreground
)

. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')
Assert-AuraProfile -Profile $Profile
Initialize-AuraDataDirectories
$port = Get-AuraProfilePort -Profile $Profile
$pidPath = Join-Path $script:AuraRunRoot "aura-$Profile.pid"

if (Test-Path -LiteralPath $pidPath) {
    $existingPid = (Get-Content -Raw -LiteralPath $pidPath).Trim()
    if ($existingPid -match '^[1-9][0-9]*$' -and (Get-Process -Id ([int]$existingPid) -ErrorAction SilentlyContinue)) {
        throw 'AURA_ALREADY_RUNNING'
    }
    Remove-Item -LiteralPath $pidPath -Force
}
if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
    throw 'AURA_PORT_IN_USE'
}

$previous = Import-AuraConfiguration -Profile $Profile
$retention = 14
$internalPrevious = @{
    AURA_DISABLE_DOTENV = [Environment]::GetEnvironmentVariable('AURA_DISABLE_DOTENV', 'Process')
    AURA_BIND_HOST = [Environment]::GetEnvironmentVariable('AURA_BIND_HOST', 'Process')
    AURA_PORT = [Environment]::GetEnvironmentVariable('AURA_PORT', 'Process')
}
try {
    $expectedDatabase = if ($Profile -eq 'production') { 'aura_demo_public' } else { 'aura_demo_staging' }
    if ($env:AURA_DB_HOST -ne '127.0.0.1' -or $env:AURA_DB_PORT -ne '5432' -or $env:AURA_DB_NAME -ne $expectedDatabase) {
        throw 'AURA_DATABASE_PROFILE_INVALID'
    }
    $env:AURA_DISABLE_DOTENV = '1'
    $env:AURA_BIND_HOST = '127.0.0.1'
    $env:AURA_PORT = [string]$port
    if ($env:AURA_LOG_RETENTION_DAYS -match '^[1-9][0-9]{0,2}$') {
        $retention = [int]$env:AURA_LOG_RETENTION_DAYS
    }
    $python = Get-AuraPythonPath
    $timestamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
    $stdout = Join-Path $script:AuraLogRoot "aura-$Profile-$timestamp.log"
    $stderr = Join-Path $script:AuraLogRoot "aura-$Profile-$timestamp.err.log"
    $process = Start-Process -FilePath $python -ArgumentList @('-m', 'app.self_host', '--profile', $Profile) -WorkingDirectory (Get-AuraRepositoryRoot) -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    Set-Content -LiteralPath $pidPath -Value ([string]$process.Id) -Encoding ascii -NoNewline
} finally {
    Restore-AuraProcessEnvironment -Previous $previous
    Restore-AuraProcessEnvironment -Previous $internalPrevious
}

$ready = $false
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    if ($process.HasExited) { break }
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$port/health" -Method Get -TimeoutSec 2 -UseBasicParsing
        if ($response.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
    Start-Sleep -Seconds 1
}
if (-not $ready) {
    if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force }
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
    throw 'AURA_READINESS_FAILED'
}
Remove-AuraExpiredFiles -Root $script:AuraLogRoot -Filter 'aura-*.log' -RetentionDays $retention
Write-Output 'AURA_START_OK'
if ($Foreground) {
    Wait-Process -Id $process.Id
    $process.Refresh()
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
    if ($process.ExitCode -ne 0) { throw 'AURA_PROCESS_FAILED' }
}

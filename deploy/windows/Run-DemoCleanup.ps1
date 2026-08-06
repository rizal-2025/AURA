[CmdletBinding()]
param(
    [ValidateSet('staging', 'production')]
    [string]$Profile = 'production'
)

. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')
$previous = Import-AuraConfiguration -Profile $Profile
$dotenvPrevious = [Environment]::GetEnvironmentVariable('AURA_DISABLE_DOTENV', 'Process')
try {
    $env:AURA_DISABLE_DOTENV = '1'
    $python = Get-AuraPythonPath
    & $python -m app.jobs.demo_cleanup --once --batch-size 100
    if ($LASTEXITCODE -ne 0) { throw 'AURA_CLEANUP_FAILED' }
} finally {
    Restore-AuraProcessEnvironment -Previous $previous
    [Environment]::SetEnvironmentVariable('AURA_DISABLE_DOTENV', $dotenvPrevious, 'Process')
}

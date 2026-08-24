[CmdletBinding()]
param(
    [string]$Profile,
    [string]$Mode,
    [string]$Confirmation
)

$ErrorActionPreference = 'Stop'
$common = Join-Path (Get-Location) 'deploy\windows\AuraWindows.Common.ps1'
. $common

$validPath = Join-Path $PSScriptRoot 'valid-secret.txt'
$invalidPath = Join-Path $PSScriptRoot 'invalid-secret.txt'
$resultPath = Join-Path $PSScriptRoot 'system-secret-acl-probe-result.txt'

try {
    Assert-AuraOperatorSecretAcl -Path $validPath
    try {
        Assert-AuraOperatorSecretAcl -Path $invalidPath
        throw 'AURA_SYSTEM_SECRET_ACL_NEGATIVE_ACCEPTED'
    } catch {
        if ($_.Exception.Message -cne 'AURA_SECRET_ACL_TOO_BROAD') { throw }
    }
    [IO.File]::WriteAllText(
        $resultPath,
        'AURA_SYSTEM_SECRET_ACL_PROBE_OK',
        [Text.Encoding]::ASCII
    )
    exit 0
} catch {
    [IO.File]::WriteAllText(
        $resultPath,
        ('AURA_SYSTEM_SECRET_ACL_PROBE_ERROR=' + $_.Exception.Message),
        [Text.Encoding]::ASCII
    )
    exit 1
}

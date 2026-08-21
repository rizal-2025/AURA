[CmdletBinding()]
param(
    [string]$Profile,
    [string]$Mode,
    [string]$Confirmation
)

$resultPath = Join-Path $PSScriptRoot 'execution-policy-probe-result.txt'
[IO.File]::WriteAllText(
    $resultPath,
    'AURA_EXECUTION_POLICY_PROBE_OK',
    [Text.Encoding]::ASCII
)
exit 0

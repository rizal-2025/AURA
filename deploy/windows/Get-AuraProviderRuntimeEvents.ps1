[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')]
    [string]$RequestId
)

. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')

Get-AuraProviderRuntimeEvents -RequestId $RequestId

[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')]
    [string]$RequestId,
    [Parameter(Mandatory)]
    [ValidatePattern('^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$')]
    [string]$NotBeforeUtc,
    [ValidateRange(1, 64)]
    [int]$MaxRecords = 32
)

. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')

Get-AuraProviderRuntimeEvents -RequestId $RequestId `
    -NotBeforeUtc $NotBeforeUtc -MaxRecords $MaxRecords

[CmdletBinding()]
param(
    [ValidateSet('staging', 'production')]
    [string]$Profile = 'production',
    [switch]$Foreground
)

. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')
Assert-AuraProfile -Profile $Profile
Initialize-AuraDataDirectories
$providerRuntimeEventLog = Initialize-AuraProviderRuntimeEventSink
$port = Get-AuraProfilePort -Profile $Profile
$ownership = Get-AuraOwnedProcessState -Kind aura -Profile $Profile `
    -RepairStaleMetadata

if ($ownership.State -in @('ambiguous', 'uncertain')) {
    throw 'AURA_PROCESS_OWNERSHIP_UNCERTAIN'
}
if ($ownership.State -eq 'owned') {
    $gatewayProcess = Get-AuraGatewayListenerProcessInfo `
        -OwnershipProcessInfo $ownership.ProcessInfo -Profile $Profile
    if (
        $null -ne $gatewayProcess `
        -and (Test-AuraLocalHealth -Profile $Profile)
    ) {
        if (
            $ownership.Legacy `
            -or [int]$gatewayProcess.ProcessId -ne `
                [int]$ownership.ProcessInfo.ProcessId
        ) {
            Write-AuraOwnershipMetadata -Path $ownership.Path `
                -ProcessInfo $gatewayProcess
        }
        Write-Output 'AURA_ALREADY_READY'
        return
    }
    & (Join-Path $PSScriptRoot 'Stop-Aura.ps1') -Profile $Profile | Out-Null
}
if (-not (Test-AuraPortClosed -Port $port)) {
    throw 'AURA_PORT_OWNERSHIP_UNEXPECTED'
}

$previous = Import-AuraConfiguration -Profile $Profile
$retention = 14
$internalPrevious = @{
    AURA_DISABLE_DOTENV = [Environment]::GetEnvironmentVariable(
        'AURA_DISABLE_DOTENV', 'Process'
    )
    AURA_BIND_HOST = [Environment]::GetEnvironmentVariable('AURA_BIND_HOST', 'Process')
    AURA_PORT = [Environment]::GetEnvironmentVariable('AURA_PORT', 'Process')
    AURA_PROVIDER_RUNTIME_EVENT_LOG_PATH = `
        [Environment]::GetEnvironmentVariable(
            'AURA_PROVIDER_RUNTIME_EVENT_LOG_PATH', 'Process'
        )
}
$process = $null
$ownershipPath = Get-AuraOwnershipPath -Kind aura -Profile $Profile
try {
    $expectedDatabase = if ($Profile -eq 'production') {
        'aura_demo_public'
    } else { 'aura_demo_staging' }
    if (
        $env:AURA_DB_HOST -ne '127.0.0.1' `
        -or $env:AURA_DB_PORT -ne '5432' `
        -or $env:AURA_DB_NAME -ne $expectedDatabase
    ) { throw 'AURA_DATABASE_PROFILE_INVALID' }
    $env:AURA_DISABLE_DOTENV = '1'
    $env:AURA_BIND_HOST = '127.0.0.1'
    $env:AURA_PORT = [string]$port
    $env:AURA_PROVIDER_RUNTIME_EVENT_LOG_PATH = $providerRuntimeEventLog
    if ($env:AURA_LOG_RETENTION_DAYS -match '^[1-9][0-9]{0,2}$') {
        $retention = [int]$env:AURA_LOG_RETENTION_DAYS
    }
    $python = Get-AuraPythonPath
    $environmentVariables = @(
        [Environment]::GetEnvironmentVariables('Process').GetEnumerator() |
            ForEach-Object { '{0}={1}' -f $_.Key, $_.Value }
    )
    $startup = New-CimInstance -ClassName Win32_ProcessStartup `
        -Namespace root\cimv2 -Property @{
            ShowWindow = [uint16]0
            EnvironmentVariables = [string[]]$environmentVariables
        } -ClientOnly
    $commandLine = '"{0}" -m app.self_host --profile {1}' -f `
        $python, $Profile
    $created = Invoke-CimMethod -ClassName Win32_Process -MethodName Create `
        -Arguments @{
            CommandLine = $commandLine
            CurrentDirectory = (Get-AuraRepositoryRoot)
            ProcessStartupInformation = $startup
        }
    if ([int]$created.ReturnValue -ne 0 -or [int]$created.ProcessId -le 0) {
        throw 'AURA_PROCESS_CREATE_FAILED'
    }
    $processId = [int]$created.ProcessId
    $process = [Diagnostics.Process]::GetProcessById($processId)
    $processInfo = $null
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        $processInfo = Get-CimInstance Win32_Process `
            -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
        if ($null -ne $processInfo) { break }
        Start-Sleep -Milliseconds 100
    }
    if (
        $null -eq $processInfo `
        -or -not (Test-AuraExpectedProcessInfo -ProcessInfo $processInfo `
            -Kind aura -Profile $Profile)
    ) { throw 'AURA_PROCESS_START_OWNERSHIP_INVALID' }
} catch {
    $ownershipAmbiguous = $false
    if ($null -ne $process) {
        $startedInfo = Get-CimInstance Win32_Process `
            -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
        if (
            $null -ne $startedInfo `
            -and (Test-AuraExpectedProcessInfo -ProcessInfo $startedInfo `
                -Kind aura -Profile $Profile)
        ) {
            $startedGateway = Get-AuraGatewayListenerProcessInfo `
                -OwnershipProcessInfo $startedInfo -Profile $Profile
            if ($null -ne $startedGateway) {
                $verifiedGateway = Assert-AuraOwnedProcessStillMatches `
                    -OriginalProcessInfo $startedGateway -Kind aura `
                    -Profile $Profile
                if ($null -ne $verifiedGateway) {
                    Stop-Process -Id ([int]$verifiedGateway.ProcessId) -Force
                }
            }
            $verifiedParent = Assert-AuraOwnedProcessStillMatches `
                -OriginalProcessInfo $startedInfo -Kind aura -Profile $Profile
            if ($null -ne $verifiedParent) {
                Stop-Process -Id ([int]$verifiedParent.ProcessId) -Force
            }
        } elseif (-not $process.HasExited) {
            $ownershipAmbiguous = $true
        }
    }
    Remove-Item -LiteralPath $ownershipPath -Force -ErrorAction SilentlyContinue
    if ($ownershipAmbiguous) {
        throw 'HUMAN_GATE_PROCESS_OWNERSHIP_AMBIGUOUS'
    }
    throw 'AURA_PROCESS_START_FAILED'
} finally {
    Restore-AuraProcessEnvironment -Previous $previous
    Restore-AuraProcessEnvironment -Previous $internalPrevious
}

$ready = $false
$gatewayProcess = $null
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    if ($process.HasExited) { break }
    $gatewayProcess = Get-AuraGatewayListenerProcessInfo `
        -OwnershipProcessInfo $processInfo -Profile $Profile
    if (
        $null -ne $gatewayProcess `
        -and (Test-AuraLocalHealth -Profile $Profile)
    ) { $ready = $true; break }
    Start-Sleep -Seconds 1
}
if (-not $ready) {
    if ($null -ne $gatewayProcess) {
        $gateway = Assert-AuraOwnedProcessStillMatches `
            -OriginalProcessInfo $gatewayProcess -Kind aura -Profile $Profile
        if ($null -ne $gateway) {
            Stop-Process -Id ([int]$gateway.ProcessId) -Force
        }
    }
    $current = Get-CimInstance Win32_Process `
        -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
    if (
        $null -ne $current `
        -and (Test-AuraExpectedProcessInfo -ProcessInfo $current `
            -Kind aura -Profile $Profile)
    ) { Stop-Process -Id $process.Id -Force }
    Remove-Item -LiteralPath $ownershipPath -Force -ErrorAction SilentlyContinue
    throw 'AURA_READINESS_FAILED'
}
try {
    Write-AuraOwnershipMetadata -Path $ownershipPath -ProcessInfo $gatewayProcess
} catch {
    $verified = Assert-AuraOwnedProcessStillMatches `
        -OriginalProcessInfo $gatewayProcess -Kind aura -Profile $Profile
    if ($null -ne $verified) {
        Stop-Process -Id ([int]$verified.ProcessId) -Force
    }
    $launcher = Assert-AuraOwnedProcessStillMatches `
        -OriginalProcessInfo $processInfo -Kind aura -Profile $Profile
    if ($null -ne $launcher) {
        Stop-Process -Id ([int]$launcher.ProcessId) -Force
    }
    Remove-Item -LiteralPath $ownershipPath -Force -ErrorAction SilentlyContinue
    throw 'AURA_PROCESS_METADATA_WRITE_FAILED'
}
Remove-AuraExpiredFiles -Root $script:AuraLogRoot -Filter 'aura-*.log' `
    -RetentionDays $retention
Write-Output 'AURA_START_OK'
if ($Foreground) {
    Wait-Process -Id $processId
    $process.Refresh()
    Remove-Item -LiteralPath $ownershipPath -Force -ErrorAction SilentlyContinue
    if ($process.ExitCode -ne 0) { throw 'AURA_PROCESS_FAILED' }
}

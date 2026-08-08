Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:AuraDataRoot = 'C:\ProgramData\AURA'
$script:AuraSecretRoot = 'C:\ProgramData\AURA\secrets'
$script:AuraLogRoot = 'C:\ProgramData\AURA\logs'
$script:AuraBackupRoot = 'C:\ProgramData\AURA\backups'
$script:AuraRunRoot = 'C:\ProgramData\AURA\run'

function Get-AuraRepositoryRoot {
    $root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    return [System.IO.Path]::GetFullPath($root)
}

function Assert-AuraProfile {
    param([Parameter(Mandatory)][string]$Profile)
    if ($Profile -notin @('staging', 'production')) {
        throw 'AURA_PROFILE_INVALID'
    }
}

function Get-AuraProfilePort {
    param([Parameter(Mandatory)][string]$Profile)
    Assert-AuraProfile -Profile $Profile
    if ($Profile -eq 'production') { return 8000 }
    return 8001
}

function Get-AuraFunnelPort {
    param([Parameter(Mandatory)][string]$Profile)
    Assert-AuraProfile -Profile $Profile
    if ($Profile -eq 'production') { return 443 }
    return 8443
}

function Get-AuraFunnelTarget {
    param([Parameter(Mandatory)][string]$Profile)
    $port = Get-AuraProfilePort -Profile $Profile
    return "http://127.0.0.1:$port"
}

function Get-TailscalePath {
    $candidates = [System.Collections.Generic.List[string]]::new()
    $programFiles = [Environment]::GetFolderPath(
        [Environment+SpecialFolder]::ProgramFiles
    )
    if (-not [string]::IsNullOrWhiteSpace($programFiles)) {
        $candidates.Add(
            (Join-Path $programFiles 'Tailscale\tailscale.exe')
        )
    }
    $command = Get-Command tailscale.exe -CommandType Application `
        -ErrorAction SilentlyContinue
    if ($null -ne $command) { $candidates.Add($command.Source) }

    foreach ($candidate in $candidates) {
        try {
            $path = [System.IO.Path]::GetFullPath($candidate)
        } catch {
            continue
        }
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }
        $signature = Get-AuthenticodeSignature -LiteralPath $path
        if (
            $signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid `
            -or $null -eq $signature.SignerCertificate `
            -or -not $signature.SignerCertificate.Subject.StartsWith(
                'CN=Tailscale Inc.,', [StringComparison]::Ordinal
            )
        ) {
            continue
        }
        return $path
    }
    throw 'AURA_TAILSCALE_NOT_FOUND'
}

function Get-AuraFunnelConfigs {
    param([Parameter(Mandatory)]$Config)
    Write-Output -NoEnumerate $Config
    $foregroundProperty = $Config.PSObject.Properties['Foreground']
    if ($null -eq $foregroundProperty -or $null -eq $foregroundProperty.Value) { return }
    foreach ($entry in $foregroundProperty.Value.PSObject.Properties) {
        Get-AuraFunnelConfigs -Config $entry.Value
    }
}

function Test-AuraFunnelStatusObject {
    param(
        [Parameter(Mandatory)]$Status,
        [Parameter(Mandatory)][string]$Profile
    )
    $publicPort = Get-AuraFunnelPort -Profile $Profile
    $target = Get-AuraFunnelTarget -Profile $Profile
    foreach ($config in Get-AuraFunnelConfigs -Config $Status) {
        $allowProperty = $config.PSObject.Properties['AllowFunnel']
        $tcpProperty = $config.PSObject.Properties['TCP']
        $webProperty = $config.PSObject.Properties['Web']
        if ($null -eq $allowProperty -or $null -eq $tcpProperty -or $null -eq $webProperty) { continue }
        foreach ($allowEntry in $allowProperty.Value.PSObject.Properties) {
            $hostPort = $allowEntry.Name
            if ($allowEntry.Value -ne $true -or -not $hostPort.EndsWith(":$publicPort", [StringComparison]::Ordinal)) { continue }
            $tcpEntry = $tcpProperty.Value.PSObject.Properties[[string]$publicPort]
            $webEntry = $webProperty.Value.PSObject.Properties[$hostPort]
            if ($null -eq $tcpEntry -or $null -eq $webEntry -or $tcpEntry.Value.HTTPS -ne $true) { continue }
            $handlers = $webEntry.Value.PSObject.Properties['Handlers']
            if ($null -eq $handlers) { continue }
            $rootHandler = $handlers.Value.PSObject.Properties['/']
            if ($null -ne $rootHandler -and $rootHandler.Value.Proxy -eq $target) { return $true }
        }
    }
    return $false
}

function Get-AuraTailscaleStatus {
    $tailscale = Get-TailscalePath
    $rawStatus = & $tailscale funnel status --json 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($rawStatus -join ''))) {
        throw 'AURA_FUNNEL_STATUS_FAILED'
    }
    try {
        return (($rawStatus -join "`n") | ConvertFrom-Json)
    } catch {
        throw 'AURA_FUNNEL_STATUS_INVALID'
    }
}

function Get-AuraFunnelBaseUri {
    param([Parameter(Mandatory)][string]$Profile)
    $tailscale = Get-TailscalePath
    $rawStatus = & $tailscale status --json 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'AURA_TAILSCALE_STATUS_FAILED' }
    try {
        $status = ($rawStatus -join "`n") | ConvertFrom-Json
        $dnsName = [string]$status.Self.DNSName
    } catch {
        throw 'AURA_TAILSCALE_STATUS_INVALID'
    }
    $dnsName = $dnsName.TrimEnd('.').ToLowerInvariant()
    if ($dnsName -notmatch '^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.ts\.net$') {
        throw 'AURA_TAILSCALE_DNS_INVALID'
    }
    $publicPort = Get-AuraFunnelPort -Profile $Profile
    if ($publicPort -eq 443) { return "https://$dnsName" }
    return "https://${dnsName}:$publicPort"
}

function Get-AuraSecretPath {
    param([Parameter(Mandatory)][string]$Profile)
    Assert-AuraProfile -Profile $Profile
    return Join-Path $script:AuraSecretRoot "$Profile.conf"
}

function Assert-AuraPathWithin {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Root
    )
    $resolvedPath = [System.IO.Path]::GetFullPath($Path)
    $resolvedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    if (-not $resolvedPath.StartsWith($resolvedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'AURA_PATH_OUTSIDE_ALLOWLIST'
    }
    return $resolvedPath
}

function ConvertTo-AuraPostgreSQLVersion {
    param([Parameter(Mandatory)][string]$Name)

    if ($Name -notmatch '^[0-9]+(?:\.[0-9]+){0,3}$') { return $null }
    $components = @($Name.Split('.'))
    while ($components.Count -lt 2) { $components += '0' }
    try {
        return [Version]::Parse(($components -join '.'))
    } catch {
        return $null
    }
}

function Get-AuraPostgreSQLToolCandidate {
    param(
        [Parameter(Mandatory)]
        [ValidateSet('psql.exe', 'pg_dump.exe', 'pg_restore.exe', 'createdb.exe', 'dropdb.exe')]
        [string]$ToolName,
        [Parameter(Mandatory)][string]$CandidatePath,
        [Parameter(Mandatory)][string]$InstallRoot
    )

    try {
        $root = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
        $path = [IO.Path]::GetFullPath($CandidatePath)
    } catch {
        return $null
    }
    if (
        [string]::IsNullOrWhiteSpace($root) `
        -or -not $path.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase) `
        -or -not [IO.Path]::GetFileName($path).Equals($ToolName, [StringComparison]::OrdinalIgnoreCase) `
        -or -not (Test-Path -LiteralPath $path -PathType Leaf)
    ) {
        return $null
    }

    $relative = $path.Substring($root.Length + 1)
    $parts = @($relative.Split('\'))
    $layout = $null
    $directories = @()
    if (
        $parts.Count -eq 3 `
        -and $parts[1].Equals('bin', [StringComparison]::OrdinalIgnoreCase) `
        -and $parts[2].Equals($ToolName, [StringComparison]::OrdinalIgnoreCase)
    ) {
        $layout = 'bin'
        $directories = @(
            (Join-Path $root $parts[0]),
            (Join-Path (Join-Path $root $parts[0]) 'bin')
        )
    } elseif (
        $parts.Count -eq 4 `
        -and $parts[1].Equals('pgAdmin 4', [StringComparison]::OrdinalIgnoreCase) `
        -and $parts[2].Equals('runtime', [StringComparison]::OrdinalIgnoreCase) `
        -and $parts[3].Equals($ToolName, [StringComparison]::OrdinalIgnoreCase)
    ) {
        $layout = 'pgadmin-runtime'
        $versionRoot = Join-Path $root $parts[0]
        $pgAdminRoot = Join-Path $versionRoot 'pgAdmin 4'
        $directories = @(
            $versionRoot,
            $pgAdminRoot,
            (Join-Path $pgAdminRoot 'runtime')
        )
    } else {
        return $null
    }

    $version = ConvertTo-AuraPostgreSQLVersion -Name $parts[0]
    if ($null -eq $version) { return $null }
    foreach ($itemPath in @($root) + $directories + @($path)) {
        try {
            $item = Get-Item -LiteralPath $itemPath -Force
        } catch {
            return $null
        }
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            return $null
        }
    }
    return [PSCustomObject]@{
        Path = $path
        Layout = $layout
        Version = $version
    }
}

function Select-AuraPostgreSQLTool {
    param(
        [Parameter(Mandatory)]
        [ValidateSet('psql.exe', 'pg_dump.exe', 'pg_restore.exe', 'createdb.exe', 'dropdb.exe')]
        [string]$ToolName,
        [Parameter(Mandatory)][string]$InstallRoot,
        [string[]]$PathCandidates = @()
    )

    $pathRuntime = $null
    foreach ($candidatePath in $PathCandidates) {
        $candidate = Get-AuraPostgreSQLToolCandidate `
            -ToolName $ToolName -CandidatePath $candidatePath `
            -InstallRoot $InstallRoot
        if ($null -eq $candidate) { continue }
        if ($candidate.Layout -eq 'bin') { return $candidate.Path }
        if ($null -eq $pathRuntime) { $pathRuntime = $candidate }
    }

    $installed = @()
    if (Test-Path -LiteralPath $InstallRoot -PathType Container) {
        foreach ($versionDirectory in Get-ChildItem -LiteralPath $InstallRoot -Directory) {
            $version = ConvertTo-AuraPostgreSQLVersion -Name $versionDirectory.Name
            if ($null -eq $version) { continue }
            foreach ($relativePath in @(
                (Join-Path 'bin' $ToolName),
                (Join-Path 'pgAdmin 4\runtime' $ToolName)
            )) {
                $candidate = Get-AuraPostgreSQLToolCandidate `
                    -ToolName $ToolName `
                    -CandidatePath (Join-Path $versionDirectory.FullName $relativePath) `
                    -InstallRoot $InstallRoot
                if ($null -ne $candidate) { $installed += $candidate }
            }
        }
    }
    $official = @($installed | Where-Object Layout -eq 'bin' | Sort-Object Version -Descending)
    if ($official.Count -gt 0) { return $official[0].Path }
    if ($null -ne $pathRuntime) { return $pathRuntime.Path }
    $runtime = @(
        $installed | Where-Object Layout -eq 'pgadmin-runtime' |
            Sort-Object Version -Descending
    )
    if ($runtime.Count -gt 0) { return $runtime[0].Path }
    throw 'AURA_POSTGRESQL_TOOL_NOT_FOUND'
}

function Resolve-AuraPostgreSQLTool {
    param(
        [Parameter(Mandatory)]
        [ValidateSet('psql.exe', 'pg_dump.exe', 'pg_restore.exe', 'createdb.exe', 'dropdb.exe')]
        [string]$ToolName
    )

    $programFiles = [Environment]::GetFolderPath(
        [Environment+SpecialFolder]::ProgramFiles
    )
    if ([string]::IsNullOrWhiteSpace($programFiles)) {
        throw 'AURA_POSTGRESQL_INSTALL_ROOT_NOT_FOUND'
    }
    $installRoot = Join-Path $programFiles 'PostgreSQL'
    $pathCandidates = @(
        Get-Command $ToolName -CommandType Application -All `
            -ErrorAction SilentlyContinue |
            ForEach-Object { $_.Source }
    )
    return Select-AuraPostgreSQLTool -ToolName $ToolName `
        -InstallRoot $installRoot -PathCandidates $pathCandidates
}

function Initialize-AuraDataDirectories {
    foreach ($path in @($script:AuraLogRoot, $script:AuraBackupRoot, $script:AuraRunRoot)) {
        [void](New-Item -ItemType Directory -Path $path -Force)
    }
}

function Assert-AuraSecretAcl {
    param([Parameter(Mandatory)][string]$Path)
    $acl = Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected) {
        throw 'AURA_SECRET_ACL_INHERITANCE_ENABLED'
    }
    $blocked = @(
        'S-1-1-0',      # Everyone
        'S-1-3-0',      # CREATOR OWNER
        'S-1-5-11',     # Authenticated Users
        'S-1-5-32-545'  # BUILTIN\Users
    )
    foreach ($rule in $acl.Access) {
        try {
            $sid = $rule.IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]
            ).Value
        } catch {
            throw 'AURA_SECRET_ACL_IDENTITY_INVALID'
        }
        if ($sid -in $blocked) {
            throw 'AURA_SECRET_ACL_TOO_BROAD'
        }
    }
}

function Assert-AuraOperatorSecretAcl {
    param([Parameter(Mandatory)][string]$Path)
    Assert-AuraSecretAcl -Path $Path
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $allowed = @($currentSid, 'S-1-5-18', 'S-1-5-32-544')
    $currentUserAllowed = $false
    foreach ($rule in (Get-Acl -LiteralPath $Path).Access) {
        try {
            $sid = $rule.IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]
            ).Value
        } catch {
            throw 'AURA_SECRET_ACL_IDENTITY_INVALID'
        }
        if (
            $rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow `
            -and $sid -notin $allowed
        ) {
            throw 'AURA_SECRET_ACL_UNEXPECTED_IDENTITY'
        }
        if (
            $rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow `
            -and $sid -eq $currentSid
        ) {
            $currentUserAllowed = $true
        }
    }
    if (-not $currentUserAllowed) { throw 'AURA_SECRET_ACL_OPERATOR_MISSING' }
}

function Set-AuraOperatorProtectedAcl {
    param(
        [Parameter(Mandatory)][string]$Path,
        [switch]$Container
    )

    $item = Get-Item -LiteralPath $Path -Force
    if (
        ($Container -and -not $item.PSIsContainer) `
        -or (-not $Container -and $item.PSIsContainer)
    ) {
        throw 'AURA_PROTECTED_ACL_PATH_TYPE_INVALID'
    }
    $acl = if ($Container) {
        [Security.AccessControl.DirectorySecurity]::new()
    } else {
        [Security.AccessControl.FileSecurity]::new()
    }
    $acl.SetAccessRuleProtection($true, $false)

    $inheritance = if ($Container) {
        [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor `
            [Security.AccessControl.InheritanceFlags]::ObjectInherit
    } else {
        [Security.AccessControl.InheritanceFlags]::None
    }
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    foreach ($sid in @(
        [Security.Principal.SecurityIdentifier]::new('S-1-5-18'),
        [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'),
        $currentSid
    )) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $sid,
            [Security.AccessControl.FileSystemRights]::FullControl,
            $inheritance,
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        )
        [void]$acl.AddAccessRule($rule)
    }
    if ($Container) {
        [IO.Directory]::SetAccessControl($item.FullName, $acl)
    } else {
        [IO.File]::SetAccessControl($item.FullName, $acl)
    }
    Assert-AuraOperatorSecretAcl -Path $item.FullName
}

function Import-AuraConfiguration {
    param([Parameter(Mandatory)][string]$Profile)
    $path = Get-AuraSecretPath -Profile $Profile
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw 'AURA_SECRET_FILE_MISSING'
    }
    Assert-AuraSecretAcl -Path $path
    $allowed = [System.Collections.Generic.HashSet[string]]::new(
        [StringComparer]::Ordinal
    )
    foreach ($name in @(
        'APP_ENV', 'DEMO_DATABASE_URL', 'DEMO_BFF_SERVICE_TOKEN',
        'AUTH_JWT_SECRET', 'AUTH_JWT_ISSUER', 'AUTH_JWT_AUDIENCE',
        'AUTH_JWT_EXPIRE_MINUTES', 'AI_PROVIDER', 'OPENAI_MODEL',
        'OPENAI_API_KEY', 'OLLAMA_BASE_URL', 'OLLAMA_MODEL',
        'AI_PROVIDER_TIMEOUT_SECONDS', 'SQL_ECHO', 'AURA_LOG_RETENTION_DAYS',
        'AURA_BACKUP_RETENTION_DAYS', 'AURA_DB_HOST', 'AURA_DB_PORT',
        'AURA_DB_NAME', 'AURA_DB_USER', 'AURA_MIGRATION_USER', 'PGPASSFILE'
    )) { [void]$allowed.Add($name) }

    $previous = @{}
    try {
        foreach ($line in [System.IO.File]::ReadLines($path)) {
            if ([string]::IsNullOrWhiteSpace($line) -or $line.StartsWith('#')) { continue }
            if ($line -notmatch '^([A-Z][A-Z0-9_]*)=(.+)$') {
                throw 'AURA_SECRET_FILE_FORMAT_INVALID'
            }
            $name = $Matches[1]
            $value = $Matches[2]
            if (-not $allowed.Contains($name) -or $value -ne $value.Trim() -or $value -match '[\x00-\x1F\x7F]') {
                throw 'AURA_SECRET_FILE_VALUE_INVALID'
            }
            if ($previous.ContainsKey($name)) { throw 'AURA_SECRET_FILE_DUPLICATE_KEY' }
            $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
            [Environment]::SetEnvironmentVariable($name, $value, 'Process')
        }
        if ($env:APP_ENV -ne 'demo' -or $env:SQL_ECHO -ne 'false') {
            throw 'AURA_SECRET_FILE_RUNTIME_INVALID'
        }
    } catch {
        Restore-AuraProcessEnvironment -Previous $previous
        throw
    }
    return $previous
}

function Restore-AuraProcessEnvironment {
    param([Parameter(Mandatory)][hashtable]$Previous)
    foreach ($name in $Previous.Keys) {
        [Environment]::SetEnvironmentVariable($name, $Previous[$name], 'Process')
    }
}

function ConvertFrom-AuraSchemaProcessResult {
    param(
        [ValidateSet('staging', 'production')]
        [string]$Profile = 'staging',
        [Parameter(Mandatory)]
        [ValidateSet('plan', 'apply-empty-schema', 'verify')]
        [string]$Operation,
        [Parameter(Mandatory)][int]$ExitCode,
        [Parameter(Mandatory)][AllowEmptyString()][string]$StandardOutput,
        [Parameter(Mandatory)][AllowEmptyString()][string]$StandardError
    )

    # stderr is captured by the caller but is never rendered because Python and
    # database libraries may put environment-specific diagnostics there.
    $null = $StandardError
    $codes = if ($Profile -eq 'production') {
        @{
            Result = 'AURA_PRODUCTION_SCHEMA_RESULT_INVALID'
            State = 'AURA_PRODUCTION_SCHEMA_STATE_INVALID'
            Plan = 'AURA_PRODUCTION_SCHEMA_PLAN_OPERATION_FAILED'
            Apply = 'AURA_PRODUCTION_SCHEMA_APPLY_OPERATION_FAILED'
            Verify = 'AURA_PRODUCTION_SCHEMA_VERIFY_OPERATION_FAILED'
        }
    } else {
        @{
            Result = 'AURA_STAGING_SCHEMA_RESULT_INVALID'
            State = 'AURA_STAGING_SCHEMA_STATE_INVALID'
            Plan = 'AURA_STAGING_SCHEMA_PLAN_OPERATION_FAILED'
            Apply = 'AURA_STAGING_SCHEMA_APPLY_OPERATION_FAILED'
            Verify = 'AURA_STAGING_SCHEMA_VERIFY_OPERATION_FAILED'
        }
    }
    $operationFailure = switch ($Operation) {
        'plan' { $codes.Plan }
        'apply-empty-schema' { $codes.Apply }
        'verify' { $codes.Verify }
    }
    if ($ExitCode -ne 0) { throw $operationFailure }

    try {
        $document = $StandardOutput | ConvertFrom-Json
    } catch {
        throw $codes.Result
    }
    if ($null -eq $document -or $document -is [array]) {
        throw $codes.Result
    }
    $statusProperty = $document.PSObject.Properties['status']
    if ($null -eq $statusProperty) { throw $codes.Result }
    if ($statusProperty.Value -in @('failed', 'blocked')) {
        throw $operationFailure
    }
    foreach ($name in @(
        'operation', 'classification', 'expectedTableCount',
        'actualTableCount', 'expectedColumnCount', 'matchingColumnCount',
        'matchingPrimaryKeyCount', 'matchingTableStructureCount'
    )) {
        if ($null -eq $document.PSObject.Properties[$name]) {
            throw $codes.Result
        }
    }
    if ($document.operation -ne $Operation -or [int]$document.expectedTableCount -ne 10) {
        throw $codes.State
    }
    if ($Operation -eq 'plan') {
        if (
            $document.status -ne 'ready' `
            -or $document.classification -ne 'additive-empty-schema' `
            -or [int]$document.actualTableCount -ne 0 `
            -or [int]$document.matchingColumnCount -ne 0 `
            -or [int]$document.matchingPrimaryKeyCount -ne 0 `
            -or [int]$document.matchingTableStructureCount -ne 0
        ) {
            throw $codes.State
        }
    } elseif (
        $document.status -ne 'verified' `
        -or $document.classification -ne 'converged' `
        -or [int]$document.actualTableCount -ne 10 `
        -or [int]$document.matchingColumnCount -ne [int]$document.expectedColumnCount `
        -or [int]$document.matchingPrimaryKeyCount -ne 10 `
        -or [int]$document.matchingTableStructureCount -ne 10
    ) {
        throw $codes.State
    }
    return $document
}

function Get-AuraPythonPath {
    $path = Join-Path (Get-AuraRepositoryRoot) '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw 'AURA_PYTHON_NOT_FOUND'
    }
    $path = [System.IO.Path]::GetFullPath($path)
    $signature = Get-AuthenticodeSignature -LiteralPath $path
    if (
        $signature.Status -ne `
            [System.Management.Automation.SignatureStatus]::Valid `
        -or $null -eq $signature.SignerCertificate `
        -or -not $signature.SignerCertificate.Subject.StartsWith(
            'CN=Python Software Foundation', [StringComparison]::Ordinal
        )
    ) { throw 'AURA_PYTHON_SIGNATURE_INVALID' }
    return $path
}

function Get-AuraPythonRuntimePaths {
    $launcher = Get-AuraPythonPath
    $paths = [System.Collections.Generic.List[string]]::new()
    $paths.Add($launcher)
    try {
        $rawBase = & $launcher -I -c `
            'import sys; print(sys._base_executable)' 2>$null
        if ($LASTEXITCODE -eq 0 -and @($rawBase).Count -eq 1) {
            $base = [IO.Path]::GetFullPath(([string]$rawBase).Trim())
            if (Test-Path -LiteralPath $base -PathType Leaf) {
                $item = Get-Item -LiteralPath $base -Force
                $signature = Get-AuthenticodeSignature -LiteralPath $base
                if (
                    ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0 `
                    -and $signature.Status -eq `
                        [System.Management.Automation.SignatureStatus]::Valid `
                    -and $null -ne $signature.SignerCertificate `
                    -and $signature.SignerCertificate.Subject.StartsWith(
                        'CN=Python Software Foundation',
                        [StringComparison]::Ordinal
                    )
                ) {
                    $paths.Add($base)
                }
            }
        }
    } catch { }
    return @($paths | Select-Object -Unique)
}

function Remove-AuraExpiredFiles {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$Filter,
        [Parameter(Mandatory)][int]$RetentionDays,
        [string]$PreservePath = ''
    )
    if ($RetentionDays -lt 1 -or $RetentionDays -gt 365) {
        throw 'AURA_RETENTION_INVALID'
    }
    $safeRoot = Assert-AuraPathWithin -Path ((Join-Path $Root 'sentinel')) -Root $script:AuraDataRoot
    $null = $safeRoot
    $cutoff = [DateTime]::UtcNow.AddDays(-$RetentionDays)
    foreach ($file in Get-ChildItem -LiteralPath $Root -File -Filter $Filter) {
        $safeFile = Assert-AuraPathWithin -Path $file.FullName -Root $Root
        if ($file.LastWriteTimeUtc -lt $cutoff -and $safeFile -ne $PreservePath) {
            Remove-Item -LiteralPath $safeFile -Force
        }
    }
}

function Assert-AuraProductionProfile {
    param([Parameter(Mandatory)][string]$Profile)
    if ($Profile -cne 'production') { throw 'AURA_PROFILE_INVALID' }
}

function Assert-AuraRepositoryLayout {
    $root = Get-AuraRepositoryRoot
    foreach ($relativePath in @(
        'app\self_host.py',
        'app\jobs\demo_schema.py',
        'deploy\windows\AuraWindows.Common.ps1'
    )) {
        $candidate = Assert-AuraPathWithin -Path (Join-Path $root $relativePath) `
            -Root $root
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            throw 'AURA_REPOSITORY_LAYOUT_INVALID'
        }
    }
    return $root
}

function Get-AuraPgPassPath {
    param([Parameter(Mandatory)][string]$Profile)
    Assert-AuraProfile -Profile $Profile
    return Join-Path $script:AuraSecretRoot "$Profile.pgpass"
}

function Assert-AuraProductionConfiguration {
    if (
        $env:APP_ENV -ne 'demo' `
        -or $env:AURA_DB_HOST -ne '127.0.0.1' `
        -or $env:AURA_DB_PORT -ne '5432' `
        -or $env:AURA_DB_NAME -ne 'aura_demo_public' `
        -or $env:AURA_DB_USER -ne 'aura_public_runtime' `
        -or $env:DEMO_DATABASE_URL -ne (
            'postgresql+psycopg://aura_public_runtime@' +
            '127.0.0.1:5432/aura_demo_public'
        )
    ) {
        throw 'AURA_PRODUCTION_CONFIGURATION_INVALID'
    }
    $expectedPgPass = [IO.Path]::GetFullPath((Get-AuraPgPassPath -Profile production))
    if (
        [string]::IsNullOrWhiteSpace($env:PGPASSFILE) `
        -or [IO.Path]::GetFullPath($env:PGPASSFILE) -ne $expectedPgPass
    ) {
        throw 'AURA_PRODUCTION_PGPASS_TARGET_INVALID'
    }
}

function Test-AuraPostgreSQLServiceRunning {
    $services = @(
        Get-Service -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Name -like 'postgresql*' -or
                $_.DisplayName -like 'PostgreSQL*'
            }
    )
    return @($services | Where-Object Status -eq 'Running').Count -gt 0
}

function Test-AuraPostgreSQLLoopbackListener {
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort 5432 `
        -ErrorAction SilentlyContinue)
    if ($listeners.Count -eq 0) { return $false }
    if (@($listeners | Where-Object LocalAddress -eq '127.0.0.1').Count -eq 0) {
        return $false
    }
    foreach ($listener in $listeners) {
        if ($listener.LocalAddress -notin @('127.0.0.1', '::1')) {
            return $false
        }
    }
    return $true
}

function Test-AuraProductionDatabaseReadiness {
    try {
        Assert-AuraProductionConfiguration
        $python = Get-AuraPythonPath
        & $python -m app.jobs.public_demo_readiness 1>$null 2>$null
        if ($LASTEXITCODE -ne 0) { return $false }
        $schemaOutput = & $python -m app.jobs.demo_schema `
            --operation verify 2>$null
        $schemaExitCode = $LASTEXITCODE
        $null = ConvertFrom-AuraSchemaProcessResult -Profile production `
            -Operation verify -ExitCode $schemaExitCode `
            -StandardOutput ($schemaOutput -join "`n") -StandardError ''
        return $true
    } catch {
        return $false
    }
}

function Test-AuraExactLoopbackListener {
    param(
        [Parameter(Mandatory)][int]$Port,
        [Nullable[int]]$OwningProcess = $null
    )
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port `
        -ErrorAction SilentlyContinue)
    if ($listeners.Count -eq 0) { return $false }
    foreach ($listener in $listeners) {
        if ($listener.LocalAddress -ne '127.0.0.1') { return $false }
        if (
            $null -ne $OwningProcess `
            -and [int]$listener.OwningProcess -ne [int]$OwningProcess
        ) { return $false }
    }
    return $true
}

function Test-AuraPortClosed {
    param([Parameter(Mandatory)][int]$Port)
    return @(
        Get-NetTCPConnection -State Listen -LocalPort $Port `
            -ErrorAction SilentlyContinue
    ).Count -eq 0
}

function Test-AuraHealthContract {
    param(
        [Parameter(Mandatory)][Uri]$Uri,
        [ValidateRange(1, 30)][int]$TimeoutSeconds = 5
    )
    try {
        $response = Invoke-WebRequest -Uri $Uri -Method Get `
            -TimeoutSec $TimeoutSeconds -UseBasicParsing -MaximumRedirection 0
        return (
            $response.StatusCode -eq 200 `
            -and $response.Content -ceq '{"status":"healthy"}' `
            -and $response.Headers['Content-Type'] -match '^application/json'
        )
    } catch {
        return $false
    }
}

function Test-AuraLocalHealth {
    param([Parameter(Mandatory)][string]$Profile)
    $port = Get-AuraProfilePort -Profile $Profile
    return Test-AuraHealthContract -Uri ([Uri]"http://127.0.0.1:$port/health")
}

function Test-AuraPublicHealth {
    param([Parameter(Mandatory)][string]$Profile)
    try {
        $baseUri = Get-AuraFunnelBaseUri -Profile $Profile
        return Test-AuraHealthContract -Uri ([Uri]"$baseUri/health") `
            -TimeoutSeconds 10
    } catch {
        return $false
    }
}

function Test-AuraFirewallRegistryRuleValues {
    param([Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Values)
    $expected = [ordered]@{
        'AURA block direct API 8000' = '8000'
        'AURA block direct API 8001' = '8001'
        'AURA block direct PostgreSQL 5432' = '5432'
    }
    $ruleCounts = @{}
    foreach ($name in $expected.Keys) { $ruleCounts[$name] = 0 }
    foreach ($value in $Values) {
        $fields = @{}
        foreach ($segment in ([string]$value -split '\|')) {
            if ($segment -match '^([^=]+)=(.*)$') {
                $fields[$Matches[1]] = $Matches[2]
            }
        }
        $name = [string]$fields['Name']
        if (-not $expected.Contains($name)) { continue }
        $profileAny = (
            -not $fields.ContainsKey('Profile') `
            -or [string]$fields['Profile'] -in @(
                'All', 'Domain,Private,Public', 'Public,Private,Domain'
            )
        )
        $unscoped = @(
            'App', 'Svc', 'LA4', 'LA6', 'RA4', 'RA6'
        ) | Where-Object { $fields.ContainsKey($_) }
        if (
            [string]$fields['EmbedCtxt'] -eq 'AURA Self-Host' `
            -and [string]$fields['Active'] -eq 'TRUE' `
            -and [string]$fields['Dir'] -eq 'In' `
            -and [string]$fields['Action'] -eq 'Block' `
            -and [string]$fields['Protocol'] -eq '6' `
            -and [string]$fields['LPort'] -eq $expected[$name] `
            -and $profileAny `
            -and @($unscoped).Count -eq 0
        ) { $ruleCounts[$name]++ }
    }
    return @($ruleCounts.Values | Where-Object { $_ -ne 1 }).Count -eq 0
}

function Test-AuraFirewallRules {
    $expected = [ordered]@{
        'AURA block direct API 8000' = '8000'
        'AURA block direct API 8001' = '8001'
        'AURA block direct PostgreSQL 5432' = '5432'
    }
    try {
        $rules = @(Get-NetFirewallRule -Group 'AURA Self-Host' `
            -ErrorAction Stop)
        if ($rules.Count -ne $expected.Count) { return $false }
        foreach ($name in $expected.Keys) {
            $matching = @($rules | Where-Object DisplayName -eq $name)
            if ($matching.Count -ne 1) { return $false }
            $rule = $matching[0]
            if (
                [string]$rule.Enabled -ne 'True' `
                -or [string]$rule.Direction -ne 'Inbound' `
                -or [string]$rule.Action -ne 'Block' `
                -or [string]$rule.Profile -ne 'Any'
            ) { return $false }
            $filters = @(Get-NetFirewallPortFilter `
                -AssociatedNetFirewallRule $rule -ErrorAction Stop)
            if (
                $filters.Count -ne 1 `
                -or [string]$filters[0].Protocol -ne 'TCP' `
                -or [string]$filters[0].LocalPort -ne $expected[$name] `
                -or [string]$filters[0].RemotePort -ne 'Any'
            ) { return $false }
        }
        return $true
    } catch {
        # Standard operators may receive an access-denied CIM error even for a
        # read-only firewall query. The protected machine firewall registry is
        # readable without elevation and provides a stable value-only fallback.
        try {
            $path = 'Registry::HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\' +
                'Services\SharedAccess\Parameters\FirewallPolicy\FirewallRules'
            $item = Get-ItemProperty -LiteralPath $path -ErrorAction Stop
            $values = @($item.PSObject.Properties | Where-Object {
                -not $_.Name.StartsWith('PS', [StringComparison]::Ordinal)
            } | ForEach-Object { [string]$_.Value })
            return Test-AuraFirewallRegistryRuleValues -Values $values
        } catch {
            return $false
        }
    }
}

function Get-AuraProcessCreationTimeUtc {
    param([Parameter(Mandatory)]$ProcessInfo)
    if ($ProcessInfo.CreationDate -is [DateTime]) {
        return $ProcessInfo.CreationDate.ToUniversalTime()
    }
    try {
        return [Management.ManagementDateTimeConverter]::ToDateTime(
            [string]$ProcessInfo.CreationDate
        ).ToUniversalTime()
    } catch {
        throw 'AURA_PROCESS_CREATION_TIME_INVALID'
    }
}

function Get-AuraOwnershipPath {
    param(
        [Parameter(Mandatory)][ValidateSet('aura', 'funnel')][string]$Kind,
        [Parameter(Mandatory)][string]$Profile
    )
    Assert-AuraProfile -Profile $Profile
    $name = if ($Kind -eq 'aura') {
        "aura-$Profile.pid"
    } else {
        "tailscale-funnel-$Profile.pid"
    }
    return Join-Path $script:AuraRunRoot $name
}

function Test-AuraExpectedProcessInfo {
    param(
        [Parameter(Mandatory)]$ProcessInfo,
        [Parameter(Mandatory)][ValidateSet('aura', 'funnel')][string]$Kind,
        [Parameter(Mandatory)][string]$Profile
    )
    $commandLine = [string]$ProcessInfo.CommandLine
    $executablePath = [string]$ProcessInfo.ExecutablePath
    if ([string]::IsNullOrWhiteSpace($commandLine) -or
        [string]::IsNullOrWhiteSpace($executablePath)) { return $false }
    if ($Kind -eq 'aura') {
        $expectedExecutables = @(Get-AuraPythonRuntimePaths)
        $executableExpected = @($expectedExecutables | Where-Object {
            $executablePath.Equals($_, [StringComparison]::OrdinalIgnoreCase)
        }).Count -eq 1
        if (
            $ProcessInfo.Name -notmatch '^python(?:\.exe)?$' `
            -or -not $executableExpected
        ) { return $false }
        $commandExecutables = @($expectedExecutables | ForEach-Object {
            '"?' + [Regex]::Escape($_) + '"?'
        }) -join '|'
        $escapedProfile = [Regex]::Escape($Profile)
        return $commandLine -match (
            '^\s*(?:' + $commandExecutables + ')\s+' +
            '-m\s+app\.self_host\s+' +
            '--profile\s+' + $escapedProfile + '\s*$'
        )
    }
    $expectedTailscale = Get-TailscalePath
    if (
        $ProcessInfo.Name -ne 'tailscale.exe' `
        -or -not $executablePath.Equals(
            $expectedTailscale, [StringComparison]::OrdinalIgnoreCase
        )
    ) { return $false }
    $escapedExecutable = [Regex]::Escape($expectedTailscale)
    $escapedTarget = [Regex]::Escape((Get-AuraFunnelTarget -Profile $Profile))
    $publicPort = Get-AuraFunnelPort -Profile $Profile
    return $commandLine -match (
        '^\s*"?' + $escapedExecutable + '"?\s+funnel\s+' +
        '--https=' + $publicPort + '\s+' + $escapedTarget + '\s*$'
    )
}

function Get-AuraGatewayListenerProcessInfo {
    param(
        [Parameter(Mandatory)]$OwnershipProcessInfo,
        [Parameter(Mandatory)][string]$Profile
    )
    $port = Get-AuraProfilePort -Profile $Profile
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $port `
        -ErrorAction SilentlyContinue)
    if (
        $listeners.Count -eq 0 `
        -or @($listeners | Where-Object LocalAddress -ne '127.0.0.1').Count -gt 0
    ) { return $null }
    $owners = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
    if ($owners.Count -ne 1) { return $null }
    $listenerProcess = Get-CimInstance Win32_Process `
        -Filter "ProcessId = $($owners[0])" -ErrorAction SilentlyContinue
    if (
        $null -eq $listenerProcess `
        -or -not (Test-AuraExpectedProcessInfo -ProcessInfo $listenerProcess `
            -Kind aura -Profile $Profile)
    ) { return $null }
    if (
        [int]$listenerProcess.ProcessId -eq [int]$OwnershipProcessInfo.ProcessId `
        -or [int]$listenerProcess.ParentProcessId -eq `
            [int]$OwnershipProcessInfo.ProcessId
    ) { return $listenerProcess }
    return $null
}

function Read-AuraOwnershipMetadata {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    Assert-AuraOperatorSecretAcl -Path $Path
    $item = Get-Item -LiteralPath $Path -Force
    if (
        $item.Length -lt 1 `
        -or $item.Length -gt 1024 `
        -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
    ) { throw 'AURA_PROCESS_METADATA_INVALID' }
    $raw = (Get-Content -Raw -LiteralPath $Path).Trim()
    if ($raw -match '^[1-9][0-9]*$') {
        return [PSCustomObject]@{
            ProcessId = [int]$raw
            CreationTimeUtc = $null
            Legacy = $true
        }
    }
    try { $document = $raw | ConvertFrom-Json } catch {
        throw 'AURA_PROCESS_METADATA_INVALID'
    }
    if (
        $null -eq $document `
        -or [string]$document.version -ne '1' `
        -or [string]$document.processId -notmatch '^[1-9][0-9]*$' `
        -or [string]::IsNullOrWhiteSpace([string]$document.creationTimeUtc)
    ) { throw 'AURA_PROCESS_METADATA_INVALID' }
    try {
        $created = [DateTime]::ParseExact(
            [string]$document.creationTimeUtc,
            'o',
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind
        ).ToUniversalTime()
    } catch { throw 'AURA_PROCESS_METADATA_INVALID' }
    return [PSCustomObject]@{
        ProcessId = [int]$document.processId
        CreationTimeUtc = $created
        Legacy = $false
    }
}

function Write-AuraOwnershipMetadata {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)]$ProcessInfo
    )
    Initialize-AuraDataDirectories
    Set-AuraOperatorProtectedAcl -Path $script:AuraRunRoot -Container
    $safePath = Assert-AuraPathWithin -Path $Path -Root $script:AuraRunRoot
    $tempPath = "$safePath.partial"
    if (Test-Path -LiteralPath $tempPath) {
        Remove-Item -LiteralPath $tempPath -Force
    }
    $payload = [ordered]@{
        version = 1
        processId = [int]$ProcessInfo.ProcessId
        creationTimeUtc = (Get-AuraProcessCreationTimeUtc -ProcessInfo $ProcessInfo).ToString('o')
    } | ConvertTo-Json -Compress
    try {
        [IO.File]::WriteAllText($tempPath, $payload, [Text.Encoding]::ASCII)
        Set-AuraOperatorProtectedAcl -Path $tempPath
        Move-Item -LiteralPath $tempPath -Destination $safePath -Force
        Assert-AuraOperatorSecretAcl -Path $safePath
    } finally {
        Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
    }
}

function Get-AuraOwnedProcessState {
    param(
        [Parameter(Mandatory)][ValidateSet('aura', 'funnel')][string]$Kind,
        [Parameter(Mandatory)][string]$Profile,
        [switch]$RepairStaleMetadata
    )
    $path = Get-AuraOwnershipPath -Kind $Kind -Profile $Profile
    $metadata = Read-AuraOwnershipMetadata -Path $path
    if ($null -ne $metadata) {
        $processInfo = Get-CimInstance Win32_Process `
            -Filter "ProcessId = $($metadata.ProcessId)" `
            -ErrorAction SilentlyContinue
        if ($null -eq $processInfo) {
            if ($RepairStaleMetadata) { Remove-Item -LiteralPath $path -Force }
            return [PSCustomObject]@{ State = 'stale'; ProcessInfo = $null; Path = $path }
        }
        if (-not (Test-AuraExpectedProcessInfo -ProcessInfo $processInfo `
            -Kind $Kind -Profile $Profile)) {
            return [PSCustomObject]@{ State = 'ambiguous'; ProcessInfo = $null; Path = $path }
        }
        if ($null -ne $metadata.CreationTimeUtc) {
            $actualCreation = Get-AuraProcessCreationTimeUtc -ProcessInfo $processInfo
            if ($actualCreation.ToString('o') -ne $metadata.CreationTimeUtc.ToString('o')) {
                return [PSCustomObject]@{ State = 'ambiguous'; ProcessInfo = $null; Path = $path }
            }
        }
        return [PSCustomObject]@{
            State = 'owned'
            ProcessInfo = $processInfo
            Path = $path
            Legacy = [bool]$metadata.Legacy
        }
    }

    if ($Kind -eq 'aura') {
        $port = Get-AuraProfilePort -Profile $Profile
        $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $port `
            -ErrorAction SilentlyContinue)
        if ($listeners.Count -gt 0) {
            return [PSCustomObject]@{ State = 'uncertain'; ProcessInfo = $null; Path = $path }
        }
    } else {
        $candidates = @(Get-CimInstance Win32_Process -Filter "Name = 'tailscale.exe'" `
            -ErrorAction SilentlyContinue | Where-Object {
                [string]$_.CommandLine -match '\sfunnel\s'
            })
        $otherProfile = if ($Profile -eq 'production') {
            'staging'
        } else { 'production' }
        $unexpected = @($candidates | Where-Object {
            -not (Test-AuraExpectedProcessInfo -ProcessInfo $_ `
                -Kind funnel -Profile $otherProfile)
        })
        if ($unexpected.Count -gt 0) {
            return [PSCustomObject]@{ State = 'ambiguous'; ProcessInfo = $null; Path = $path }
        }
    }
    return [PSCustomObject]@{ State = 'absent'; ProcessInfo = $null; Path = $path }
}

function Test-AuraFunnelProcessesAbsent {
    param([Parameter(Mandatory)][string]$Profile)
    $otherProfile = if ($Profile -eq 'production') {
        'staging'
    } else { 'production' }
    $candidates = @(Get-CimInstance Win32_Process -Filter "Name = 'tailscale.exe'" `
        -ErrorAction SilentlyContinue | Where-Object {
            [string]$_.CommandLine -match '\sfunnel\s'
        })
    foreach ($candidate in $candidates) {
        if (Test-AuraExpectedProcessInfo -ProcessInfo $candidate `
            -Kind funnel -Profile $Profile) { return $false }
        if (-not (Test-AuraExpectedProcessInfo -ProcessInfo $candidate `
            -Kind funnel -Profile $otherProfile)) { return $false }
    }
    return $true
}

function Assert-AuraOwnedProcessStillMatches {
    param(
        [Parameter(Mandatory)]$OriginalProcessInfo,
        [Parameter(Mandatory)][ValidateSet('aura', 'funnel')][string]$Kind,
        [Parameter(Mandatory)][string]$Profile
    )
    $current = Get-CimInstance Win32_Process `
        -Filter "ProcessId = $($OriginalProcessInfo.ProcessId)" `
        -ErrorAction SilentlyContinue
    if ($null -eq $current) { return $null }
    if (-not (Test-AuraExpectedProcessInfo -ProcessInfo $current `
        -Kind $Kind -Profile $Profile)) {
        throw 'HUMAN_GATE_PROCESS_OWNERSHIP_AMBIGUOUS'
    }
    $originalTime = Get-AuraProcessCreationTimeUtc -ProcessInfo $OriginalProcessInfo
    $currentTime = Get-AuraProcessCreationTimeUtc -ProcessInfo $current
    if ($originalTime.ToString('o') -ne $currentTime.ToString('o')) {
        throw 'HUMAN_GATE_PROCESS_OWNERSHIP_AMBIGUOUS'
    }
    return $current
}

function Get-AuraBackupAgeClassification {
    param(
        [string]$Profile = 'production',
        [DateTime]$NowUtc = [DateTime]::UtcNow
    )
    Assert-AuraProfile -Profile $Profile
    $database = if ($Profile -eq 'production') {
        'aura_demo_public'
    } else { 'aura_demo_staging' }
    $latest = Get-ChildItem -LiteralPath $script:AuraBackupRoot -File `
        -Filter "${database}_*.dump" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
    if ($null -eq $latest) { return 'missing' }
    $age = $NowUtc.ToUniversalTime() - $latest.LastWriteTimeUtc.ToUniversalTime()
    if ($age.TotalHours -le 24) { return 'fresh' }
    if ($age.TotalHours -le 48) { return 'warning' }
    return 'stale'
}

function Write-AuraOperationLog {
    param(
        [Parameter(Mandatory)][string]$Profile,
        [Parameter(Mandatory)][ValidatePattern('^[A-Z0-9_]+$')][string]$Stage,
        [Parameter(Mandatory)][ValidatePattern('^[A-Z0-9_]+$')][string]$Code,
        [ValidateRange(0, 3600000)][int]$ElapsedMs = 0
    )
    Initialize-AuraDataDirectories
    $day = [DateTime]::UtcNow.ToString('yyyyMMdd')
    $path = Join-Path $script:AuraLogRoot "operations-$day.log"
    $line = 'timestamp={0} profile={1} stage={2} code={3} elapsed_ms={4}' -f `
        [DateTime]::UtcNow.ToString('o'), $Profile, $Stage, $Code, $ElapsedMs
    Add-Content -LiteralPath $path -Value $line -Encoding ascii
    Remove-AuraExpiredFiles -Root $script:AuraLogRoot `
        -Filter 'operations-*.log' -RetentionDays 14 -PreservePath $path
}

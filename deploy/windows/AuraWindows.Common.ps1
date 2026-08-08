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
    return [System.IO.Path]::GetFullPath($path)
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

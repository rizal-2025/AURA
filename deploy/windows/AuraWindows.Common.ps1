Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:AuraDataRoot = 'C:\ProgramData\AURA'
$script:AuraSecretRoot = 'C:\ProgramData\AURA\secrets'
$script:AuraLogRoot = 'C:\ProgramData\AURA\logs'
$script:AuraBackupRoot = 'C:\ProgramData\AURA\backups'
$script:AuraRunRoot = 'C:\ProgramData\AURA\run'
$script:AuraProviderRuntimeEventLog = Join-Path `
    $script:AuraLogRoot 'provider-runtime-events.jsonl'
$script:AuraProviderRuntimeEventLock = Join-Path `
    $script:AuraLogRoot 'provider-runtime-events.lock'

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
    $item = Get-Item -LiteralPath $Path -Force
    $acl = Get-Acl -LiteralPath $item.FullName
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $systemSid = 'S-1-5-18'
    $administratorsSid = 'S-1-5-32-544'
    $operatorSid = $currentSid

    if ($currentSid -ceq $systemSid) {
        $rules = @($acl.Access)
        if ($rules.Count -ne 3) {
            throw 'AURA_SECRET_ACL_SYSTEM_SHAPE_INVALID'
        }
        $candidateSids = @(
            @(
                foreach ($rule in $rules) {
                    try {
                        $sid = $rule.IdentityReference.Translate(
                            [Security.Principal.SecurityIdentifier]
                        ).Value
                    } catch {
                        throw 'AURA_SECRET_ACL_IDENTITY_INVALID'
                    }
                    if ($sid -notin @($systemSid, $administratorsSid)) {
                        $sid
                    }
                }
            ) | Sort-Object -Unique
        )
        if (
            $candidateSids.Count -ne 1 `
            -or $candidateSids[0] -cnotmatch '^S-1-5-21-(?:\d+-){3}\d+$'
        ) {
            throw 'AURA_SECRET_ACL_SYSTEM_OPERATOR_INVALID'
        }
        $operatorSid = $candidateSids[0]
        $expectedInheritance = if ($item.PSIsContainer) {
            [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [Security.AccessControl.InheritanceFlags]::ObjectInherit
        } else {
            [Security.AccessControl.InheritanceFlags]::None
        }
        $seen = @{}
        foreach ($rule in $rules) {
            $sid = $rule.IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]
            ).Value
            if (
                $rule.AccessControlType -ne
                    [Security.AccessControl.AccessControlType]::Allow `
                -or $rule.IsInherited `
                -or $rule.FileSystemRights -ne
                    [Security.AccessControl.FileSystemRights]::FullControl `
                -or $rule.InheritanceFlags -ne $expectedInheritance `
                -or $rule.PropagationFlags -ne
                    [Security.AccessControl.PropagationFlags]::None `
                -or $seen.ContainsKey($sid)
            ) {
                throw 'AURA_SECRET_ACL_SYSTEM_SHAPE_INVALID'
            }
            $seen[$sid] = $true
        }
        foreach ($requiredSid in @(
            $systemSid, $administratorsSid, $operatorSid
        )) {
            if (-not $seen.ContainsKey($requiredSid)) {
                throw 'AURA_SECRET_ACL_SYSTEM_SHAPE_INVALID'
            }
        }
    }

    $allowed = @($operatorSid, $systemSid, $administratorsSid)
    $currentUserAllowed = $false
    $operatorAllowed = $false
    foreach ($rule in $acl.Access) {
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
        if (
            $rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow `
            -and $sid -eq $operatorSid
        ) {
            $operatorAllowed = $true
        }
    }
    if (-not $currentUserAllowed -or -not $operatorAllowed) {
        throw 'AURA_SECRET_ACL_OPERATOR_MISSING'
    }
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

function Assert-AuraOperatorRuntimeContainerAcl {
    param([Parameter(Mandatory)][string]$Path)

    $item = Get-Item -LiteralPath $Path -Force
    $invalidPathType = -not $item.PSIsContainer -or (
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
    )
    if ($invalidPathType) { throw 'AURA_RUNTIME_ACL_PATH_TYPE_INVALID' }

    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $expectedRights = @{
        $currentSid = (
            [Security.AccessControl.FileSystemRights]::Modify -bor
            [Security.AccessControl.FileSystemRights]::Synchronize
        )
        'S-1-5-18' = [Security.AccessControl.FileSystemRights]::FullControl
        'S-1-5-32-544' = [Security.AccessControl.FileSystemRights]::FullControl
    }
    $expectedInheritance = (
        [Security.AccessControl.InheritanceFlags]::ObjectInherit -bor
        [Security.AccessControl.InheritanceFlags]::ContainerInherit
    )
    $expectedPropagation = [Security.AccessControl.PropagationFlags]::None
    $acl = Get-Acl -LiteralPath $item.FullName
    if (-not $acl.AreAccessRulesProtected) {
        throw 'AURA_RUNTIME_ACL_INHERITANCE_ENABLED'
    }
    $rules = @($acl.Access)
    if ($rules.Count -ne $expectedRights.Count) {
        throw 'AURA_RUNTIME_ACL_ACE_COUNT_INVALID'
    }

    $seen = @{}
    foreach ($rule in $rules) {
        if ($rule.IsInherited) { throw 'AURA_RUNTIME_ACL_INHERITED_RULE_FOUND' }
        if (
            $rule.AccessControlType -ne
            [Security.AccessControl.AccessControlType]::Allow
        ) { throw 'AURA_RUNTIME_ACL_DENY_OR_UNKNOWN_TYPE_FOUND' }
        try {
            $sid = $rule.IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]
            ).Value
        } catch { throw 'AURA_RUNTIME_ACL_IDENTITY_INVALID' }
        if (-not $expectedRights.ContainsKey($sid)) {
            throw 'AURA_RUNTIME_ACL_UNEXPECTED_IDENTITY'
        }
        if ($seen.ContainsKey($sid)) { throw 'AURA_RUNTIME_ACL_DUPLICATE_ACE' }
        if ([long]$rule.FileSystemRights -ne [long]$expectedRights[$sid]) {
            throw 'AURA_RUNTIME_ACL_RIGHTS_INVALID'
        }
        if ($rule.InheritanceFlags -ne $expectedInheritance) {
            throw 'AURA_RUNTIME_ACL_INHERITANCE_FLAGS_INVALID'
        }
        if ($rule.PropagationFlags -ne $expectedPropagation) {
            throw 'AURA_RUNTIME_ACL_PROPAGATION_FLAGS_INVALID'
        }
        $seen[$sid] = $true
    }
    foreach ($sid in $expectedRights.Keys) {
        if (-not $seen.ContainsKey($sid)) {
            throw 'AURA_RUNTIME_ACL_REQUIRED_IDENTITY_MISSING'
        }
    }
}

function Get-AuraProviderRuntimeEventLogPath {
    return $script:AuraProviderRuntimeEventLog
}

function Get-AuraProviderRuntimeEventLockPath {
    return $script:AuraProviderRuntimeEventLock
}

function Initialize-AuraProviderRuntimeEventFile {
    param(
        [Parameter(Mandatory)][string]$Path,
        [switch]$LockFile
    )

    $path = Assert-AuraPathWithin -Path $Path -Root $script:AuraLogRoot
    $created = $false
    if (-not (Test-Path -LiteralPath $path)) {
        $stream = $null
        try {
            $stream = [IO.File]::Open(
                $path,
                [IO.FileMode]::CreateNew,
                [IO.FileAccess]::ReadWrite,
                [IO.FileShare]::ReadWrite
            )
            if ($LockFile) { $stream.WriteByte(0) }
            $stream.Flush($true)
            $created = $true
        } catch [IO.IOException] {
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw }
        } finally {
            if ($null -ne $stream) { $stream.Dispose() }
        }
    }
    $item = Get-Item -LiteralPath $path -Force
    if (
        $item.PSIsContainer `
        -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
    ) { throw 'AURA_PROVIDER_RUNTIME_EVENT_PATH_INVALID' }
    try {
        if ($created) { Set-AuraOperatorProtectedAcl -Path $path }
        Assert-AuraOperatorSecretAcl -Path $path
        if ($LockFile -and (Get-Item -LiteralPath $path -Force).Length -lt 1) {
            throw 'AURA_PROVIDER_RUNTIME_EVENT_LOCK_PATH_INVALID'
        }
    } catch {
        if ($created) {
            Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        }
        throw 'AURA_PROVIDER_RUNTIME_EVENT_ACL_INVALID'
    }
    return $path
}

function Initialize-AuraProviderRuntimeEventSink {
    Initialize-AuraDataDirectories
    Assert-AuraOperatorRuntimeContainerAcl -Path $script:AuraLogRoot
    $eventPath = Initialize-AuraProviderRuntimeEventFile `
        -Path $script:AuraProviderRuntimeEventLog
    $lockPath = Initialize-AuraProviderRuntimeEventFile `
        -Path $script:AuraProviderRuntimeEventLock -LockFile
    return [PSCustomObject]@{
        EventPath = $eventPath
        LockPath = $lockPath
    }
}

function ConvertFrom-AuraProviderRuntimeEventLines {
    param(
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Lines,
        [Parameter(Mandatory)][string]$RequestId,
        [Parameter(Mandatory)][string]$NotBeforeUtc,
        [ValidateRange(1, 64)][int]$MaxRecords = 32
    )

    $requestGuid = [Guid]::Empty
    if (
        -not [Guid]::TryParseExact($RequestId, 'D', [ref]$requestGuid) `
        -or $RequestId -cne $requestGuid.ToString('D')
    ) { throw 'AURA_PROVIDER_RUNTIME_REQUEST_ID_INVALID' }
    if ($NotBeforeUtc -cnotmatch `
        '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$') {
        throw 'AURA_PROVIDER_RUNTIME_TIMESTAMP_INVALID'
    }
    $notBefore = [DateTimeOffset]::MinValue
    if (-not [DateTimeOffset]::TryParseExact(
        $NotBeforeUtc,
        'yyyy-MM-ddTHH:mm:ss.fffZ',
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::AssumeUniversal,
        [ref]$notBefore
    )) { throw 'AURA_PROVIDER_RUNTIME_TIMESTAMP_INVALID' }

    $commonProperties = @(
        'event', 'model', 'operation', 'provider', 'request_id', 'timestamp'
    )
    $outcomes = @(
        'AUTH', 'BILLING', 'CLIENT_ERROR', 'PROVIDER_ERROR', 'RATE_LIMIT',
        'SUCCESS', 'TIMEOUT', 'UNKNOWN_ERROR'
    )
    $records = [System.Collections.Generic.List[object]]::new()
    foreach ($line in $Lines) {
        if ([string]::IsNullOrWhiteSpace($line) -or $line.Length -gt 2048) {
            throw 'AURA_PROVIDER_RUNTIME_EVENT_RECORD_INVALID'
        }
        try { $record = $line | ConvertFrom-Json } catch {
            throw 'AURA_PROVIDER_RUNTIME_EVENT_RECORD_INVALID'
        }
        if ($null -eq $record -or $record -is [array]) {
            throw 'AURA_PROVIDER_RUNTIME_EVENT_RECORD_INVALID'
        }

        $eventType = [string]$record.event
        $expectedProperties = switch ($eventType) {
            'AI_PROVIDER_ATTEMPT' { $commonProperties }
            'AI_PROVIDER_OUTCOME' {
                $commonProperties + @('elapsed_ms', 'outcome')
            }
            'AI_PROVIDER_FALLBACK' {
                $commonProperties + @('locale', 'reason')
            }
            default { throw 'AURA_PROVIDER_RUNTIME_EVENT_RECORD_INVALID' }
        }
        $actualProperties = @($record.PSObject.Properties.Name | Sort-Object)
        $expectedProperties = @($expectedProperties | Sort-Object)
        if (
            $actualProperties.Count -ne $expectedProperties.Count `
            -or @(Compare-Object $actualProperties $expectedProperties `
                -CaseSensitive).Count -ne 0
        ) { throw 'AURA_PROVIDER_RUNTIME_EVENT_RECORD_INVALID' }

        $recordGuid = [Guid]::Empty
        $recordRequestId = [string]$record.request_id
        $timestamp = [DateTimeOffset]::MinValue
        if (
            -not [Guid]::TryParseExact($recordRequestId, 'D', [ref]$recordGuid) `
            -or $recordRequestId -cne $recordGuid.ToString('D') `
            -or [string]$record.provider -cne 'openai' `
            -or [string]$record.operation -cne 'responses.create' `
            -or [string]$record.model -cnotmatch `
                '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$' `
            -or [string]$record.timestamp -cnotmatch `
                '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$' `
            -or -not [DateTimeOffset]::TryParseExact(
                [string]$record.timestamp,
                'yyyy-MM-ddTHH:mm:ss.fffZ',
                [Globalization.CultureInfo]::InvariantCulture,
                [Globalization.DateTimeStyles]::AssumeUniversal,
                [ref]$timestamp
            )
        ) { throw 'AURA_PROVIDER_RUNTIME_EVENT_RECORD_INVALID' }

        if ($eventType -eq 'AI_PROVIDER_OUTCOME') {
            if (
                ($record.elapsed_ms -isnot [int] `
                    -and $record.elapsed_ms -isnot [long]) `
                -or [long]$record.elapsed_ms -lt 0 `
                -or [long]$record.elapsed_ms -gt 3600000 `
                -or [string]$record.outcome -cnotin $outcomes
            ) { throw 'AURA_PROVIDER_RUNTIME_EVENT_RECORD_INVALID' }
        } elseif ($eventType -eq 'AI_PROVIDER_FALLBACK') {
            if (
                [string]$record.locale -cnotin @('en-US', 'id-ID') `
                -or [string]$record.reason -ceq 'SUCCESS' `
                -or [string]$record.reason -cnotin $outcomes
            ) { throw 'AURA_PROVIDER_RUNTIME_EVENT_RECORD_INVALID' }
        }

        if (
            $recordRequestId -ceq $RequestId `
            -and $timestamp -ge $notBefore
        ) {
            if ($records.Count -ge $MaxRecords) {
                throw 'AURA_PROVIDER_RUNTIME_EVENT_RESULT_LIMIT_EXCEEDED'
            }
            $records.Add($record)
        }
    }
    return @($records)
}

function Get-AuraProviderRuntimeEvents {
    param(
        [Parameter(Mandatory)][string]$RequestId,
        [Parameter(Mandatory)][string]$NotBeforeUtc,
        [ValidateRange(1, 64)][int]$MaxRecords = 32
    )

    $eventPath = Assert-AuraPathWithin `
        -Path $script:AuraProviderRuntimeEventLog -Root $script:AuraLogRoot
    $lockPath = Assert-AuraPathWithin `
        -Path $script:AuraProviderRuntimeEventLock -Root $script:AuraLogRoot
    foreach ($path in @($eventPath, $lockPath)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw 'AURA_PROVIDER_RUNTIME_EVENT_LOG_MISSING'
        }
        $item = Get-Item -LiteralPath $path -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'AURA_PROVIDER_RUNTIME_EVENT_PATH_INVALID'
        }
        Assert-AuraOperatorSecretAcl -Path $path
    }
    Assert-AuraOperatorRuntimeContainerAcl -Path $script:AuraLogRoot
    if ((Get-Item -LiteralPath $lockPath -Force).Length -lt 1) {
        throw 'AURA_PROVIDER_RUNTIME_EVENT_LOCK_PATH_INVALID'
    }
    if ((Get-Item -LiteralPath $eventPath -Force).Length -gt 67108864) {
        throw 'AURA_PROVIDER_RUNTIME_EVENT_LOG_TOO_LARGE'
    }

    $lockStream = $null
    $locked = $false
    $deadline = [DateTime]::UtcNow.AddSeconds(5)
    while (-not $locked -and [DateTime]::UtcNow -lt $deadline) {
        try {
            $lockStream = [IO.File]::Open(
                $lockPath,
                [IO.FileMode]::Open,
                [IO.FileAccess]::ReadWrite,
                [IO.FileShare]::ReadWrite
            )
            $lockStream.Lock(0, 1)
            $locked = $true
        } catch [IO.IOException] {
            if ($null -ne $lockStream) { $lockStream.Dispose() }
            $lockStream = $null
            Start-Sleep -Milliseconds 10
        }
    }
    if (-not $locked) {
        throw 'AURA_PROVIDER_RUNTIME_EVENT_LOCK_TIMEOUT'
    }
    try {
        if ((Get-Item -LiteralPath $eventPath -Force).Length -gt 67108864) {
            throw 'AURA_PROVIDER_RUNTIME_EVENT_LOG_TOO_LARGE'
        }
        $lines = @([IO.File]::ReadAllLines($eventPath, [Text.Encoding]::UTF8))
    } finally {
        $lockStream.Unlock(0, 1)
        $lockStream.Dispose()
    }
    return @(ConvertFrom-AuraProviderRuntimeEventLines `
        -Lines $lines -RequestId $RequestId -NotBeforeUtc $NotBeforeUtc `
        -MaxRecords $MaxRecords)
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

function Invoke-AuraRepositoryPythonOperation {
    param(
        [Parameter(Mandatory)]
        [ValidateSet('readiness', 'schema-verify', 'readiness-import')]
        [string]$Operation
    )

    $repositoryRoot = Assert-AuraRepositoryLayout
    $arguments = switch ($Operation) {
        'readiness' { '-m app.jobs.public_demo_readiness' }
        'schema-verify' { '-m app.jobs.demo_schema --operation verify' }
        'readiness-import' { '-B -c "import app.jobs.public_demo_readiness"' }
    }
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = Get-AuraPythonPath
    $startInfo.Arguments = $arguments
    $startInfo.WorkingDirectory = $repositoryRoot
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true

    $process = [System.Diagnostics.Process]::Start($startInfo)
    if ($null -eq $process) { throw 'AURA_PYTHON_PROCESS_START_FAILED' }
    try {
        $standardOutput = $process.StandardOutput.ReadToEnd()
        $standardError = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        return [PSCustomObject]@{
            ExitCode = $process.ExitCode
            StandardOutput = $standardOutput
            StandardError = $standardError
            WorkingDirectory = $repositoryRoot
        }
    } finally {
        $standardOutput = $null
        $standardError = $null
        $process.Dispose()
    }
}

function Test-AuraProductionDatabaseReadiness {
    try {
        Assert-AuraProductionConfiguration
        $readiness = Invoke-AuraRepositoryPythonOperation -Operation readiness
        if ($readiness.ExitCode -ne 0) { return $false }
        $schema = Invoke-AuraRepositoryPythonOperation -Operation schema-verify
        $null = ConvertFrom-AuraSchemaProcessResult -Profile production `
            -Operation verify -ExitCode $schema.ExitCode `
            -StandardOutput $schema.StandardOutput `
            -StandardError $schema.StandardError
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
    Assert-AuraOperatorRuntimeContainerAcl -Path $script:AuraRunRoot
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

function Assert-AuraSystemReadAccess {
    param(
        [Parameter(Mandatory)][string]$Path,
        [switch]$RequireModify
    )
    $acl = Get-Acl -LiteralPath $Path
    $systemAllowed = $false
    foreach ($rule in $acl.Access) {
        try {
            $sid = $rule.IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]
            ).Value
        } catch { continue }
        if ($sid -cne 'S-1-5-18') { continue }
        if ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Deny) {
            throw 'AURA_SYSTEM_ACCESS_DENIED'
        }
        $required = if ($RequireModify) {
            [Security.AccessControl.FileSystemRights]::Modify
        } else {
            [Security.AccessControl.FileSystemRights]::ReadAndExecute
        }
        if (($rule.FileSystemRights -band $required) -eq $required) {
            $systemAllowed = $true
        }
    }
    if (-not $systemAllowed) {
        if ($RequireModify) { throw 'AURA_SYSTEM_MODIFY_ACCESS_MISSING' }
        throw 'AURA_SYSTEM_READ_ACCESS_MISSING'
    }
}

function Write-AuraCleanupOperationLog {
    param(
        [Parameter(Mandatory)][string]$Profile,
        [Parameter(Mandatory)][ValidateSet('dry-run', 'execute')][string]$Mode,
        [Parameter(Mandatory)][ValidateRange(0, 500)][int]$EligibleSessions,
        [Parameter(Mandatory)][ValidateRange(0, 500)][int]$AttemptedSessions,
        [Parameter(Mandatory)][ValidateRange(0, 500)][int]$SuccessfulCleanupCount,
        [Parameter(Mandatory)][ValidateRange(0, 500)][int]$FailedCleanupCount,
        [Parameter(Mandatory)][ValidateSet('success', 'partial_failure', 'failure')]
        [string]$Result,
        [ValidateRange(0, 3600000)][int]$ElapsedMs = 0
    )
    Assert-AuraProfile -Profile $Profile
    Initialize-AuraDataDirectories
    $day = [DateTime]::UtcNow.ToString('yyyyMMdd')
    $path = Join-Path $script:AuraLogRoot "operations-$day.log"
    $line = (
        'timestamp={0} profile={1} stage=CLEANUP mode={2} ' +
        'eligible_sessions={3} attempted_sessions={4} ' +
        'successful_cleanup_count={5} failed_cleanup_count={6} ' +
        'result={7} elapsed_ms={8}'
    ) -f [DateTime]::UtcNow.ToString('o'), $Profile, $Mode, `
        $EligibleSessions, $AttemptedSessions, $SuccessfulCleanupCount, `
        $FailedCleanupCount, $Result, $ElapsedMs
    Add-Content -LiteralPath $path -Value $line -Encoding ascii
    Remove-AuraExpiredFiles -Root $script:AuraLogRoot `
        -Filter 'operations-*.log' -RetentionDays 14 -PreservePath $path
}

function Get-AuraCleanupActivationPath {
    param([string]$Profile = 'production')
    Assert-AuraProfile -Profile $Profile
    return Join-Path $script:AuraRunRoot "cleanup-activation-$Profile.json"
}

function Read-AuraCleanupActivationMarker {
    param([string]$Profile = 'production')
    $path = Get-AuraCleanupActivationPath -Profile $Profile
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    $safePath = Assert-AuraPathWithin -Path $path -Root $script:AuraRunRoot
    Assert-AuraOperatorSecretAcl -Path $safePath
    $item = Get-Item -LiteralPath $safePath -Force
    if (
        $item.Length -lt 1 -or $item.Length -gt 2048 `
        -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
    ) { throw 'AURA_CLEANUP_ACTIVATION_MARKER_INVALID' }
    try { $document = Get-Content -Raw -LiteralPath $safePath | ConvertFrom-Json } catch {
        throw 'AURA_CLEANUP_ACTIVATION_MARKER_INVALID'
    }
    $expectedProperties = @('activatedAtUtc', 'profile', 'state', 'taskName', 'version')
    $actualProperties = @($document.PSObject.Properties.Name | Sort-Object)
    $version = [string]$document.version
    $state = [string]$document.state
    if (
        (@($actualProperties).Count -ne $expectedProperties.Count) `
        -or (@(Compare-Object $actualProperties $expectedProperties).Count -ne 0) `
        -or $version -cnotin @('1', '2') `
        -or [string]$document.profile -cne $Profile `
        -or [string]$document.taskName -cne 'AURA Demo Cleanup'
    ) { throw 'AURA_CLEANUP_ACTIVATION_MARKER_INVALID' }
    if (
        ($version -ceq '1' -and $state -cne 'active') `
        -or ($version -ceq '2' -and $state -cnotin @('activating', 'active'))
    ) { throw 'AURA_CLEANUP_ACTIVATION_MARKER_INVALID' }
    $activatedAt = [DateTimeOffset]::MinValue
    if (-not [DateTimeOffset]::TryParseExact(
        [string]$document.activatedAtUtc,
        'o',
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::RoundtripKind,
        [ref]$activatedAt
    )) { throw 'AURA_CLEANUP_ACTIVATION_MARKER_INVALID' }
    return [PSCustomObject]@{
        Version = [int]$version
        Profile = $Profile
        State = $state
        ActivatedAtUtc = $activatedAt.UtcDateTime
        TaskName = 'AURA Demo Cleanup'
        Path = $safePath
    }
}

function Write-AuraCleanupActivationMarker {
    param(
        [string]$Profile = 'production',
        [Parameter(Mandatory)]
        [ValidateSet('activating')]
        [string]$State,
        [DateTime]$ActivatedAtUtc = [DateTime]::UtcNow
    )
    Assert-AuraProductionProfile -Profile $Profile
    Initialize-AuraDataDirectories
    Assert-AuraOperatorRuntimeContainerAcl -Path $script:AuraRunRoot
    $path = Assert-AuraPathWithin `
        -Path (Get-AuraCleanupActivationPath -Profile $Profile) `
        -Root $script:AuraRunRoot
    if (Test-Path -LiteralPath $path) {
        throw 'AURA_CLEANUP_ACTIVATION_MARKER_EXISTS'
    }
    $tempPath = "$path.$([Guid]::NewGuid().ToString('N')).partial"
    $payload = [ordered]@{
        version = 2
        profile = $Profile
        state = $State
        activatedAtUtc = $ActivatedAtUtc.ToUniversalTime().ToString('o')
        taskName = 'AURA Demo Cleanup'
    } | ConvertTo-Json -Compress
    $installed = $false
    try {
        [IO.File]::WriteAllText($tempPath, $payload, [Text.Encoding]::ASCII)
        Set-AuraOperatorProtectedAcl -Path $tempPath
        [IO.File]::Move($tempPath, $path)
        $installed = $true
        Assert-AuraOperatorSecretAcl -Path $path
        return Read-AuraCleanupActivationMarker -Profile $Profile
    } catch {
        if ($installed) {
            Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        }
        throw
    } finally {
        Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
    }
}

function Set-AuraCleanupActivationMarkerActive {
    param(
        [string]$Profile = 'production',
        [DateTime]$ActivatedAtUtc = [DateTime]::UtcNow
    )
    Assert-AuraProductionProfile -Profile $Profile
    $marker = Read-AuraCleanupActivationMarker -Profile $Profile
    if (
        $null -eq $marker -or $marker.Version -ne 2 `
        -or $marker.State -cne 'activating'
    ) { throw 'AURA_CLEANUP_ACTIVATION_TRANSITION_INVALID' }
    $path = $marker.Path
    $tempPath = "$path.$([Guid]::NewGuid().ToString('N')).partial"
    $backupPath = "$path.$([Guid]::NewGuid().ToString('N')).backup"
    $payload = [ordered]@{
        version = 2
        profile = $Profile
        state = 'active'
        activatedAtUtc = $ActivatedAtUtc.ToUniversalTime().ToString('o')
        taskName = 'AURA Demo Cleanup'
    } | ConvertTo-Json -Compress
    try {
        [IO.File]::WriteAllText($tempPath, $payload, [Text.Encoding]::ASCII)
        Set-AuraOperatorProtectedAcl -Path $tempPath
        [IO.File]::Replace($tempPath, $path, $backupPath)
        return [PSCustomObject]@{
            Version = 2
            Profile = $Profile
            State = 'active'
            ActivatedAtUtc = $ActivatedAtUtc.ToUniversalTime()
            TaskName = 'AURA Demo Cleanup'
            Path = $path
        }
    } finally {
        Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $backupPath -Force -ErrorAction SilentlyContinue
    }
}

function Remove-AuraCleanupActivationMarker {
    param([string]$Profile = 'production')
    $marker = Read-AuraCleanupActivationMarker -Profile $Profile
    if ($null -eq $marker) { return }
    Remove-Item -LiteralPath $marker.Path -Force
    if (Test-Path -LiteralPath $marker.Path) {
        throw 'AURA_CLEANUP_ACTIVATION_MARKER_REMOVE_FAILED'
    }
}

function Get-AuraCleanupTaskArguments {
    param([Parameter(Mandatory)][string]$CleanupScript)
    return "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$CleanupScript`" -Profile production -Mode Execute -Confirmation RUN_AURA_DEMO_CLEANUP"
}

function Get-AuraCleanupTaskPrePr43Arguments {
    param([Parameter(Mandatory)][string]$CleanupScript)
    return "-NoProfile -NonInteractive -File `"$CleanupScript`" -Profile production -Mode Execute -Confirmation RUN_AURA_DEMO_CLEANUP"
}

function New-AuraCleanupTaskXml {
    param(
        [Parameter(Mandatory)][string]$PowerShellPath,
        [Parameter(Mandatory)][string]$CleanupScript,
        [Parameter(Mandatory)][string]$RepositoryRoot,
        [bool]$Enabled = $false
    )
    $command = [Security.SecurityElement]::Escape([string]$PowerShellPath)
    $arguments = [Security.SecurityElement]::Escape(
        (Get-AuraCleanupTaskArguments -CleanupScript $CleanupScript)
    )
    $workingDirectory = [Security.SecurityElement]::Escape([string]$RepositoryRoot)
    $enabledText = ([string]$Enabled).ToLowerInvariant()
    return @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Hourly bounded demo cleanup at minute 17.</Description></RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <Repetition><Interval>PT1H</Interval><Duration>P1D</Duration><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
      <StartBoundary>2024-01-01T00:17:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="System"><UserId>S-1-5-18</UserId><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>false</StartWhenAvailable>
    <UseUnifiedSchedulingEngine>false</UseUnifiedSchedulingEngine>
    <Enabled>$enabledText</Enabled>
    <ExecutionTimeLimit>PT20M</ExecutionTimeLimit>
  </Settings>
  <Actions Context="System">
    <Exec><Command>$command</Command><Arguments>$arguments</Arguments><WorkingDirectory>$workingDirectory</WorkingDirectory></Exec>
  </Actions>
</Task>
"@
}

function ConvertTo-AuraCleanupTaskRegistrationXml {
    param([Parameter(Mandatory)][string]$Xml)
    [xml]$document = $Xml
    $manager = [Xml.XmlNamespaceManager]::new($document.NameTable)
    $manager.AddNamespace(
        't', 'http://schemas.microsoft.com/windows/2004/02/mit/task'
    )
    $unifiedEngine = $document.SelectSingleNode(
        '/t:Task/t:Settings/t:UseUnifiedSchedulingEngine', $manager
    )
    if (
        [string]$document.Task.version -cne '1.4' `
        -or $null -eq $unifiedEngine `
        -or [string]$unifiedEngine.InnerText -cne 'false'
    ) { throw 'AURA_CLEANUP_TASK_REGISTRATION_XML_INVALID' }
    $document.Task.SetAttribute('version', '1.2')
    $null = $unifiedEngine.ParentNode.RemoveChild($unifiedEngine)
    return $document.OuterXml
}

function Register-AuraCleanupTaskDefinition {
    param(
        [Parameter(Mandatory)][string]$TaskName,
        [Parameter(Mandatory)][string]$Xml,
        [switch]$Force
    )
    $registrationXml = ConvertTo-AuraCleanupTaskRegistrationXml -Xml $Xml
    $parameters = @{
        TaskName = $TaskName
        Xml = $registrationXml
        ErrorAction = 'Stop'
    }
    if ($Force) { $parameters.Force = $true }
    Register-ScheduledTask @parameters | Out-Null
}

function Test-AuraCleanupTaskXml {
    param(
        [Parameter(Mandatory)][string]$Xml,
        [Parameter(Mandatory)][string]$PowerShellPath,
        [Parameter(Mandatory)][string]$CleanupScript,
        [Parameter(Mandatory)][string]$RepositoryRoot,
        [Parameter(Mandatory)][bool]$Enabled,
        [AllowNull()][object]$EffectiveRunLevel,
        [AllowNull()][object]$EffectiveStartWhenAvailable,
        [AllowNull()][object]$EffectiveUseUnifiedSchedulingEngine,
        [AllowNull()][object]$EffectiveEnabled
    )
    try {
        [xml]$document = $Xml
        $manager = [Xml.XmlNamespaceManager]::new($document.NameTable)
        $manager.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')
        function Get-TaskValue([string]$XPath) {
            $node = $document.SelectSingleNode($XPath, $manager)
            if ($null -eq $node) { return $null }
            return [string]$node.InnerText
        }
        $expectedEnabled = ([string]$Enabled).ToLowerInvariant()
        $runLevel = Get-TaskValue '/t:Task/t:Principals/t:Principal/t:RunLevel'
        $hasEffectiveRunLevel = $PSBoundParameters.ContainsKey(
            'EffectiveRunLevel'
        )
        $effectiveRunLevelSafe = $false
        if ($hasEffectiveRunLevel -and $null -ne $EffectiveRunLevel) {
            $effectiveRunLevelText = [string]$EffectiveRunLevel
            $effectiveRunLevelSafe = (
                $effectiveRunLevelText -ceq 'LeastPrivilege' `
                -or $effectiveRunLevelText -ceq 'Limited'
            )
        }
        $runLevelSafe = if ($null -eq $runLevel) {
            $hasEffectiveRunLevel -and $effectiveRunLevelSafe
        } else {
            $runLevel -ceq 'LeastPrivilege' -and (
                -not $hasEffectiveRunLevel -or $effectiveRunLevelSafe
            )
        }
        $startWhenAvailable = Get-TaskValue `
            '/t:Task/t:Settings/t:StartWhenAvailable'
        $hasEffectiveStartWhenAvailable = $PSBoundParameters.ContainsKey(
            'EffectiveStartWhenAvailable'
        )
        $effectiveStartWhenAvailableSafe = (
            $hasEffectiveStartWhenAvailable `
            -and $EffectiveStartWhenAvailable -is [bool] `
            -and -not [bool]$EffectiveStartWhenAvailable
        )
        $startWhenAvailableSafe = if ($null -eq $startWhenAvailable) {
            $effectiveStartWhenAvailableSafe
        } else {
            $startWhenAvailable -ceq 'false' -and (
                -not $hasEffectiveStartWhenAvailable `
                -or $effectiveStartWhenAvailableSafe
            )
        }
        $useUnifiedSchedulingEngine = Get-TaskValue `
            '/t:Task/t:Settings/t:UseUnifiedSchedulingEngine'
        $hasEffectiveUseUnifiedSchedulingEngine = $PSBoundParameters.ContainsKey(
            'EffectiveUseUnifiedSchedulingEngine'
        )
        $effectiveUseUnifiedSchedulingEngineSafe = (
            $hasEffectiveUseUnifiedSchedulingEngine `
            -and $EffectiveUseUnifiedSchedulingEngine -is [bool] `
            -and -not [bool]$EffectiveUseUnifiedSchedulingEngine
        )
        $useUnifiedSchedulingEngineSafe = if (
            $null -eq $useUnifiedSchedulingEngine
        ) {
            [string]$document.Task.version -ceq '1.2' `
                -and $effectiveUseUnifiedSchedulingEngineSafe
        } else {
            [string]$document.Task.version -ceq '1.4' `
                -and $useUnifiedSchedulingEngine -ceq 'false' `
                -and $effectiveUseUnifiedSchedulingEngineSafe
        }
        $enabledValue = Get-TaskValue '/t:Task/t:Settings/t:Enabled'
        $hasEffectiveEnabled = $PSBoundParameters.ContainsKey(
            'EffectiveEnabled'
        )
        $effectiveEnabledSafe = (
            $hasEffectiveEnabled `
            -and $EffectiveEnabled -is [bool] `
            -and [bool]$EffectiveEnabled -eq $Enabled
        )
        $enabledSafe = if ($null -eq $enabledValue) {
            $Enabled -and $effectiveEnabledSafe
        } else {
            $enabledValue -ceq $expectedEnabled -and $effectiveEnabledSafe
        }
        return (
            (Get-TaskValue '/t:Task/t:Triggers/t:CalendarTrigger/t:Repetition/t:Interval') -ceq 'PT1H' `
            -and (Get-TaskValue '/t:Task/t:Triggers/t:CalendarTrigger/t:Repetition/t:Duration') -ceq 'P1D' `
            -and (Get-TaskValue '/t:Task/t:Triggers/t:CalendarTrigger/t:StartBoundary') -ceq '2024-01-01T00:17:00' `
            -and (Get-TaskValue '/t:Task/t:Triggers/t:CalendarTrigger/t:ScheduleByDay/t:DaysInterval') -ceq '1' `
            -and (Get-TaskValue '/t:Task/t:Principals/t:Principal/t:UserId') -ceq 'S-1-5-18' `
            -and $null -eq (Get-TaskValue '/t:Task/t:Principals/t:Principal/t:LogonType') `
            -and $runLevelSafe `
            -and (Get-TaskValue '/t:Task/t:Settings/t:MultipleInstancesPolicy') -ceq 'IgnoreNew' `
            -and $startWhenAvailableSafe `
            -and $useUnifiedSchedulingEngineSafe `
            -and $enabledSafe `
            -and (Get-TaskValue '/t:Task/t:Settings/t:ExecutionTimeLimit') -ceq 'PT20M' `
            -and (Get-TaskValue '/t:Task/t:Actions/t:Exec/t:Command') -ceq $PowerShellPath `
            -and (Get-TaskValue '/t:Task/t:Actions/t:Exec/t:Arguments') -ceq (Get-AuraCleanupTaskArguments -CleanupScript $CleanupScript) `
            -and (Get-TaskValue '/t:Task/t:Actions/t:Exec/t:WorkingDirectory') -ceq $RepositoryRoot
        )
    } catch { return $false }
}

function Get-AuraCleanupTaskEffectiveUseUnifiedSchedulingEngine {
    param(
        [Parameter(Mandatory)][string]$TaskName,
        [AllowNull()][object]$Task
    )
    if ($null -ne $Task -and $null -ne $Task.Settings) {
        $property = $Task.Settings.PSObject.Properties[
            'UseUnifiedSchedulingEngine'
        ]
        if ($null -ne $property -and $property.Value -is [bool]) {
            return [bool]$property.Value
        }
    }
    $service = New-Object -ComObject 'Schedule.Service'
    $service.Connect()
    $registered = $service.GetFolder('\').GetTask('\' + $TaskName)
    $value = $registered.Definition.Settings.UseUnifiedSchedulingEngine
    if ($value -isnot [bool]) {
        throw 'AURA_CLEANUP_TASK_UNIFIED_ENGINE_STATE_UNKNOWN'
    }
    return [bool]$value
}

function Get-AuraCleanupTaskSnapshot {
    param(
        [string]$TaskName = 'AURA Demo Cleanup',
        [Parameter(Mandatory)][string]$PowerShellPath,
        [Parameter(Mandatory)][string]$CleanupScript,
        [Parameter(Mandatory)][string]$RepositoryRoot
    )
    $tasks = @(Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)
    if ($tasks.Count -eq 0) { return $null }
    if ($tasks.Count -ne 1) { throw 'AURA_CLEANUP_TASK_IDENTITY_AMBIGUOUS' }
    $xml = [string](Export-ScheduledTask -TaskName $TaskName -ErrorAction Stop)
    $disabled = [string]$tasks[0].State -ceq 'Disabled'
    $effectiveUseUnifiedSchedulingEngine = `
        Get-AuraCleanupTaskEffectiveUseUnifiedSchedulingEngine `
            -TaskName $TaskName -Task $tasks[0]
    $matches = Test-AuraCleanupTaskXml -Xml $xml -PowerShellPath $PowerShellPath `
        -CleanupScript $CleanupScript -RepositoryRoot $RepositoryRoot `
        -Enabled (-not $disabled) `
        -EffectiveRunLevel $tasks[0].Principal.RunLevel `
        -EffectiveStartWhenAvailable $tasks[0].Settings.StartWhenAvailable `
        -EffectiveUseUnifiedSchedulingEngine `
            $effectiveUseUnifiedSchedulingEngine `
        -EffectiveEnabled $tasks[0].Settings.Enabled
    return [PSCustomObject]@{
        State = [string]$tasks[0].State
        Disabled = $disabled
        DefinitionMatches = $matches
        Xml = $xml
    }
}

function Test-AuraCleanupTaskPreUnifiedEngineXml {
    param(
        [Parameter(Mandatory)][string]$Xml,
        [Parameter(Mandatory)][string]$PowerShellPath,
        [Parameter(Mandatory)][string]$CleanupScript,
        [Parameter(Mandatory)][string]$RepositoryRoot,
        [Parameter(Mandatory)][bool]$Enabled,
        [AllowNull()][object]$EffectiveRunLevel,
        [AllowNull()][object]$EffectiveStartWhenAvailable,
        [AllowNull()][object]$EffectiveUseUnifiedSchedulingEngine,
        [AllowNull()][object]$EffectiveEnabled,
        [AllowNull()][object]$EffectiveTriggerEnabled,
        [AllowNull()][object]$EffectiveStopAtDurationEnd
    )
    try {
        [xml]$document = $Xml
        $manager = [Xml.XmlNamespaceManager]::new($document.NameTable)
        $manager.AddNamespace(
            't', 'http://schemas.microsoft.com/windows/2004/02/mit/task'
        )
        function Get-PreUnifiedTaskValue([string]$XPath) {
            $node = $document.SelectSingleNode($XPath, $manager)
            if ($null -eq $node) { return $null }
            return [string]$node.InnerText
        }
        $actionNodes = @($document.SelectNodes(
            '/t:Task/t:Actions/*', $manager
        ))
        $triggerNodes = @($document.SelectNodes(
            '/t:Task/t:Triggers/*', $manager
        ))
        $principalNodes = @($document.SelectNodes(
            '/t:Task/t:Principals/t:Principal', $manager
        ))
        $triggerEnabled = Get-PreUnifiedTaskValue `
            '/t:Task/t:Triggers/t:CalendarTrigger/t:Enabled'
        $triggerEnabledSafe = if ($null -eq $triggerEnabled) {
            $EffectiveTriggerEnabled -is [bool] `
                -and [bool]$EffectiveTriggerEnabled
        } else {
            $triggerEnabled -ceq 'true' -and (
                $null -eq $EffectiveTriggerEnabled `
                -or ($EffectiveTriggerEnabled -is [bool] `
                    -and [bool]$EffectiveTriggerEnabled)
            )
        }
        $stopAtDurationEnd = Get-PreUnifiedTaskValue `
            '/t:Task/t:Triggers/t:CalendarTrigger/t:Repetition/t:StopAtDurationEnd'
        $stopAtDurationEndSafe = if ($null -eq $stopAtDurationEnd) {
            $EffectiveStopAtDurationEnd -is [bool] `
                -and -not [bool]$EffectiveStopAtDurationEnd
        } else {
            $stopAtDurationEnd -ceq 'false' -and (
                $null -eq $EffectiveStopAtDurationEnd `
                -or ($EffectiveStopAtDurationEnd -is [bool] `
                    -and -not [bool]$EffectiveStopAtDurationEnd)
            )
        }
        $unifiedEngineNode = $document.SelectSingleNode(
            '/t:Task/t:Settings/t:UseUnifiedSchedulingEngine', $manager
        )
        if (
            $actionNodes.Count -ne 1 `
            -or $actionNodes[0].LocalName -cne 'Exec' `
            -or $triggerNodes.Count -ne 1 `
            -or $triggerNodes[0].LocalName -cne 'CalendarTrigger' `
            -or $principalNodes.Count -ne 1 `
            -or [string]$principalNodes[0].GetAttribute('id') -cne 'System' `
            -or [string]$document.Task.version -cne '1.4' `
            -or (Get-PreUnifiedTaskValue `
                '/t:Task/t:RegistrationInfo/t:Description') -cne `
                'Hourly bounded demo cleanup at minute 17.' `
            -or -not $triggerEnabledSafe `
            -or -not $stopAtDurationEndSafe `
            -or (Get-PreUnifiedTaskValue `
                '/t:Task/t:Settings/t:DisallowStartIfOnBatteries') -cne `
                'false' `
            -or (Get-PreUnifiedTaskValue `
                '/t:Task/t:Settings/t:StopIfGoingOnBatteries') -cne `
                'false' `
            -or [string]$document.Task.Actions.Context -cne 'System' `
            -or $null -eq $unifiedEngineNode `
            -or [string]$unifiedEngineNode.InnerText -cne 'true' `
            -or $EffectiveUseUnifiedSchedulingEngine -isnot [bool] `
            -or -not [bool]$EffectiveUseUnifiedSchedulingEngine
        ) { return $false }
        $unifiedEngineNode.InnerText = 'false'
        return Test-AuraCleanupTaskXml -Xml $document.OuterXml `
            -PowerShellPath $PowerShellPath -CleanupScript $CleanupScript `
            -RepositoryRoot $RepositoryRoot -Enabled $Enabled `
            -EffectiveRunLevel $EffectiveRunLevel `
            -EffectiveStartWhenAvailable $EffectiveStartWhenAvailable `
            -EffectiveUseUnifiedSchedulingEngine $false `
            -EffectiveEnabled $EffectiveEnabled
    } catch { return $false }
}

function Get-AuraCleanupTaskPreUnifiedEngineSnapshot {
    param(
        [string]$TaskName = 'AURA Demo Cleanup',
        [Parameter(Mandatory)][string]$PowerShellPath,
        [Parameter(Mandatory)][string]$CleanupScript,
        [Parameter(Mandatory)][string]$RepositoryRoot
    )
    $tasks = @(Get-ScheduledTask -TaskName $TaskName `
        -ErrorAction SilentlyContinue)
    if ($tasks.Count -eq 0) { return $null }
    if ($tasks.Count -ne 1) {
        throw 'AURA_CLEANUP_TASK_IDENTITY_AMBIGUOUS'
    }
    $task = $tasks[0]
    $xml = [string](Export-ScheduledTask -TaskName $TaskName -ErrorAction Stop)
    $effectiveUseUnifiedSchedulingEngine = `
        Get-AuraCleanupTaskEffectiveUseUnifiedSchedulingEngine `
            -TaskName $TaskName -Task $task
    $disabled = (
        [string]$task.State -ceq 'Disabled' `
        -and $task.Settings.Enabled -is [bool] `
        -and -not [bool]$task.Settings.Enabled
    )
    $matches = (
        [string]$task.TaskPath -ceq '\' `
        -and (Test-AuraCleanupTaskSystemPrincipal `
            -UserId $task.Principal.UserId) `
        -and [string]$task.Principal.LogonType -ceq 'ServiceAccount' `
        -and @($task.Triggers).Count -eq 1 `
        -and (Test-AuraCleanupTaskPreUnifiedEngineXml -Xml $xml `
            -PowerShellPath $PowerShellPath -CleanupScript $CleanupScript `
            -RepositoryRoot $RepositoryRoot -Enabled (-not $disabled) `
            -EffectiveRunLevel $task.Principal.RunLevel `
            -EffectiveStartWhenAvailable `
                $task.Settings.StartWhenAvailable `
            -EffectiveUseUnifiedSchedulingEngine `
                $effectiveUseUnifiedSchedulingEngine `
            -EffectiveEnabled $task.Settings.Enabled `
            -EffectiveTriggerEnabled $task.Triggers[0].Enabled `
            -EffectiveStopAtDurationEnd `
                $task.Triggers[0].Repetition.StopAtDurationEnd)
    )
    return [PSCustomObject]@{
        State = [string]$task.State
        Disabled = $disabled
        DefinitionMatches = $matches
        Xml = $xml
        DefinitionVersion = 'pre-unified-engine-fix'
    }
}

function Test-AuraCleanupTaskPrePr43Xml {
    param(
        [Parameter(Mandatory)][string]$Xml,
        [Parameter(Mandatory)][string]$PowerShellPath,
        [Parameter(Mandatory)][string]$CleanupScript,
        [Parameter(Mandatory)][string]$RepositoryRoot,
        [AllowNull()][object]$EffectiveRunLevel,
        [AllowNull()][object]$EffectiveStartWhenAvailable,
        [AllowNull()][object]$EffectiveUseUnifiedSchedulingEngine,
        [AllowNull()][object]$EffectiveEnabled,
        [AllowNull()][object]$EffectiveTriggerEnabled,
        [AllowNull()][object]$EffectiveStopAtDurationEnd
    )
    try {
        [xml]$document = $Xml
        $manager = [Xml.XmlNamespaceManager]::new($document.NameTable)
        $manager.AddNamespace(
            't', 'http://schemas.microsoft.com/windows/2004/02/mit/task'
        )
        function Get-PrePr43TaskValue([string]$XPath) {
            $node = $document.SelectSingleNode($XPath, $manager)
            if ($null -eq $node) { return $null }
            return [string]$node.InnerText
        }
        $actionNodes = @($document.SelectNodes(
            '/t:Task/t:Actions/*', $manager
        ))
        $triggerNodes = @($document.SelectNodes(
            '/t:Task/t:Triggers/*', $manager
        ))
        $principalNodes = @($document.SelectNodes(
            '/t:Task/t:Principals/t:Principal', $manager
        ))
        $argumentNodes = @($document.SelectNodes(
            '/t:Task/t:Actions/t:Exec/t:Arguments', $manager
        ))
        $triggerEnabled = Get-PrePr43TaskValue `
            '/t:Task/t:Triggers/t:CalendarTrigger/t:Enabled'
        $triggerEnabledSafe = if ($null -eq $triggerEnabled) {
            $EffectiveTriggerEnabled -is [bool] `
                -and [bool]$EffectiveTriggerEnabled
        } else {
            $triggerEnabled -ceq 'true' -and (
                $null -eq $EffectiveTriggerEnabled `
                -or ($EffectiveTriggerEnabled -is [bool] `
                    -and [bool]$EffectiveTriggerEnabled)
            )
        }
        $stopAtDurationEnd = Get-PrePr43TaskValue `
            '/t:Task/t:Triggers/t:CalendarTrigger/t:Repetition/t:StopAtDurationEnd'
        $stopAtDurationEndSafe = if ($null -eq $stopAtDurationEnd) {
            $EffectiveStopAtDurationEnd -is [bool] `
                -and -not [bool]$EffectiveStopAtDurationEnd
        } else {
            $stopAtDurationEnd -ceq 'false' -and (
                $null -eq $EffectiveStopAtDurationEnd `
                -or ($EffectiveStopAtDurationEnd -is [bool] `
                    -and -not [bool]$EffectiveStopAtDurationEnd)
            )
        }
        if (
            $actionNodes.Count -ne 1 `
            -or $actionNodes[0].LocalName -cne 'Exec' `
            -or $triggerNodes.Count -ne 1 `
            -or $triggerNodes[0].LocalName -cne 'CalendarTrigger' `
            -or $principalNodes.Count -ne 1 `
            -or [string]$principalNodes[0].GetAttribute('id') -cne 'System' `
            -or [string]$document.Task.version -cne '1.4' `
            -or (Get-PrePr43TaskValue `
                '/t:Task/t:RegistrationInfo/t:Description') -cne `
                'Hourly bounded demo cleanup at minute 17.' `
            -or -not $triggerEnabledSafe `
            -or -not $stopAtDurationEndSafe `
            -or (Get-PrePr43TaskValue `
                '/t:Task/t:Settings/t:DisallowStartIfOnBatteries') -cne `
                'false' `
            -or (Get-PrePr43TaskValue `
                '/t:Task/t:Settings/t:StopIfGoingOnBatteries') -cne `
                'false' `
            -or [string]$document.Task.Actions.Context -cne 'System' `
            -or $argumentNodes.Count -ne 1 `
            -or [string]$argumentNodes[0].InnerText -cne `
                (Get-AuraCleanupTaskPrePr43Arguments `
                    -CleanupScript $CleanupScript)
        ) { return $false }
        $argumentNodes[0].InnerText = Get-AuraCleanupTaskArguments `
            -CleanupScript $CleanupScript
        if (
            $PSBoundParameters.ContainsKey(
                'EffectiveUseUnifiedSchedulingEngine'
            ) -and (Test-AuraCleanupTaskPreUnifiedEngineXml `
                -Xml $document.OuterXml -PowerShellPath $PowerShellPath `
                -CleanupScript $CleanupScript -RepositoryRoot $RepositoryRoot `
                -Enabled $false -EffectiveRunLevel $EffectiveRunLevel `
                -EffectiveStartWhenAvailable $EffectiveStartWhenAvailable `
                -EffectiveUseUnifiedSchedulingEngine `
                    $EffectiveUseUnifiedSchedulingEngine `
                -EffectiveEnabled $EffectiveEnabled `
                -EffectiveTriggerEnabled $EffectiveTriggerEnabled `
                -EffectiveStopAtDurationEnd $EffectiveStopAtDurationEnd)
        ) { return $true }
        $currentParameters = @{
            Xml = $document.OuterXml
            PowerShellPath = $PowerShellPath
            CleanupScript = $CleanupScript
            RepositoryRoot = $RepositoryRoot
            Enabled = $false
            EffectiveRunLevel = $EffectiveRunLevel
            EffectiveStartWhenAvailable = $EffectiveStartWhenAvailable
            EffectiveEnabled = $EffectiveEnabled
        }
        if ($PSBoundParameters.ContainsKey(
            'EffectiveUseUnifiedSchedulingEngine'
        )) {
            $currentParameters.EffectiveUseUnifiedSchedulingEngine = `
                $EffectiveUseUnifiedSchedulingEngine
        }
        return Test-AuraCleanupTaskXml @currentParameters
    } catch { return $false }
}

function Test-AuraCleanupTaskSystemPrincipal {
    param([AllowNull()][object]$UserId)
    if ($null -eq $UserId) { return $false }
    try {
        $text = [string]$UserId
        $sid = if ($text -match '^S-\d-(?:\d+-)+\d+$') {
            [Security.Principal.SecurityIdentifier]::new($text)
        } else {
            [Security.Principal.NTAccount]::new($text).Translate(
                [Security.Principal.SecurityIdentifier]
            )
        }
        return $sid.Value -ceq 'S-1-5-18'
    } catch { return $false }
}

function Get-AuraCleanupTaskPrePr43Snapshot {
    param(
        [string]$TaskName = 'AURA Demo Cleanup',
        [Parameter(Mandatory)][string]$PowerShellPath,
        [Parameter(Mandatory)][string]$CleanupScript,
        [Parameter(Mandatory)][string]$RepositoryRoot
    )
    $tasks = @(Get-ScheduledTask -TaskName $TaskName `
        -ErrorAction SilentlyContinue)
    if ($tasks.Count -eq 0) { return $null }
    if ($tasks.Count -ne 1) {
        throw 'AURA_CLEANUP_TASK_IDENTITY_AMBIGUOUS'
    }
    $task = $tasks[0]
    $xml = [string](Export-ScheduledTask -TaskName $TaskName -ErrorAction Stop)
    $effectiveUseUnifiedSchedulingEngine = `
        Get-AuraCleanupTaskEffectiveUseUnifiedSchedulingEngine `
            -TaskName $TaskName -Task $task
    $disabled = (
        [string]$task.State -ceq 'Disabled' `
        -and $task.Settings.Enabled -is [bool] `
        -and -not [bool]$task.Settings.Enabled
    )
    $matches = (
        [string]$task.TaskPath -ceq '\' `
        -and (Test-AuraCleanupTaskSystemPrincipal `
            -UserId $task.Principal.UserId) `
        -and [string]$task.Principal.LogonType -ceq 'ServiceAccount' `
        -and @($task.Triggers).Count -eq 1 `
        -and (Test-AuraCleanupTaskPrePr43Xml -Xml $xml `
            -PowerShellPath $PowerShellPath -CleanupScript $CleanupScript `
            -RepositoryRoot $RepositoryRoot `
            -EffectiveRunLevel $task.Principal.RunLevel `
            -EffectiveStartWhenAvailable `
                $task.Settings.StartWhenAvailable `
            -EffectiveUseUnifiedSchedulingEngine `
                $effectiveUseUnifiedSchedulingEngine `
            -EffectiveEnabled $task.Settings.Enabled `
            -EffectiveTriggerEnabled $task.Triggers[0].Enabled `
            -EffectiveStopAtDurationEnd `
                $task.Triggers[0].Repetition.StopAtDurationEnd)
    )
    return [PSCustomObject]@{
        State = [string]$task.State
        Disabled = $disabled
        DefinitionMatches = $matches
        Xml = $xml
        DefinitionVersion = 'pre-pr43'
    }
}

function Test-AuraCleanupProcessActive {
    param([Parameter(Mandatory)][string]$CleanupScript)
    $escapedScript = [regex]::Escape([IO.Path]::GetFullPath($CleanupScript))
    $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    return @($processes | Where-Object {
        $commandLine = [string]$_.CommandLine
        [int]$_.ProcessId -ne $PID `
        -and -not [string]::IsNullOrEmpty($commandLine) -and (
            $commandLine -match $escapedScript `
            -or $commandLine -match '(?i)(^|\s)-m\s+app\.jobs\.demo_cleanup(?:\s|$)'
        )
    }).Count -gt 0
}

function Assert-AuraCleanupTaskUpgradeHostSafe {
    param([Parameter(Mandatory)][string]$CleanupScript)
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )) { throw 'AURA_ADMIN_REQUIRED' }
    foreach ($kind in @('aura', 'funnel')) {
        $state = Get-AuraOwnedProcessState -Kind $kind -Profile production
        if ([string]$state.State -cne 'absent') {
            throw 'AURA_CLEANUP_TASK_UPGRADE_PRODUCTION_NOT_OFFLINE'
        }
    }
    if (-not (Test-AuraPortClosed -Port 8000)) {
        throw 'AURA_CLEANUP_TASK_UPGRADE_PRODUCTION_NOT_OFFLINE'
    }
    if (Test-AuraCleanupProcessActive -CleanupScript $CleanupScript) {
        throw 'AURA_CLEANUP_TASK_UPGRADE_PROCESS_ACTIVE'
    }
}

function Get-AuraCleanupTaskUpgradeSourceSnapshot {
    param(
        [string]$TaskName = 'AURA Demo Cleanup',
        [Parameter(Mandatory)][string]$PowerShellPath,
        [Parameter(Mandatory)][string]$CleanupScript,
        [Parameter(Mandatory)][string]$RepositoryRoot
    )
    $preUnified = Get-AuraCleanupTaskPreUnifiedEngineSnapshot `
        -TaskName $TaskName -PowerShellPath $PowerShellPath `
        -CleanupScript $CleanupScript -RepositoryRoot $RepositoryRoot
    if ($null -ne $preUnified -and $preUnified.DefinitionMatches) {
        return $preUnified
    }
    $prePr43 = Get-AuraCleanupTaskPrePr43Snapshot -TaskName $TaskName `
        -PowerShellPath $PowerShellPath -CleanupScript $CleanupScript `
        -RepositoryRoot $RepositoryRoot
    if ($null -ne $prePr43 -and $prePr43.DefinitionMatches) {
        return $prePr43
    }
    return $preUnified
}

function Restore-AuraCleanupTaskUpgradeSource {
    param(
        [Parameter(Mandatory)][string]$OldXml,
        [Parameter(Mandatory)][ValidateSet('pre-pr43', 'pre-unified-engine-fix')]
        [string]$DefinitionVersion,
        [Parameter(Mandatory)][string]$PowerShellPath,
        [Parameter(Mandatory)][string]$CleanupScript,
        [Parameter(Mandatory)][string]$RepositoryRoot,
        [string]$TaskName = 'AURA Demo Cleanup'
    )
    try {
        Register-ScheduledTask -TaskName $TaskName -Xml $OldXml -Force `
            -ErrorAction Stop | Out-Null
        $restored = Get-AuraCleanupTaskUpgradeSourceSnapshot `
            -TaskName $TaskName `
            -PowerShellPath $PowerShellPath -CleanupScript $CleanupScript `
            -RepositoryRoot $RepositoryRoot
        $marker = Read-AuraCleanupActivationMarker -Profile production
        if (
            $null -eq $restored -or -not $restored.Disabled `
            -or -not $restored.DefinitionMatches `
            -or $restored.DefinitionVersion -cne $DefinitionVersion `
            -or $null -ne $marker
        ) { throw 'AURA_CLEANUP_TASK_UPGRADE_ROLLBACK_VALIDATION_FAILED' }
    } catch {
        throw 'AURA_CLEANUP_TASK_UPGRADE_ROLLBACK_FAILED'
    }
}

function Upgrade-AuraCleanupTaskVersioned {
    param(
        [Parameter(Mandatory)][string]$PowerShellPath,
        [Parameter(Mandatory)][string]$CleanupScript,
        [Parameter(Mandatory)][string]$RepositoryRoot,
        [string]$TaskName = 'AURA Demo Cleanup'
    )
    Assert-AuraProductionProfile -Profile production
    if ($TaskName -cne 'AURA Demo Cleanup') {
        if (
            $env:AURA_TEST_ALLOW_CLEANUP_TASK_UPGRADE -cne '1' `
            -or $TaskName -cnotmatch `
                '^AURA Cleanup Upgrade Test [a-f0-9]{32}$'
        ) { throw 'AURA_CLEANUP_TASK_UPGRADE_IDENTITY_INVALID' }
    }
    Assert-AuraCleanupTaskUpgradeHostSafe -CleanupScript $CleanupScript
    $marker = Read-AuraCleanupActivationMarker -Profile production
    $current = Get-AuraCleanupTaskSnapshot -TaskName $TaskName `
        -PowerShellPath $PowerShellPath -CleanupScript $CleanupScript `
        -RepositoryRoot $RepositoryRoot
    if ($null -ne $current -and $current.DefinitionMatches) {
        if (
            $null -ne $marker -and $marker.State -ceq 'active' `
            -and -not $current.Disabled
        ) { return 'AURA_CLEANUP_TASK_ALREADY_ACTIVE' }
        if ($null -eq $marker -and $current.Disabled) {
            return 'AURA_CLEANUP_TASK_ALREADY_STAGED'
        }
        throw 'AURA_CLEANUP_TASK_STATE_MISMATCH'
    }
    if ($null -ne $marker) {
        throw 'AURA_CLEANUP_TASK_UPGRADE_MARKER_PRESENT'
    }
    $old = Get-AuraCleanupTaskUpgradeSourceSnapshot -TaskName $TaskName `
        -PowerShellPath $PowerShellPath -CleanupScript $CleanupScript `
        -RepositoryRoot $RepositoryRoot
    if (
        $null -eq $old -or -not $old.Disabled `
        -or -not $old.DefinitionMatches
    ) { throw 'AURA_CLEANUP_TASK_DEFINITION_MISMATCH' }

    $captured = Get-AuraCleanupTaskUpgradeSourceSnapshot -TaskName $TaskName `
        -PowerShellPath $PowerShellPath -CleanupScript $CleanupScript `
        -RepositoryRoot $RepositoryRoot
    Assert-AuraCleanupTaskUpgradeHostSafe -CleanupScript $CleanupScript
    if (
        $null -eq $captured -or -not $captured.Disabled `
        -or -not $captured.DefinitionMatches `
        -or $captured.DefinitionVersion -cne $old.DefinitionVersion `
        -or $captured.Xml -cne $old.Xml `
        -or $null -ne (Read-AuraCleanupActivationMarker -Profile production) `
        -or (Test-AuraCleanupProcessActive -CleanupScript $CleanupScript)
    ) { throw 'AURA_CLEANUP_TASK_UPGRADE_PRECONDITION_CHANGED' }
    $oldXml = $captured.Xml
    $newXml = New-AuraCleanupTaskXml -PowerShellPath $PowerShellPath `
        -CleanupScript $CleanupScript -RepositoryRoot $RepositoryRoot `
        -Enabled $false
    $registered = $false
    try {
        Register-AuraCleanupTaskDefinition -TaskName $TaskName `
            -Xml $newXml -Force
        $registered = $true
        $upgraded = Get-AuraCleanupTaskSnapshot -TaskName $TaskName `
            -PowerShellPath $PowerShellPath -CleanupScript $CleanupScript `
            -RepositoryRoot $RepositoryRoot
        if (
            $null -eq $upgraded -or -not $upgraded.Disabled `
            -or -not $upgraded.DefinitionMatches `
            -or $null -ne (Read-AuraCleanupActivationMarker `
                -Profile production)
        ) { throw 'AURA_CLEANUP_TASK_UPGRADE_VALIDATION_FAILED' }
        return 'AURA_CLEANUP_TASK_UPGRADED_DISABLED'
    } catch {
        $failureCode = if ($registered) {
            'AURA_CLEANUP_TASK_UPGRADE_VALIDATION_FAILED'
        } else { 'AURA_CLEANUP_TASK_UPGRADE_REGISTRATION_FAILED' }
        Restore-AuraCleanupTaskUpgradeSource -OldXml $oldXml `
            -DefinitionVersion $captured.DefinitionVersion `
            -PowerShellPath $PowerShellPath -CleanupScript $CleanupScript `
            -RepositoryRoot $RepositoryRoot -TaskName $TaskName
        throw $failureCode
    }
}

function Assert-AuraCleanupExecutionActivated {
    param(
        [string]$Profile = 'production',
        [string]$TaskName = 'AURA Demo Cleanup'
    )
    Assert-AuraProductionProfile -Profile $Profile
    $marker = Read-AuraCleanupActivationMarker -Profile $Profile
    if ($null -eq $marker -or $marker.State -cne 'active') {
        throw 'AURA_CLEANUP_EXECUTION_NOT_ACTIVE'
    }
    $repositoryRoot = Assert-AuraRepositoryLayout
    $cleanupScript = Join-Path $PSScriptRoot 'Run-DemoCleanup.ps1'
    $powerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
    $task = Get-AuraCleanupTaskSnapshot -TaskName $TaskName `
        -PowerShellPath $powerShell -CleanupScript $cleanupScript `
        -RepositoryRoot $repositoryRoot
    if ($null -eq $task -or $task.Disabled -or -not $task.DefinitionMatches) {
        throw 'AURA_CLEANUP_EXECUTION_TASK_INVALID'
    }
    return $marker
}

function Register-AuraCleanupTaskStaged {
    param(
        [Parameter(Mandatory)][string]$PowerShellPath,
        [Parameter(Mandatory)][string]$CleanupScript,
        [Parameter(Mandatory)][string]$RepositoryRoot,
        [string]$TaskName = 'AURA Demo Cleanup'
    )
    $marker = Read-AuraCleanupActivationMarker -Profile production
    $existing = Get-AuraCleanupTaskSnapshot -TaskName $TaskName `
        -PowerShellPath $PowerShellPath -CleanupScript $CleanupScript `
        -RepositoryRoot $RepositoryRoot
    if ($null -ne $existing) {
        if (-not $existing.DefinitionMatches) { throw 'AURA_CLEANUP_TASK_DEFINITION_MISMATCH' }
        if (
            $null -ne $marker -and $marker.State -ceq 'active' `
            -and -not $existing.Disabled
        ) {
            return 'AURA_CLEANUP_TASK_ALREADY_ACTIVE'
        }
        if ($null -eq $marker -and $existing.Disabled) {
            return 'AURA_CLEANUP_TASK_ALREADY_STAGED'
        }
        throw 'AURA_CLEANUP_TASK_STATE_MISMATCH'
    }
    if ($null -ne $marker) { throw 'AURA_CLEANUP_TASK_EXPECTED_MISSING' }

    $created = $false
    try {
        $xml = New-AuraCleanupTaskXml -PowerShellPath $PowerShellPath `
            -CleanupScript $CleanupScript -RepositoryRoot $RepositoryRoot `
            -Enabled $false
        Register-AuraCleanupTaskDefinition -TaskName $TaskName -Xml $xml
        $created = $true
        $staged = Get-AuraCleanupTaskSnapshot -TaskName $TaskName `
            -PowerShellPath $PowerShellPath -CleanupScript $CleanupScript `
            -RepositoryRoot $RepositoryRoot
        if (
            $null -eq $staged -or -not $staged.Disabled `
            -or -not $staged.DefinitionMatches
        ) { throw 'AURA_CLEANUP_TASK_VALIDATION_FAILED' }
        return 'AURA_CLEANUP_TASK_STAGED_DISABLED'
    } catch {
        $failure = $_
        $removeAttributedArtifact = $created
        if (-not $removeAttributedArtifact) {
            try {
                $partialTask = @(Get-ScheduledTask -TaskName $TaskName `
                    -ErrorAction SilentlyContinue)
                if ($partialTask.Count -eq 1) {
                    $partialXml = [string](Export-ScheduledTask `
                        -TaskName $TaskName -ErrorAction Stop)
                    $removeAttributedArtifact = Test-AuraCleanupTaskXml `
                        -Xml $partialXml -PowerShellPath $PowerShellPath `
                        -CleanupScript $CleanupScript `
                        -RepositoryRoot $RepositoryRoot -Enabled $false `
                        -EffectiveRunLevel $partialTask[0].Principal.RunLevel `
                        -EffectiveStartWhenAvailable `
                            $partialTask[0].Settings.StartWhenAvailable `
                        -EffectiveUseUnifiedSchedulingEngine `
                            (Get-AuraCleanupTaskEffectiveUseUnifiedSchedulingEngine `
                                -TaskName $TaskName -Task $partialTask[0]) `
                        -EffectiveEnabled $partialTask[0].Settings.Enabled
                }
            } catch { $removeAttributedArtifact = $false }
        }
        if ($removeAttributedArtifact) {
            try {
                Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false `
                    -ErrorAction Stop
            } catch { throw 'AURA_CLEANUP_TASK_REGISTRATION_ROLLBACK_FAILED' }
        }
        throw $failure
    }
}

function Assert-AuraCleanupActivationWindow {
    param([DateTime]$NowLocal = [DateTime]::Now)
    $now = $NowLocal.ToLocalTime()
    if ($now.Minute -eq 17) { throw 'AURA_CLEANUP_ACTIVATION_WINDOW_UNSAFE' }
    $next = [DateTime]::new(
        $now.Year, $now.Month, $now.Day, $now.Hour, 17, 0, $now.Kind
    )
    if ($next -le $now) { $next = $next.AddHours(1) }
    if (($next - $now).TotalMinutes -lt 2) {
        throw 'AURA_CLEANUP_ACTIVATION_WINDOW_UNSAFE'
    }
}

function Enable-AuraCleanupTaskActivation {
    param(
        [Parameter(Mandatory)][string]$PowerShellPath,
        [Parameter(Mandatory)][string]$CleanupScript,
        [Parameter(Mandatory)][string]$RepositoryRoot,
        [string]$TaskName = 'AURA Demo Cleanup'
    )
    if ($null -ne (Read-AuraCleanupActivationMarker -Profile production)) {
        throw 'AURA_CLEANUP_ALREADY_ACTIVATED'
    }
    $staged = Get-AuraCleanupTaskSnapshot -TaskName $TaskName `
        -PowerShellPath $PowerShellPath -CleanupScript $CleanupScript `
        -RepositoryRoot $RepositoryRoot
    if ($null -eq $staged) { throw 'AURA_CLEANUP_TASK_MISSING' }
    if (-not $staged.DefinitionMatches -or -not $staged.Disabled) {
        throw 'AURA_CLEANUP_TASK_NOT_EXACTLY_STAGED'
    }
    $null = Write-AuraCleanupActivationMarker -Profile production `
        -State activating
    $enableAttempted = $false
    try {
        Assert-AuraCleanupActivationWindow
        $enableAttempted = $true
        Enable-ScheduledTask -TaskName $TaskName -ErrorAction Stop | Out-Null
        $enabled = Get-AuraCleanupTaskSnapshot -TaskName $TaskName `
            -PowerShellPath $PowerShellPath -CleanupScript $CleanupScript `
            -RepositoryRoot $RepositoryRoot
        if ($null -eq $enabled -or $enabled.Disabled -or -not $enabled.DefinitionMatches) {
            throw 'AURA_CLEANUP_TASK_ENABLE_VALIDATION_FAILED'
        }
        $activeMarker = Set-AuraCleanupActivationMarkerActive `
            -Profile production
        if ($activeMarker.State -cne 'active') {
            throw 'AURA_CLEANUP_ACTIVATION_TRANSITION_INVALID'
        }
        return 'AURA_CLEANUP_ACTIVATED'
    } catch {
        $failure = $_
        if ($enableAttempted) {
            try {
                Disable-ScheduledTask -TaskName $TaskName -ErrorAction Stop | Out-Null
                $rolledBack = Get-AuraCleanupTaskSnapshot -TaskName $TaskName `
                    -PowerShellPath $PowerShellPath -CleanupScript $CleanupScript `
                    -RepositoryRoot $RepositoryRoot
                if ($null -eq $rolledBack -or -not $rolledBack.Disabled) {
                    throw 'AURA_CLEANUP_ACTIVATION_ROLLBACK_FAILED'
                }
            } catch { throw 'AURA_CLEANUP_ACTIVATION_ROLLBACK_FAILED' }
        }
        try {
            Remove-AuraCleanupActivationMarker -Profile production
        } catch { throw 'AURA_CLEANUP_ACTIVATION_ROLLBACK_FAILED' }
        throw $failure
    }
}

function Disable-AuraCleanupTaskActivation {
    param(
        [Parameter(Mandatory)][string]$PowerShellPath,
        [Parameter(Mandatory)][string]$CleanupScript,
        [Parameter(Mandatory)][string]$RepositoryRoot,
        [string]$TaskName = 'AURA Demo Cleanup'
    )
    if ($null -eq (Read-AuraCleanupActivationMarker -Profile production)) {
        throw 'AURA_CLEANUP_NOT_ACTIVATED'
    }
    $active = Get-AuraCleanupTaskSnapshot -TaskName $TaskName `
        -PowerShellPath $PowerShellPath -CleanupScript $CleanupScript `
        -RepositoryRoot $RepositoryRoot
    $activeIsPreUnified = $false
    if ($null -eq $active -or -not $active.DefinitionMatches) {
        $active = Get-AuraCleanupTaskPreUnifiedEngineSnapshot `
            -TaskName $TaskName -PowerShellPath $PowerShellPath `
            -CleanupScript $CleanupScript -RepositoryRoot $RepositoryRoot
        $activeIsPreUnified = (
            $null -ne $active -and $active.DefinitionMatches
        )
    }
    if ($null -eq $active -or -not $active.DefinitionMatches) {
        throw 'AURA_CLEANUP_TASK_ACTIVE_STATE_INVALID'
    }
    if (-not $active.Disabled) {
        Disable-ScheduledTask -TaskName $TaskName -ErrorAction Stop | Out-Null
    }
    $disabled = if ($activeIsPreUnified) {
        Get-AuraCleanupTaskPreUnifiedEngineSnapshot -TaskName $TaskName `
            -PowerShellPath $PowerShellPath -CleanupScript $CleanupScript `
            -RepositoryRoot $RepositoryRoot
    } else {
        Get-AuraCleanupTaskSnapshot -TaskName $TaskName `
            -PowerShellPath $PowerShellPath -CleanupScript $CleanupScript `
            -RepositoryRoot $RepositoryRoot
    }
    if ($null -eq $disabled -or -not $disabled.Disabled -or -not $disabled.DefinitionMatches) {
        throw 'AURA_CLEANUP_TASK_DISABLE_VALIDATION_FAILED'
    }
    Remove-AuraCleanupActivationMarker -Profile production
    return 'AURA_CLEANUP_DEACTIVATED'
}

function Get-AuraCleanupHealth {
    param(
        [string]$Profile = 'production',
        [string]$TaskName = 'AURA Demo Cleanup',
        [DateTime]$NowUtc = [DateTime]::UtcNow,
        [ValidateRange(1, 168)][int]$StaleAfterHours = 3
    )
    Assert-AuraProfile -Profile $Profile
    $base = [ordered]@{
        Status = 'CLEANUP_NOT_CONFIGURED'
        Activated = $false
        ReadyCompatible = $true
        LastAttemptAge = 'never'
        LastDryRunAge = 'never'
        LastSuccessAge = 'never'
    }
    try { $marker = Read-AuraCleanupActivationMarker -Profile $Profile } catch {
        $base.Status = 'CLEANUP_ACTIVATION_INVALID'
        $base.ReadyCompatible = $false
        return [PSCustomObject]$base
    }
    try {
        $repositoryRoot = Assert-AuraRepositoryLayout
        $cleanupScript = Join-Path $PSScriptRoot 'Run-DemoCleanup.ps1'
        $powerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
        $task = Get-AuraCleanupTaskSnapshot -TaskName $TaskName `
            -PowerShellPath $powerShell -CleanupScript $cleanupScript `
            -RepositoryRoot $repositoryRoot
    } catch {
        $task = [PSCustomObject]@{ Disabled = $false; DefinitionMatches = $false }
    }
    if ($null -eq $marker) {
        if ($null -ne $task -and -not $task.Disabled) {
            $base.Status = 'CLEANUP_ACTIVATION_INCONSISTENT'
            $base.ReadyCompatible = $false
        }
        return [PSCustomObject]$base
    }
    if ($marker.State -cne 'active') {
        $base.Status = 'CLEANUP_ACTIVATION_INCOMPLETE'
        $base.ReadyCompatible = $false
        return [PSCustomObject]$base
    }
    $base.Activated = $true
    $base.ReadyCompatible = $false
    if ($null -eq $task) {
        $base.Status = 'CLEANUP_TASK_MISSING'
        return [PSCustomObject]$base
    }
    if (-not $task.DefinitionMatches) {
        $base.Status = 'CLEANUP_TASK_INVALID'
        return [PSCustomObject]$base
    }
    if ($task.Disabled) {
        $base.Status = 'CLEANUP_TASK_DISABLED'
        return [PSCustomObject]$base
    }

    $records = [System.Collections.Generic.List[object]]::new()
    $pattern = (
        '^timestamp=(?<timestamp>\S+) profile=(?<profile>staging|production) ' +
        'stage=CLEANUP mode=(?<mode>dry-run|execute) eligible_sessions=\d+ attempted_sessions=\d+ ' +
        'successful_cleanup_count=\d+ failed_cleanup_count=\d+ ' +
        'result=(?<result>success|partial_failure|failure) elapsed_ms=\d+$'
    )
    foreach ($file in Get-ChildItem -LiteralPath $script:AuraLogRoot -File `
        -Filter 'operations-*.log' -ErrorAction SilentlyContinue) {
        foreach ($line in Get-Content -LiteralPath $file.FullName `
            -ErrorAction SilentlyContinue) {
            $match = [regex]::Match([string]$line, $pattern)
            if (-not $match.Success -or $match.Groups['profile'].Value -cne $Profile) {
                continue
            }
            $timestamp = [DateTimeOffset]::MinValue
            if (-not [DateTimeOffset]::TryParse(
                $match.Groups['timestamp'].Value,
                [Globalization.CultureInfo]::InvariantCulture,
                [Globalization.DateTimeStyles]::RoundtripKind,
                [ref]$timestamp
            )) { continue }
            if ($timestamp.UtcDateTime -lt $marker.ActivatedAtUtc) { continue }
            $records.Add([PSCustomObject]@{
                Timestamp = $timestamp.UtcDateTime
                Mode = $match.Groups['mode'].Value
                Result = $match.Groups['result'].Value
            })
        }
    }
    $latestDryRun = $records | Where-Object Mode -eq 'dry-run' |
        Sort-Object Timestamp -Descending | Select-Object -First 1
    $executeRecords = @($records | Where-Object Mode -eq 'execute')
    $latest = $executeRecords | Sort-Object Timestamp -Descending | Select-Object -First 1
    $latestSuccess = $executeRecords | Where-Object Result -eq 'success' |
        Sort-Object Timestamp -Descending | Select-Object -First 1
    function Get-CleanupAge($Record) {
        if ($null -eq $Record) { return 'never' }
        if (($NowUtc.ToUniversalTime() - $Record.Timestamp).TotalHours -gt $StaleAfterHours) {
            return 'stale'
        }
        return 'fresh'
    }
    $base.LastAttemptAge = Get-CleanupAge $latest
    $base.LastDryRunAge = Get-CleanupAge $latestDryRun
    $base.LastSuccessAge = Get-CleanupAge $latestSuccess
    if ($null -eq $latestSuccess) {
        $base.Status = if ($null -ne $latest) { 'CLEANUP_FAILED' } else { 'CLEANUP_NEVER_RAN' }
        return [PSCustomObject]$base
    }
    if ($latest.Result -ne 'success') {
        $base.Status = 'CLEANUP_FAILED'
        return [PSCustomObject]$base
    }
    $age = $NowUtc.ToUniversalTime() - $latestSuccess.Timestamp
    if ($age.TotalHours -gt $StaleAfterHours) {
        $base.Status = 'CLEANUP_STALE'
        return [PSCustomObject]$base
    }
    $base.Status = 'CLEANUP_HEALTHY'
    $base.ReadyCompatible = $true
    return [PSCustomObject]$base
}

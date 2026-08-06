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
    $blocked = @('S-1-1-0', 'S-1-5-11', 'S-1-5-32-545')
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

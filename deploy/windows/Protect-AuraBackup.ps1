[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('staging', 'production')]
    [string]$Profile,
    [Parameter(Mandatory)][string]$BackupPath,
    [Parameter(Mandatory)][ValidateSet('PROTECT_AURA_BACKUP')]
    [string]$Confirmation
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'AuraWindows.Common.ps1')

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'AURA_ADMIN_REQUIRED'
}

$repositoryRoot = Get-AuraRepositoryRoot
$currentRoot = [IO.Path]::GetFullPath((Get-Location).Path).TrimEnd('\')
if ($currentRoot -ne $repositoryRoot.TrimEnd('\')) {
    throw 'AURA_BACKUP_PROTECTION_REPOSITORY_ROOT_REQUIRED'
}

$safeBackup = Assert-AuraPathWithin -Path $BackupPath -Root $script:AuraBackupRoot
$expectedDatabase = if ($Profile -eq 'production') {
    'aura_demo_public'
} else {
    'aura_demo_staging'
}
$expectedNamePattern = '^' + [Regex]::Escape($expectedDatabase) + `
    '_[0-9]{8}T[0-9]{6}Z\.dump$'
if (
    -not (Test-Path -LiteralPath $safeBackup -PathType Leaf) `
    -or [IO.Path]::GetFileName($safeBackup) -notmatch $expectedNamePattern `
    -or (Get-Item -LiteralPath $safeBackup).Length -le 0
) {
    throw 'AURA_BACKUP_PROTECTION_ARTIFACT_INVALID'
}

Assert-AuraOperatorRuntimeContainerAcl -Path $script:AuraBackupRoot
Set-AuraOperatorProtectedAcl -Path $safeBackup

$pgRestore = Resolve-AuraPostgreSQLTool -ToolName 'pg_restore.exe'
& $pgRestore --list $safeBackup 1>$null 2>$null
if ($LASTEXITCODE -ne 0) { throw 'AURA_BACKUP_PROTECTION_ARCHIVE_INVALID' }
Assert-AuraOperatorSecretAcl -Path $safeBackup
Write-Output (
    'AURA_BACKUP_PROTECTED database={0} archive=valid acl=protected' -f `
        $expectedDatabase
)
